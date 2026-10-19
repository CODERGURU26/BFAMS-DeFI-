import unittest
import uuid
from decimal import Decimal

from app.integrations.setu.exceptions import (
    SetuConfigurationError,
    SetuTestDatabaseError,
)
from app.integrations.setu.storage import store_setu_normalized_data
from app.models import DataSyncRun, FinancialAccount, FinancialConnection, Transaction
from app.schemas import NormalizedAccount, NormalizedTransaction


class FakeStorageSession:
    def __init__(
        self,
        database_name="BFAMS-DeFI-test",
        *,
        fail_account_insert=False,
        connection_count=1,
    ):
        self.database_name = database_name
        self.fail_account_insert = fail_account_insert
        self.connection_count = connection_count
        self.database_guard_passed = False
        self.added = []
        self.persisted = []
        self.rollback_count = 0
        self.commit_count = 0
        self.flush_count = 0

    def scalar(self, statement):
        query = str(statement).lower()
        if "current_database" in query:
            self.database_guard_passed = (
                self.database_name == "BFAMS-DeFI-test"
            )
            return self.database_name
        return self.connection_count

    def scalars(self, _statement):
        return type("ScalarResult", (), {"first": lambda _self: None})()

    def add(self, record):
        if not self.database_guard_passed:
            raise AssertionError("TEST database guard must precede every write")
        if isinstance(record, FinancialAccount) and self.fail_account_insert:
            raise RuntimeError("mock account insert failure")
        self.added.append(record)

    def flush(self):
        self.flush_count += 1
        for record in self.added:
            if isinstance(record, FinancialAccount) and record.id is None:
                record.id = uuid.uuid4()

    def commit(self):
        self.commit_count += 1
        self.persisted.extend(self.added)
        self.added.clear()

    def rollback(self):
        self.rollback_count += 1
        self.added.clear()


def connection(**overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "external_connection_id": "SETU-SANDBOX-1",
    }
    values.update(overrides)
    return FinancialConnection(**values)


def normalized_inputs():
    account = NormalizedAccount(
        external_account_id="stable-account-ref",
        account_type="deposit",
        currency="INR",
    )
    transactions = [
        NormalizedTransaction(
            external_transaction_id="stable-credit-id",
            transaction_type="credit",
            amount="12.34",
            currency="INR",
            transaction_at="2026-01-01T00:00:00+00:00",
        ),
        NormalizedTransaction(
            external_transaction_id="stable-debit-id",
            transaction_type="debit",
            amount="5.67",
            currency="INR",
            transaction_at="2026-01-02T00:00:00+00:00",
        ),
    ]
    return [account], {account.external_account_id: transactions}, []


class TestSetuStorage(unittest.TestCase):
    def test_stores_accounts_and_credit_debit_transactions_via_store_sync(self):
        session = FakeStorageSession()
        account, transactions, rejected = normalized_inputs()
        sandbox_connection = connection()

        run = store_setu_normalized_data(
            session,
            sandbox_connection,
            "setu_sandbox",
            account,
            transactions,
            rejected,
        )

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.records_processed, 3)
        self.assertTrue(session.database_guard_passed)
        self.assertEqual(session.rollback_count, 0)
        self.assertEqual(session.commit_count, 1)
        stored_accounts = [
            item for item in session.persisted if isinstance(item, FinancialAccount)
        ]
        stored_transactions = [
            item for item in session.persisted if isinstance(item, Transaction)
        ]
        self.assertEqual(len(stored_accounts), 1)
        self.assertEqual(
            [(item.transaction_type, item.amount) for item in stored_transactions],
            [("credit", Decimal("12.34")), ("debit", Decimal("5.67"))],
        )
        self.assertTrue(all(item.amount > 0 for item in stored_transactions))
        self.assertEqual(stored_accounts[0].connection_id, sandbox_connection.id)
        self.assertEqual(stored_accounts[0].external_account_id, "stable-account-ref")

    def test_wrong_database_fails_before_any_write(self):
        session = FakeStorageSession(database_name="BFAMS-DeFI")
        accounts, transactions, rejected = normalized_inputs()

        with self.assertRaises(SetuTestDatabaseError):
            store_setu_normalized_data(
                session,
                connection(),
                "setu_sandbox",
                accounts,
                transactions,
                rejected,
            )

        self.assertFalse(session.database_guard_passed)
        self.assertEqual(session.added, [])
        self.assertEqual(session.persisted, [])
        self.assertEqual(session.commit_count, 0)

    def test_wrong_provider_or_connection_is_rejected_before_write(self):
        accounts, transactions, rejected = normalized_inputs()
        invalid_connections = (
            ("dhan_manual", connection()),
            (
                "setu_sandbox",
                connection(external_connection_id="TEST-CONNECTION-1"),
            ),
        )
        for provider_code, sandbox_connection in invalid_connections:
            with self.subTest(provider_code=provider_code):
                session = FakeStorageSession()
                with self.assertRaises(SetuConfigurationError):
                    store_setu_normalized_data(
                        session,
                        sandbox_connection,
                        provider_code,
                        accounts,
                        transactions,
                        rejected,
                    )
                self.assertEqual(session.added, [])
                self.assertEqual(session.persisted, [])

    def test_rejects_setu_user_with_multiple_connections(self):
        session = FakeStorageSession(connection_count=2)
        accounts, transactions, rejected = normalized_inputs()

        with self.assertRaises(SetuConfigurationError):
            store_setu_normalized_data(
                session,
                connection(),
                "setu_sandbox",
                accounts,
                transactions,
                rejected,
            )

        self.assertEqual(session.added, [])
        self.assertEqual(session.persisted, [])
        self.assertFalse(session.database_guard_passed)

    def test_rejects_transaction_for_unknown_account_before_write(self):
        session = FakeStorageSession()
        accounts, transactions, rejected = normalized_inputs()
        transactions["unknown-account"] = transactions.pop("stable-account-ref")

        with self.assertRaises(SetuConfigurationError):
            store_setu_normalized_data(
                session,
                connection(),
                "setu_sandbox",
                accounts,
                transactions,
                rejected,
            )

        self.assertEqual(session.added, [])
        self.assertEqual(session.persisted, [])

    def test_store_sync_rolls_back_failed_account_insert(self):
        session = FakeStorageSession(fail_account_insert=True)
        accounts, transactions, rejected = normalized_inputs()

        with self.assertRaisesRegex(RuntimeError, "mock account insert failure"):
            store_setu_normalized_data(
                session,
                connection(),
                "setu_sandbox",
                accounts,
                transactions,
                rejected,
            )

        self.assertEqual(session.rollback_count, 1)
        self.assertEqual(session.commit_count, 1)
        self.assertEqual(session.added, [])
        self.assertEqual(len(session.persisted), 1)
        self.assertIsInstance(session.persisted[0], DataSyncRun)
        self.assertEqual(session.persisted[0].status, "failed")
        self.assertEqual(session.persisted[0].records_processed, 0)


if __name__ == "__main__":
    unittest.main()
