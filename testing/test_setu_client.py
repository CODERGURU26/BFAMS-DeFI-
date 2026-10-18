import unittest

import httpx
from pydantic import SecretStr

from app.integrations.setu.client import SetuClient, SETU_SANDBOX_BASE_URL
from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import (
    SetuAPIError,
    SetuConfigurationError,
    SetuMalformedResponseError,
    SetuTimeoutError,
    SetuTransportError,
)


def configured_settings(**overrides):
    values = {
        "client_id": SecretStr("test-client-id"),
        "client_secret": SecretStr("test-client-secret"),
        "product_instance_id": SecretStr("test-product-id"),
        "environment": "sandbox",
    }
    values.update(overrides)
    return SetuSettings(**values)


class TestSetuClient(unittest.TestCase):
    def test_initializes_with_sandbox_headers_and_parses_json(self):
        def handler(request):
            self.assertEqual(str(request.url), f"{SETU_SANDBOX_BASE_URL}/health")
            self.assertEqual(request.headers["x-client-id"], "test-client-id")
            self.assertEqual(request.headers["x-client-secret"], "test-client-secret")
            self.assertEqual(
                request.headers["x-product-instance-id"], "test-product-id"
            )
            self.assertEqual(request.headers["content-type"], "application/json")
            return httpx.Response(200, json={"status": "ok"})

        with SetuClient(
            configured_settings(), transport=httpx.MockTransport(handler)
        ) as client:
            self.assertEqual(client.request("GET", "/health"), {"status": "ok"})

    def test_missing_credentials_are_rejected(self):
        with self.assertRaises(SetuConfigurationError):
            SetuClient(SetuSettings(_env_file=None), transport=httpx.MockTransport(lambda _: None))

    def test_non_sandbox_environment_is_rejected(self):
        with self.assertRaises(SetuConfigurationError):
            SetuClient(configured_settings(environment="production"))

    def test_non_success_status_is_sanitized(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(401, text="private"))
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuAPIError) as raised:
                client.request("GET", "/health")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("private", str(raised.exception))

    def test_timeout_is_controlled(self):
        def handler(_):
            raise httpx.ReadTimeout("private timeout detail")

        with SetuClient(
            configured_settings(), transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SetuTimeoutError) as raised:
                client.request("GET", "/health")

        self.assertNotIn("private timeout detail", str(raised.exception))

    def test_transport_error_is_controlled(self):
        def handler(_):
            raise httpx.ConnectError("private connection detail")

        with SetuClient(
            configured_settings(), transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SetuTransportError) as raised:
                client.request("GET", "/health")

        self.assertNotIn("private connection detail", str(raised.exception))

    def test_malformed_json_is_rejected(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, text="<bad>"))
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuMalformedResponseError):
                client.request("GET", "/health")

    def test_non_object_json_is_rejected(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuMalformedResponseError):
                client.request("GET", "/health")

    def test_rejects_absolute_url_as_path(self):
        with SetuClient(
            configured_settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        ) as client:
            with self.assertRaises(SetuConfigurationError):
                client.request("GET", "https://other.example/path")
