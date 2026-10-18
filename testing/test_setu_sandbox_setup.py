import unittest
import uuid

from app.models import DataProvider, FinancialConnection, User
from scripts.setu_sandbox_setup import (
    EXTERNAL_CONNECTION_ID,
    PROVIDER_CODE,
    PROVIDER_NAME,
    PROVIDER_TYPE,
    setup_sandbox_connection,
)


class FakeSession:
    def __init__(self, database_name="BFAMS-DeFI-test"):
        self.database_name = database_name
        self.providers = []
        self.users = []
        self.connections = []
        self.pending = []
        self.commits = 0
        self.rollbacks = 0
        self.database_checks = 0

    def scalar(self, statement):
        sql = str(statement)
        if "current_database" in sql.lower():
            self.database_checks += 1
            return self.database_name
        if "financial_connections.user_id" in sql:
            return sum(
                connection.user_id == self._connection_user_id
                for connection in self.connections
            )
        if "financial_connections.provider_id" in sql:
            return sum(
                connection.provider_id == self._provider_id
                for connection in self.connections
            )
        return 0

    def scalars(self, statement):
        sql = str(statement)
        if "data_providers" in sql:
            rows = [
                provider
                for provider in self.providers
                if provider.provider_code == PROVIDER_CODE
            ]
            if rows:
                self._provider_id = rows[0].id
        else:
            rows = [
                connection
                for connection in self.connections
                if connection.external_connection_id == EXTERNAL_CONNECTION_ID
            ]
        return type("ScalarResult", (), {"first": lambda _self: rows[0] if rows else None})()

    def add(self, record):
        self.pending.append(record)
        if isinstance(record, DataProvider):
            self._provider_id = record.id or uuid.uuid4()
            record.id = self._provider_id
        elif isinstance(record, User):
            record.id = record.id or uuid.uuid4()
            self._connection_user_id = record.id
        elif isinstance(record, FinancialConnection):
            record.id = record.id or uuid.uuid4()
            self.connections.append(record)

    def flush(self):
        for record in self.pending:
            if isinstance(record, DataProvider) and record not in self.providers:
                self.providers.append(record)
            elif isinstance(record, User) and record not in self.users:
                self.users.append(record)
        self.pending.clear()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        self.pending.clear()


class TestSetuSandboxSetup(unittest.TestCase):
    def test_dry_run_counts_only_writes_nothing(self):
        session = FakeSession()

        counts = setup_sandbox_connection(session, apply=False)

        self.assertEqual(
            counts,
            {
                "providers_created": 1,
                "users_created": 1,
                "connections_created": 1,
            },
        )
        self.assertEqual(session.providers, [])
        self.assertEqual(session.users, [])
        self.assertEqual(session.connections, [])
        self.assertGreaterEqual(session.database_checks, 1)
        self.assertEqual(session.commits, 0)

    def test_apply_inserts_provider_new_user_and_pending_connection_once(self):
        session = FakeSession()

        counts = setup_sandbox_connection(session, apply=True)

        self.assertEqual(counts["providers_created"], 1)
        self.assertEqual(counts["users_created"], 1)
        self.assertEqual(counts["connections_created"], 1)
        self.assertEqual(len(session.providers), 1)
        self.assertEqual(len(session.users), 1)
        self.assertEqual(len(session.connections), 1)
        self.assertEqual(session.providers[0].provider_code, PROVIDER_CODE)
        self.assertEqual(session.providers[0].provider_name, PROVIDER_NAME)
        self.assertEqual(session.providers[0].provider_type, PROVIDER_TYPE)
        self.assertEqual(
            session.connections[0].external_connection_id,
            EXTERNAL_CONNECTION_ID,
        )
        self.assertEqual(session.connections[0].status, "pending")
        self.assertEqual(session.commits, 1)
        self.assertGreaterEqual(session.database_checks, 3)

    def test_wrong_database_fails_before_write(self):
        session = FakeSession(database_name="BFAMS-DeFI")

        with self.assertRaises(RuntimeError):
            setup_sandbox_connection(session, apply=True)

        self.assertEqual(session.providers, [])
        self.assertEqual(session.users, [])
        self.assertEqual(session.connections, [])
        self.assertEqual(session.commits, 0)

    def test_existing_provider_and_connection_are_not_reinserted(self):
        session = FakeSession()
        provider = DataProvider(
            id=uuid.uuid4(),
            provider_code=PROVIDER_CODE,
            provider_name=PROVIDER_NAME,
            provider_type=PROVIDER_TYPE,
        )
        user = User(id=uuid.uuid4())
        connection = FinancialConnection(
            id=uuid.uuid4(),
            user_id=user.id,
            provider_id=provider.id,
            external_connection_id=EXTERNAL_CONNECTION_ID,
            status="pending",
        )
        session.providers.append(provider)
        session.users.append(user)
        session.connections.append(connection)
        session._provider_id = provider.id
        session._connection_user_id = user.id

        counts = setup_sandbox_connection(session, apply=True)

        self.assertEqual(
            counts,
            {"providers_created": 0, "users_created": 0, "connections_created": 0},
        )
        self.assertEqual(len(session.providers), 1)
        self.assertEqual(len(session.users), 1)
        self.assertEqual(len(session.connections), 1)
        self.assertEqual(session.commits, 1)


if __name__ == "__main__":
    unittest.main()
