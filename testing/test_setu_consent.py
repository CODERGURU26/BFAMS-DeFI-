import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.integrations.setu.service import CreateConsentRequest
from app.routes.setu import request_consent


def request_body(connection_id):
    return CreateConsentRequest.model_validate(
        {
            "connection_id": str(connection_id),
            "vua": "1234567890@onemoney",
            "consentDuration": {"unit": "MONTH", "value": 1},
        }
    )


class FakeSession:
    def __init__(self, *, database_name="BFAMS-DeFI-test", connection=True):
        self.database_name = database_name
        self.connection = connection
        self.connection_id = uuid4()
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0

    def scalar(self, _statement):
        if "current_database" not in str(_statement).lower():
            return 1
        return self.database_name

    def execute(self, _statement):
        if not self.connection:
            return SimpleNamespace(first=lambda: None)
        return SimpleNamespace(
            first=lambda: (
                SimpleNamespace(
                    id=self.connection_id,
                    user_id=uuid4(),
                    external_connection_id="SETU-SANDBOX-1",
                ),
                "setu_sandbox",
            )
        )

    def get(self, _model, _id):
        return object()

    def scalars(self, _statement):
        return SimpleNamespace(first=lambda: None)

    def add(self, record):
        self.added.append(record)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


def mock_setu_client(payload):
    client = MagicMock()
    client.__enter__.return_value = client
    client.request.return_value = payload
    return client


class TestSetuConsent(unittest.TestCase):
    def _settings(self):
        return SimpleNamespace(
            environment="sandbox",
            has_credentials=True,
        )

    def test_creates_pending_consent_for_sandbox_connection(self):
        session = FakeSession()
        client = mock_setu_client(
            {
                "id": "consent-123",
                "url": "https://consent-sandbox.setu.co/approve/123",
                "status": "PENDING",
            }
        )
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
        ):
            result = request_consent(request_body(session.connection_id), session)

        self.assertEqual(result["consent_id"], "consent-123")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(session.commit_count, 1)
        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.added[0].external_consent_id, "consent-123")
        self.assertEqual(session.added[0].status, "pending")
        self.assertFalse(hasattr(session.added[0], "vua"))
        sent_payload = client.request.call_args.kwargs["json"]
        self.assertEqual(sent_payload["consentDuration"], {"unit": "MONTH", "value": 1})
        self.assertEqual(sent_payload["vua"], "1234567890@onemoney")

    def test_rejects_non_sandbox_connection_before_provider_call(self):
        session = FakeSession()
        row = session.execute(None).first()
        connection = row[0]
        connection.external_connection_id = "DHAN-MANUAL-1"
        session.execute = lambda _statement: SimpleNamespace(
            first=lambda: (connection, "dhan_manual")
        )
        client = mock_setu_client({})
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(HTTPException) as raised,
        ):
            request_consent(request_body(session.connection_id), session)

        self.assertEqual(raised.exception.status_code, 404)
        client.request.assert_not_called()

    def test_rejects_missing_connection(self):
        session = FakeSession(connection=False)
        client = mock_setu_client({})
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(HTTPException) as raised,
        ):
            request_consent(request_body(session.connection_id), session)

        self.assertEqual(raised.exception.status_code, 404)
        client.request.assert_not_called()

    def test_rejects_non_pending_provider_response(self):
        session = FakeSession()
        client = mock_setu_client(
            {
                "id": "consent-123",
                "url": "https://consent-sandbox.setu.co/approve/123",
                "status": "ACTIVE",
            }
        )
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(HTTPException) as raised,
        ):
            request_consent(request_body(session.connection_id), session)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(session.commit_count, 0)
        self.assertEqual(session.added, [])

    def test_does_not_write_when_database_is_not_test(self):
        session = FakeSession(database_name="BFAMS-DeFI")
        client = mock_setu_client(
            {
                "id": "consent-123",
                "url": "https://consent-sandbox.setu.co/approve/123",
                "status": "PENDING",
            }
        )
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(RuntimeError),
        ):
            request_consent(request_body(session.connection_id), session)

        self.assertEqual(session.commit_count, 0)
        self.assertEqual(session.rollback_count, 1)

    def test_requires_timezone_aware_data_range(self):
        body = {
            "connection_id": str(uuid4()),
            "vua": "1234567890@onemoney",
            "consentDuration": {"unit": "MONTH", "value": 1},
            "dataRange": {
                "from": "2026-01-01T00:00:00",
                "to": "2026-02-01T00:00:00+00:00",
            },
        }
        with self.assertRaises(ValueError):
            CreateConsentRequest.model_validate(body)


if __name__ == "__main__":
    unittest.main()
