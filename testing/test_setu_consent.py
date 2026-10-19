import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.integrations.setu.exceptions import SetuMalformedResponseError
from app.integrations.setu.service import (
    ConsentResponse,
    CreateConsentRequest,
    CreateDataSessionRequest,
    DataSessionResponse,
    create_data_session,
    fetch_data_session_summary,
    get_consent_status as service_get_consent_status,
    get_fi_fetch_status,
    request_fi_data_fetch,
    refresh_consent_status,
)
from app.models import ConsentRecord
from app.routes.setu import (
    fetch_data_session,
    get_consent_status,
    request_consent,
    request_data_session,
    request_fi_fetch,
    sync_fi_data,
)


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
        self.record = ConsentRecord(
            id=uuid4(),
            connection_id=self.connection_id,
            external_consent_id="consent-123",
            status="pending",
        )
        self.connection_row = SimpleNamespace(
            id=self.connection_id,
            user_id=uuid4(),
            external_connection_id="SETU-SANDBOX-1",
        )

    def scalar(self, _statement):
        if "current_database" not in str(_statement).lower():
            return 1
        return self.database_name

    def execute(self, statement):
        if not self.connection:
            return SimpleNamespace(first=lambda: None, all=lambda: [])
        if statement is not None and "financial_connections.external_connection_id" in str(
            statement.whereclause
        ):
            return SimpleNamespace(
                first=lambda: (self.connection_row, "setu_sandbox"),
                all=lambda: [(self.connection_row, "setu_sandbox")],
            )
        return SimpleNamespace(
            first=lambda: (
                self.connection_row,
                "setu_sandbox",
            ),
            all=lambda: [(self.record, self.connection_row, "setu_sandbox")],
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

        self.assertIsInstance(result, ConsentResponse)
        self.assertEqual(result.id, "consent-123")
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.url, "https://consent-sandbox.setu.co/approve/123")
        self.assertEqual(session.commit_count, 0)
        self.assertEqual(session.added, [])
        sent_payload = client.request.call_args.kwargs["json"]
        self.assertEqual(sent_payload["consentDuration"], {"unit": "MONTH", "value": 1})
        self.assertEqual(sent_payload["vua"], "1234567890@onemoney")
        self.assertEqual(client.request.call_args.args, ("POST", "/v2/consents"))

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

    def test_consent_creation_does_not_persist_provider_response(self):
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

        self.assertEqual(session.commit_count, 0)
        self.assertEqual(session.added, [])
        self.assertEqual(result.id, "consent-123")

    def test_polls_consent_status_without_database_storage(self):
        client = mock_setu_client(
            {
                "id": "consent-123",
                "status": "ACTIVE",
                "url": "https://consent-sandbox.setu.co/approve/123",
            }
        )
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
        ):
            result = get_consent_status("consent-123")

        self.assertEqual(result.id, "consent-123")
        self.assertEqual(result.status, "ACTIVE")
        self.assertEqual(result.url, "https://consent-sandbox.setu.co/approve/123")
        client.request.assert_called_once_with("GET", "/v2/consents/consent-123")

    def test_service_status_polling_accepts_response_without_approval_url(self):
        client = mock_setu_client({"id": "consent-123", "status": "REJECTED"})

        result = service_get_consent_status(client, "consent-123")

        self.assertEqual(result.id, "consent-123")
        self.assertEqual(result.status, "REJECTED")
        self.assertIsNone(result.url)

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

    def test_active_provider_consent_maps_to_approved_and_persists_expiry(self):
        session = FakeSession()
        record = ConsentRecord(
            id=uuid4(),
            connection_id=session.connection_id,
            external_consent_id="consent-123",
            status="pending",
        )
        client = mock_setu_client(
            {
                "id": "consent-123",
                "status": "ACTIVE",
                "detail": {"consentExpiry": "2026-11-18T06:22:17Z"},
            }
        )

        result = refresh_consent_status(session, client, record)

        self.assertEqual(result["status"], "approved")
        self.assertEqual(record.status, "approved")
        self.assertEqual(
            record.expires_at.isoformat(),
            "2026-11-18T06:22:17+00:00",
        )
        self.assertEqual(session.commit_count, 1)
        self.assertEqual(
            client.request.call_args.args,
            ("GET", "/v2/consents/consent-123"),
        )

    def test_pending_status_does_not_commit_when_unchanged(self):
        session = FakeSession()
        record = ConsentRecord(
            id=uuid4(),
            connection_id=session.connection_id,
            external_consent_id="consent-123",
            status="pending",
        )
        client = mock_setu_client(
            {"id": "consent-123", "status": "PENDING"}
        )

        result = refresh_consent_status(session, client, record)

        self.assertEqual(result["status"], "pending")
        self.assertEqual(session.commit_count, 0)

    def test_rejects_unknown_or_mismatched_provider_status_without_write(self):
        for response in (
            {"id": "consent-123", "status": "FAILED"},
            {"id": "different-consent", "status": "ACTIVE"},
        ):
            with self.subTest(response=response):
                session = FakeSession()
                record = ConsentRecord(
                    id=uuid4(),
                    connection_id=session.connection_id,
                    external_consent_id="consent-123",
                    status="pending",
                )
                client = mock_setu_client(response)

                with self.assertRaises(SetuMalformedResponseError):
                    refresh_consent_status(session, client, record)

                self.assertEqual(session.commit_count, 0)

    def test_rejects_naive_expiry_without_write(self):
        session = FakeSession()
        record = ConsentRecord(
            id=uuid4(),
            connection_id=session.connection_id,
            external_consent_id="consent-123",
            status="pending",
        )
        client = mock_setu_client(
            {
                "id": "consent-123",
                "status": "ACTIVE",
                "detail": {"consentExpiry": "2026-11-18T06:22:17"},
            }
        )

        with self.assertRaises(SetuMalformedResponseError):
            refresh_consent_status(session, client, record)

        self.assertEqual(session.commit_count, 0)

    def test_creates_data_session_from_approved_consent(self):
        consent = ConsentResponse(
            id="consent-123",
            status="ACTIVE",
        )
        request = CreateDataSessionRequest.model_validate(
            {
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "json",
            }
        )
        client = mock_setu_client(
            {
                "id": "session-456",
                "consentId": "consent-123",
                "format": "json",
                "status": "PENDING",
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "fips": None,
                "traceId": "trace-789",
            }
        )

        result = create_data_session(client, consent, request)

        self.assertIsInstance(result, DataSessionResponse)
        self.assertEqual(result.id, "session-456")
        self.assertEqual(result.consentId, "consent-123")
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.format, "json")
        self.assertEqual(client.request.call_args.args, ("POST", "/v2/sessions"))
        self.assertEqual(
            client.request.call_args.kwargs["json"],
            {
                "consentId": "consent-123",
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "json",
            },
        )

    def test_does_not_create_session_for_unapproved_consent(self):
        consent = ConsentResponse(id="consent-123", status="PENDING")
        request = CreateDataSessionRequest.model_validate(
            {
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "json",
            }
        )
        client = mock_setu_client({})

        with self.assertRaises(ValueError):
            create_data_session(client, consent, request)

        client.request.assert_not_called()

    def test_data_session_request_requires_explicit_format(self):
        with self.assertRaises(ValueError):
            CreateDataSessionRequest.model_validate(
                {
                    "dataRange": {
                        "from": "2026-01-01T00:00:00Z",
                        "to": "2026-02-01T00:00:00Z",
                    }
                }
            )

    def test_creates_data_session_with_xml_format(self):
        consent = ConsentResponse(id="consent-123", status="ACTIVE")
        request = CreateDataSessionRequest.model_validate(
            {
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "xml",
            }
        )
        client = mock_setu_client(
            {
                "id": "session-456",
                "consentId": "consent-123",
                "format": "xml",
                "status": "PENDING",
            }
        )

        result = create_data_session(client, consent, request)

        self.assertEqual(result.format, "xml")
        self.assertEqual(client.request.call_args.kwargs["json"]["format"], "xml")

    def test_rejects_data_session_response_for_different_request(self):
        consent = ConsentResponse(id="consent-123", status="ACTIVE")
        request = CreateDataSessionRequest.model_validate(
            {
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "json",
            }
        )
        client = mock_setu_client(
            {
                "id": "session-456",
                "consentId": "different-consent",
                "format": "json",
                "status": "PENDING",
            }
        )

        with self.assertRaises(SetuMalformedResponseError):
            create_data_session(client, consent, request)

    def test_session_route_refreshes_approval_before_create(self):
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {
                "id": "session-456",
                "consentId": "consent-123",
                "format": "json",
                "status": "PENDING",
            },
        ]
        request = CreateDataSessionRequest.model_validate(
            {
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "json",
            }
        )
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
        ):
            result = request_data_session("consent-123", request)

        self.assertEqual(result.id, "session-456")
        self.assertEqual(result.format, "json")
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(
            client.request.call_args_list[0].args,
            ("GET", "/v2/consents/consent-123"),
        )
        self.assertEqual(
            client.request.call_args_list[1].args,
            ("POST", "/v2/sessions"),
        )
        self.assertEqual(
            client.request.call_args_list[1].kwargs["json"]["format"],
            "json",
        )

    def test_session_route_does_not_create_session_until_consent_active(self):
        client = mock_setu_client(
            {"id": "consent-123", "status": "PENDING"}
        )
        request = CreateDataSessionRequest.model_validate(
            {
                "dataRange": {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-02-01T00:00:00Z",
                },
                "format": "json",
            }
        )
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(HTTPException) as raised,
        ):
            request_data_session("consent-123", request)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(client.request.call_count, 1)

    def test_fetch_summarizes_ready_response_without_returning_payload(self):
        record = ConsentRecord(
            id=uuid4(),
            connection_id=uuid4(),
            external_consent_id="consent-123",
            status="approved",
        )
        client = mock_setu_client(
            {
                "id": "session-456",
                "consentId": "consent-123",
                "status": "PARTIAL",
                "format": "json",
                "fips": [
                    {
                        "accounts": [
                            {
                                "status": "DELIVERED",
                                "data": {
                                    "account": {
                                        "linkedAccRef": "synthetic-account",
                                        "type": "deposit",
                                        "summary": {"currency": "INR"},
                                        "transactions": {
                                            "transaction": [
                                                {
                                                    "txnId": "synthetic-id",
                                                    "amount": "25.50",
                                                    "type": "CREDIT",
                                                    "transactionTimestamp": "2026-01-01T00:00:00Z",
                                                }
                                            ]
                                        },
                                    }
                                },
                            },
                            {"status": "PENDING", "data": None},
                        ]
                    }
                ],
            }
        )

        result = fetch_data_session_summary(client, record, "session-456")

        self.assertEqual(
            result,
            {
                "status": "partial",
                "fi_provider_count": 1,
                "account_count": 1,
                "transaction_count": 1,
                "rejected_count": 0,
            },
        )
        self.assertNotIn("synthetic-id", str(result))
        self.assertEqual(
            client.request.call_args.args,
            ("GET", "/v2/sessions/session-456"),
        )

    def test_fetch_rejects_not_ready_and_mismatched_session(self):
        record = ConsentRecord(
            id=uuid4(),
            connection_id=uuid4(),
            external_consent_id="consent-123",
            status="approved",
        )
        client = mock_setu_client(
            {
                "id": "session-456",
                "consentId": "consent-123",
                "status": "PENDING",
                "format": "json",
            }
        )
        with self.assertRaises(ValueError):
            fetch_data_session_summary(client, record, "session-456")

        for response in (
            {
                "id": "different-session",
                "consentId": "consent-123",
                "status": "COMPLETED",
                "format": "json",
                "fips": [],
            },
            {
                "id": "session-456",
                "consentId": "different-consent",
                "status": "COMPLETED",
                "format": "json",
                "fips": [],
            },
        ):
            with self.subTest(response_id=response["id"]):
                client = mock_setu_client(response)
                with self.assertRaises(SetuMalformedResponseError):
                    fetch_data_session_summary(client, record, "session-456")

    def test_fetch_route_requires_fresh_active_consent(self):
        session = FakeSession()
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {"status": "PENDING", "consentId": "consent-123"},
        ]
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(HTTPException) as raised,
        ):
            fetch_data_session("consent-123", "session-456", session)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(
            client.request.call_args_list[1].args,
            ("GET", "/v2/consents/consent-123/fetch/status"),
        )

    def test_fetch_route_returns_only_counts_after_active_consent(self):
        session = FakeSession()
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {"status": "COMPLETED", "consentId": "consent-123"},
            {
                "id": "session-456",
                "consentId": "consent-123",
                "status": "COMPLETED",
                "format": "json",
                "fips": [
                    {
                        "accounts": [
                            {
                                "status": "DELIVERED",
                                "data": {
                                    "account": {
                                        "linkedAccRef": "synthetic-account",
                                        "type": "deposit",
                                        "transactions": {"transaction": []},
                                    }
                                },
                            }
                        ]
                    }
                ],
            },
        ]
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
        ):
            result = fetch_data_session("consent-123", "session-456", session)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["account_count"], 1)
        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(
            client.request.call_args_list[2].args,
            ("GET", "/v2/sessions/session-456"),
        )

    def test_explicit_fetch_trigger_calls_post_without_exposing_payload(self):
        session = FakeSession()
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {},
        ]

        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
        ):
            result = request_fi_fetch("consent-123", "session-456", session)

        self.assertEqual(result, {"status": "fetch_requested"})
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(
            client.request.call_args_list[1].args,
            ("POST", "/v2/sessions/session-456"),
        )
        self.assertTrue(client.request.call_args_list[1].kwargs["allow_empty"])

    def test_fetch_trigger_does_not_call_provider_for_pending_consent(self):
        session = FakeSession()
        client = mock_setu_client(
            {"id": "consent-123", "status": "PENDING"}
        )

        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            self.assertRaises(HTTPException) as raised,
        ):
            request_fi_fetch("consent-123", "session-456", session)

        self.assertEqual(raised.exception.status_code, 409)
        client.request.assert_called_once_with("GET", "/v2/consents/consent-123")

    def test_fetch_status_maps_provider_status(self):
        record = ConsentRecord(
            id=uuid4(),
            connection_id=uuid4(),
            external_consent_id="consent-123",
            status="approved",
        )
        client = mock_setu_client(
            {"status": "PARTIAL", "consentId": "consent-123"}
        )

        result = get_fi_fetch_status(client, record)

        self.assertEqual(result, {"status": "partial"})
        self.assertEqual(
            client.request.call_args.args,
            ("GET", "/v2/consents/consent-123/fetch/status"),
        )

    def test_fetch_status_rejects_unknown_status_and_wrong_consent(self):
        record = ConsentRecord(
            id=uuid4(),
            connection_id=uuid4(),
            external_consent_id="consent-123",
            status="approved",
        )
        for response in (
            {"status": "READY"},
            {"status": "COMPLETED", "consentId": "other-consent"},
        ):
            with self.subTest(response=response):
                client = mock_setu_client(response)
                with self.assertRaises(SetuMalformedResponseError):
                    get_fi_fetch_status(client, record)

    def test_sync_route_uses_store_only_after_ready_and_returns_counts(self):
        session = FakeSession()
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {
                "id": "session-456",
                "consentId": "consent-123",
                "status": "COMPLETED",
                "format": "json",
                "fips": [
                    {
                        "accounts": [
                            {
                                "status": "DELIVERED",
                                "data": {
                                    "account": {
                                        "linkedAccRef": "synthetic-account",
                                        "type": "deposit",
                                        "summary": {"currency": "INR"},
                                        "transactions": {
                                            "transaction": [
                                                {
                                                    "txnId": "synthetic-txn",
                                                    "type": "CREDIT",
                                                    "amount": "25",
                                                    "transactionTimestamp": "2026-01-01T00:00:00Z",
                                                }
                                            ]
                                        },
                                    }
                                },
                            }
                        ]
                    }
                ],
            },
        ]
        run = SimpleNamespace(status="completed", records_processed=2)

        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            patch(
                "app.integrations.setu.storage.store_sync",
                return_value=run,
            ) as store,
        ):
            result = sync_fi_data("consent-123", "session-456", session)

        self.assertEqual(
            result,
            {
                "status": "completed",
                "accounts_normalized": 1,
                "transactions_normalized": 1,
                "records_processed": 2,
                "records_rejected": 0,
                "rejection_counts": {},
            },
        )
        self.assertEqual(store.call_args.args[1], session.connection_id)
        self.assertEqual(store.call_args.kwargs["sync_type"], "setu_aa")
        self.assertEqual(len(store.call_args.args[2]), 1)
        self.assertEqual(len(store.call_args.args[3]["synthetic-account"]), 1)

    def test_sync_route_does_not_store_when_fetch_is_not_ready(self):
        session = FakeSession()
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {
                "id": "session-456",
                "consentId": "consent-123",
                "status": "PENDING",
                "format": "json",
                "fips": [],
            },
        ]
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            patch("app.integrations.setu.storage.store_sync") as store,
            self.assertRaises(HTTPException) as raised,
        ):
            sync_fi_data("consent-123", "session-456", session)

        self.assertEqual(raised.exception.status_code, 409)
        store.assert_not_called()

    def test_sync_route_refuses_wrong_database_before_store(self):
        session = FakeSession(database_name="BFAMS-DeFI")
        session.record.status = "approved"
        client = mock_setu_client({})
        client.request.side_effect = [
            {"id": "consent-123", "status": "ACTIVE"},
            {
                "id": "session-456",
                "consentId": "consent-123",
                "status": "COMPLETED",
                "format": "json",
                "fips": [],
            },
        ]
        with (
            patch("app.routes.setu.SetuSettings", return_value=self._settings()),
            patch("app.routes.setu.SetuClient", return_value=client),
            patch("app.integrations.setu.storage.store_sync") as store,
            self.assertRaises(HTTPException) as raised,
        ):
            sync_fi_data("consent-123", "session-456", session)

        self.assertEqual(raised.exception.status_code, 503)
        store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
