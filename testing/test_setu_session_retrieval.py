import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.integrations.setu.client import SETU_TOKEN_URL, SetuClient
from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import (
    SetuAPIError,
    SetuDataSessionPollingTimeoutError,
    SetuDataSessionTerminalError,
)
from app.integrations.setu.service import poll_data_session, retrieve_data_session
from app.routes.setu import get_data_session


def configured_settings(**overrides):
    values = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "product_instance_id": "test-product-id",
        "environment": "sandbox",
        "session_poll_interval_seconds": 0.5,
        "session_poll_max_attempts": 4,
        "session_poll_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return SetuSettings(**values)


def session_response(status, *, session_id="session-123"):
    return {
        "id": session_id,
        "consentId": "consent-456",
        "status": status,
        "format": "json",
        "fips": [
            {
                "accounts": [
                    {
                        "status": "DELIVERED",
                        "data": {
                            "account": {
                                "summary": {"currentBalance": "123.45"},
                                "transactions": {
                                    "transaction": [{"amount": "67.89"}]
                                },
                            }
                        },
                    }
                ]
            }
        ],
    }


class TestSetuSessionRetrieval(unittest.TestCase):
    def test_retrieves_raw_pending_session_without_interpreting_fi_data(self):
        response = session_response("PENDING")
        client = MagicMock()
        client.request.return_value = response

        result = retrieve_data_session(client, "session-123")

        self.assertIs(result, response)
        self.assertEqual(result["fips"][0]["accounts"][0]["data"]["account"]["summary"]["currentBalance"], "123.45")
        client.request.assert_called_once_with("GET", "/v2/sessions/session-123")

    def test_polling_waits_while_pending_then_returns_partial_unchanged(self):
        pending = session_response("PENDING")
        partial = session_response("PARTIAL")
        client = MagicMock()
        client.request.side_effect = [pending, partial]
        sleeper = MagicMock()

        result = poll_data_session(
            client,
            "session-123",
            interval_seconds=0.25,
            max_attempts=3,
            timeout_seconds=5,
            _sleep=sleeper,
            _monotonic=MagicMock(return_value=0),
        )

        self.assertIs(result, partial)
        self.assertEqual(result["fips"], partial["fips"])
        self.assertEqual(client.request.call_count, 2)
        sleeper.assert_called_once_with(0.25)

    def test_polling_returns_completed_session(self):
        completed = session_response("COMPLETED")
        client = MagicMock()
        client.request.return_value = completed

        result = poll_data_session(client, "session-123", _sleep=MagicMock())

        self.assertIs(result, completed)
        self.assertEqual(client.request.call_count, 1)

    def test_expired_and_failed_are_terminal(self):
        for status in ("EXPIRED", "FAILED"):
            with self.subTest(status=status):
                client = MagicMock()
                client.request.return_value = session_response(status)

                with self.assertRaises(SetuDataSessionTerminalError) as raised:
                    poll_data_session(client, "session-123", _sleep=MagicMock())

                self.assertEqual(raised.exception.status, status)
                client.request.assert_called_once()

    def test_pending_session_stops_at_max_attempts(self):
        client = MagicMock()
        client.request.return_value = session_response("PENDING")
        sleeper = MagicMock()

        with self.assertRaises(SetuDataSessionPollingTimeoutError):
            poll_data_session(
                client,
                "session-123",
                interval_seconds=0.1,
                max_attempts=3,
                timeout_seconds=10,
                _sleep=sleeper,
                _monotonic=MagicMock(return_value=0),
            )

        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_pending_session_stops_at_time_limit(self):
        client = MagicMock()
        client.request.return_value = session_response("PENDING")
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(duration):
            clock[0] += duration

        with self.assertRaises(SetuDataSessionPollingTimeoutError):
            poll_data_session(
                client,
                "session-123",
                interval_seconds=1,
                max_attempts=10,
                timeout_seconds=1.5,
                _sleep=sleep,
                _monotonic=monotonic,
            )

        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(clock[0], 1.5)

    def test_rejects_non_positive_polling_bounds(self):
        client = MagicMock()
        for options in (
            {"interval_seconds": 0},
            {"max_attempts": 0},
            {"timeout_seconds": 0},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    poll_data_session(client, "session-123", **options)

        client.request.assert_not_called()

    def test_authenticated_http_401_failure_is_sanitized(self):
        def handler(request):
            if str(request.url) == SETU_TOKEN_URL:
                return httpx.Response(200, json={"access_token": "mock-token"})
            self.assertEqual(request.headers["authorization"], "Bearer mock-token")
            self.assertEqual(request.headers["x-product-instance-id"], "test-product-id")
            return httpx.Response(401, text="private authorization details")

        with SetuClient(
            configured_settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(SetuAPIError) as raised:
                retrieve_data_session(client, "session-123")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("private authorization details", str(raised.exception))

    def test_http_client_and_server_failures_are_reported(self):
        for status_code in (400, 500):
            with self.subTest(status_code=status_code):

                def handler(request):
                    if str(request.url) == SETU_TOKEN_URL:
                        return httpx.Response(200, json={"access_token": "mock-token"})
                    return httpx.Response(status_code, text="private response")

                with SetuClient(
                    configured_settings(),
                    transport=httpx.MockTransport(handler),
                ) as client:
                    with self.assertRaises(SetuAPIError) as raised:
                        retrieve_data_session(client, "session-123")

                self.assertEqual(raised.exception.status_code, status_code)
                self.assertNotIn("private response", str(raised.exception))

    def test_route_returns_raw_response_by_default(self):
        response = session_response("COMPLETED")
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = response
        settings = configured_settings()

        with (
            patch("app.routes.setu.SetuSettings", return_value=settings),
            patch("app.routes.setu.SetuClient", return_value=client),
        ):
            result = get_data_session("session-123")

        self.assertIs(result, response)
        client.request.assert_called_once_with("GET", "/v2/sessions/session-123")

    def test_route_polling_uses_configured_bounds(self):
        settings = configured_settings(
            session_poll_interval_seconds=0.75,
            session_poll_max_attempts=6,
            session_poll_timeout_seconds=8,
        )
        client = MagicMock()
        client.__enter__.return_value = client
        expected = session_response("PARTIAL")
        client.request.return_value = expected

        with (
            patch("app.routes.setu.SetuSettings", return_value=settings),
            patch("app.routes.setu.SetuClient", return_value=client),
            patch("app.routes.setu.poll_data_session", return_value=expected) as poll,
        ):
            result = get_data_session("session-123", poll=True)

        self.assertIs(result, expected)
        poll.assert_called_once_with(
            client,
            "session-123",
            interval_seconds=0.75,
            max_attempts=6,
            timeout_seconds=8,
        )


if __name__ == "__main__":
    unittest.main()
