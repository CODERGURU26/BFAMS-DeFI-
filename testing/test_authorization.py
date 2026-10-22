import os
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import routes as auth_routes
from app.auth.authorization import get_owned_account, get_owned_connection
from app.auth.deps import get_auth_db, get_current_user
from app.auth.tokens import create_access_token
from app.dependencies import get_mf_db
from app.models import (
    ConsentRecord,
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    InvestmentHolding,
    Transaction,
    User,
    UserCredential,
)
from app.routes import (
    accounts,
    connections,
    mf_analytics,
    portfolio,
    setu,
    testing_dashboard,
    transactions,
)
from app.routes import setu as setu_routes
from database import get_test_engine


class AuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        cls.connection = cls.engine.connect()
        cls.outer_transaction = cls.connection.begin()
        database_name = cls.connection.execute(
            text("SELECT current_database()")
        ).scalar_one()
        if database_name != "BFAMS-DeFI-test":
            cls.outer_transaction.rollback()
            cls.connection.close()
            cls.engine.dispose()
            raise AssertionError("Tests require BFAMS-DeFI-test.")
        cls.counts_before = cls._table_counts()

        cls.app = FastAPI()
        for route_module in (
            accounts,
            connections,
            transactions,
            portfolio,
            testing_dashboard,
            setu,
            mf_analytics,
            auth_routes,
        ):
            cls.app.include_router(route_module.router)

        def override_test_db():
            session = Session(
                bind=cls.connection,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                session.close()

        cls.app.dependency_overrides[get_auth_db] = override_test_db
        cls.app.dependency_overrides[get_mf_db] = override_test_db
        cls.app.dependency_overrides[setu_routes.get_setu_test_db] = override_test_db

    @classmethod
    def _table_counts(cls):
        return {
            table: cls.connection.execute(
                text(f'SELECT count(*) FROM "{table}"')
            ).scalar_one()
            for table in (
                "users",
                "user_credentials",
                "data_providers",
                "financial_connections",
                "financial_accounts",
                "consent_records",
                "transactions",
                "investment_holdings",
            )
        }

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.dependency_overrides.clear()
            if cls._table_counts() != cls.counts_before:
                raise AssertionError("Authorization tests changed database counts.")
        finally:
            cls.outer_transaction.rollback()
            cls.connection.close()
            cls.engine.dispose()

    def setUp(self):
        self.secret = "authorization-test-secret-long-enough"
        self.environment = patch.dict(
            os.environ,
            {"AUTH_JWT_SECRET": self.secret},
        )
        self.environment.start()
        self.client = TestClient(self.app, base_url="https://testserver")
        self.db = Session(
            bind=self.connection,
            join_transaction_mode="create_savepoint",
        )
        self.db.execute(text("SELECT 1"))
        self.user_a, self.token_a = self._create_user()
        self.user_b, self.token_b = self._create_user()
        self.account_a, self.connection_a = self._create_account(
            self.user_a,
            external_id="ACCOUNT-A",
            provider_code=f"provider-{uuid.uuid4()}",
        )
        self.account_b, self.connection_b = self._create_account(
            self.user_b,
            external_id="ACCOUNT-B",
            provider_code=f"provider-{uuid.uuid4()}",
        )
        self.transaction_a = self._create_transaction(self.account_a)
        self.transaction_b = self._create_transaction(self.account_b)
        self.holding_a = self._create_holding(self.account_a, "Private A")
        self.holding_b = self._create_holding(self.account_b, "Private B")
        self.setu_connection_b = self._create_setu_connection(self.user_b)
        self.foreign_consent_id = f"private-consent-{uuid.uuid4()}"
        self.db.add(
            ConsentRecord(
                connection_id=self.setu_connection_b.id,
                external_consent_id=self.foreign_consent_id,
                status="approved",
            )
        )
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.client.close()
        self.environment.stop()

    def _create_user(self):
        user = User()
        self.db.add(user)
        self.db.flush()
        self.db.add(
            UserCredential(
                user_id=user.id,
                email=f"{uuid.uuid4()}@example.test",
                password_hash="test-hash",
                failed_attempts=0,
                locked_until=None,
                is_active=True,
                password_changed_at=datetime.now(timezone.utc),
            )
        )
        self.db.flush()
        token, _ = create_access_token(str(user.id))
        return user, token

    def _create_account(self, user, *, external_id, provider_code):
        provider = DataProvider(
            provider_code=provider_code,
            provider_name=provider_code,
            provider_type="manual_import",
        )
        self.db.add(provider)
        self.db.flush()
        connection = FinancialConnection(
            user_id=user.id,
            provider_id=provider.id,
            external_connection_id=f"connection-{uuid.uuid4()}",
            status="active",
        )
        self.db.add(connection)
        self.db.flush()
        account = FinancialAccount(
            connection_id=connection.id,
            external_account_id=external_id,
            account_type="demat",
            account_name=external_id,
            currency="INR",
            status="active",
        )
        self.db.add(account)
        self.db.flush()
        return account, connection

    def _create_setu_connection(self, user):
        provider = DataProvider(
            provider_code=f"setu-test-{uuid.uuid4()}",
            provider_name="Setu test",
            provider_type="account_aggregator",
        )
        self.db.add(provider)
        self.db.flush()
        connection = FinancialConnection(
            user_id=user.id,
            provider_id=provider.id,
            external_connection_id=f"SETU-SANDBOX-{uuid.uuid4()}",
            status="active",
        )
        self.db.add(connection)
        self.db.flush()
        return connection

    def _create_transaction(self, account):
        transaction = Transaction(
            account_id=account.id,
            external_transaction_id=str(uuid.uuid4()),
            transaction_type="credit",
            amount="125.00",
            currency="INR",
            description=f"private-{account.external_account_id}",
            category="investment",
            transaction_status="completed",
            transaction_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        self.db.add(transaction)
        self.db.flush()
        return transaction

    def _create_holding(self, account, name):
        holding = InvestmentHolding(
            account_id=account.id,
            asset_name=name,
            asset_identifier=None,
            asset_type="EQUITY",
            quantity=1,
            unit_price=125,
            market_value=125,
            currency="INR",
            observed_at=datetime.now(timezone.utc),
        )
        self.db.add(holding)
        self.db.flush()
        return holding

    def _headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_users_only_list_their_accounts_connections_and_transactions(self):
        accounts_a = self.client.get(
            "/accounts/",
            headers=self._headers(self.token_a),
        )
        connections_a = self.client.get(
            "/connections/",
            headers=self._headers(self.token_a),
        )
        transactions_a = self.client.get(
            "/transactions/",
            headers=self._headers(self.token_a),
        )

        self.assertEqual(accounts_a.status_code, 200, accounts_a.text)
        self.assertEqual([item["id"] for item in accounts_a.json()], [str(self.account_a.id)])
        self.assertEqual(connections_a.status_code, 200)
        self.assertEqual(
            [item["id"] for item in connections_a.json()],
            [str(self.connection_a.id)],
        )
        self.assertEqual(transactions_a.status_code, 200)
        self.assertEqual(
            [item["id"] for item in transactions_a.json()],
            [str(self.transaction_a.id)],
        )
        self.assertNotIn("ACCOUNT-B", str(accounts_a.json()))
        self.assertNotIn("Private B", str(transactions_a.json()))

        accounts_b = self.client.get(
            "/accounts/",
            headers=self._headers(self.token_b),
        )
        transactions_b = self.client.get(
            "/transactions/",
            headers=self._headers(self.token_b),
        )
        connections_b = self.client.get(
            "/connections/",
            headers=self._headers(self.token_b),
        )
        self.assertEqual(
            [item["id"] for item in accounts_b.json()],
            [str(self.account_b.id)],
        )
        self.assertEqual(
            [item["id"] for item in transactions_b.json()],
            [str(self.transaction_b.id)],
        )
        self.assertEqual(
            {item["id"] for item in connections_b.json()},
            {str(self.connection_b.id), str(self.setu_connection_b.id)},
        )

    def test_account_holdings_and_valuation_are_owner_only(self):
        own = self.client.get(
            f"/portfolio/holdings/{self.account_a.id}",
            headers=self._headers(self.token_a),
        )
        foreign = self.client.get(
            f"/portfolio/holdings/{self.account_b.id}",
            headers=self._headers(self.token_a),
        )
        valuation = self.client.get(
            f"/portfolio/valuation/{self.account_b.id}",
            headers=self._headers(self.token_a),
        )

        self.assertEqual(own.status_code, 200)
        self.assertEqual([row["id"] for row in own.json()], [str(self.holding_a.id)])
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(valuation.status_code, 404)
        for response in (foreign, valuation):
            self.assertEqual(response.json(), {"detail": "Resource not found."})
            for private_id in (
                str(self.account_b.id),
                str(self.connection_b.id),
                str(self.user_b.id),
            ):
                self.assertNotIn(private_id, response.text)

        own_b = self.client.get(
            f"/portfolio/holdings/{self.account_b.id}",
            headers=self._headers(self.token_b),
        )
        foreign_b = self.client.get(
            f"/portfolio/holdings/{self.account_a.id}",
            headers=self._headers(self.token_b),
        )
        self.assertEqual([row["id"] for row in own_b.json()], [str(self.holding_b.id)])
        self.assertEqual(foreign_b.status_code, 404)

    def test_dashboard_analytics_are_scoped_to_authenticated_user(self):
        response = self.client.get(
            "/testing/dashboard/data",
            headers=self._headers(self.token_a),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"]["accounts"], 1)
        self.assertEqual(payload["counts"]["transactions"], 1)
        self.assertEqual(payload["counts"]["holdings"], 1)
        self.assertNotIn("ACCOUNT-B", response.text)
        self.assertNotIn("Private B", response.text)
        self.assertNotIn(str(self.user_b.id), response.text)

    def test_nonowned_setu_sync_is_404_before_external_call(self):
        with patch.object(setu_routes, "SetuClient") as client:
            response = self.client.post(
                f"/api/setu/consents/{self.foreign_consent_id}/sessions/private-session/sync",
                headers=self._headers(self.token_a),
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Resource not found."})
        self.assertNotIn(self.foreign_consent_id, response.text)
        self.assertNotIn(str(self.connection_b.id), response.text)
        client.assert_not_called()

    def test_nonowned_setu_session_is_404_before_external_call(self):
        with patch.object(setu_routes, "SetuClient") as client:
            response = self.client.get(
                f"/api/setu/sessions/private-session?consent_id={self.foreign_consent_id}",
                headers=self._headers(self.token_a),
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Resource not found."})
        client.assert_not_called()

    def test_dhan_operation_on_foreign_account_stops_before_external_call(self):
        headers = {
            **self._headers(self.token_a),
            "X-BFAMS-Sync": "1",
            "Origin": "https://testserver",
        }
        with patch.object(portfolio, "DhanClient") as client:
            valuation = self.client.post(
                "/portfolio/dhan/live-valuation",
                headers=headers,
            )
            sync = self.client.post(
                "/portfolio/dhan/sync?dry_run=true",
                headers=headers,
            )

        self.assertEqual(valuation.status_code, 404)
        self.assertEqual(valuation.json(), {"detail": "Resource not found."})
        self.assertEqual(sync.status_code, 404)
        self.assertEqual(sync.json(), {"detail": "Resource not found."})
        client.assert_not_called()

    def test_private_route_coverage_and_unauthenticated_responses(self):
        protected_prefixes = (
            "/accounts",
            "/connections",
            "/transactions",
            "/portfolio",
            "/testing/dashboard/data",
            "/api/setu",
        )
        private_routes = [
            route
            for module in (
                accounts,
                connections,
                transactions,
                portfolio,
                testing_dashboard,
                setu,
            )
            for route in module.router.routes
            if isinstance(route, APIRoute)
            and any(route.path.startswith(prefix) for prefix in protected_prefixes)
            and route.path != "/api/setu/health"
        ]
        self.assertTrue(private_routes)
        for route in private_routes:
            dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(get_current_user, dependency_calls, route.path)

        unauthenticated_requests = (
            ("GET", "/accounts/"),
            ("GET", "/connections/"),
            ("GET", "/transactions/"),
            ("GET", f"/portfolio/holdings/{self.account_a.id}"),
            ("GET", "/testing/dashboard/data"),
            ("GET", f"/api/setu/consents/{self.foreign_consent_id}"),
            (
                "POST",
                "/portfolio/dhan/live-valuation",
            ),
            (
                "POST",
                f"/api/setu/consents/{self.foreign_consent_id}/sessions/private-session/sync",
            ),
            (
                "GET",
                f"/api/setu/sessions/private-session?consent_id={self.foreign_consent_id}",
            ),
        )
        for method, path in unauthenticated_requests:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401)

    def test_ownership_helpers_hide_foreign_identifiers(self):
        with self.assertRaises(HTTPException) as account_error:
            get_owned_account(self.db, self.account_b.id, self.user_a.id)
        with self.assertRaises(HTTPException) as connection_error:
            get_owned_connection(self.db, self.connection_b.id, self.user_a.id)
        self.assertEqual(account_error.exception.status_code, 404)
        self.assertEqual(connection_error.exception.status_code, 404)
        self.assertNotIn(str(self.account_b.id), str(account_error.exception.detail))
        self.assertNotIn(str(self.connection_b.id), str(connection_error.exception.detail))


if __name__ == "__main__":
    unittest.main()
