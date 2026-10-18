import unittest

import httpx
from pydantic import SecretStr

from app.integrations.setu.client import (
    SETU_SANDBOX_BASE_URL,
    SETU_TOKEN_URL,
    SetuClient,
)
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


def mock_transport(api_handler, *, token_response=None):
    def handler(request):
        if str(request.url) == SETU_TOKEN_URL:
            if token_response is not None:
                return token_response
            return httpx.Response(200, json={"access_token": "mock-access-token"})
        return api_handler(request)

    return httpx.MockTransport(handler)


class TestSetuClient(unittest.TestCase):
    def test_obtains_bearer_token_then_calls_fiu_api(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), f"{SETU_SANDBOX_BASE_URL}/health")
            self.assertEqual(
                request.headers["authorization"], "Bearer mock-access-token"
            )
            self.assertEqual(
                request.headers["x-product-instance-id"], "test-product-id"
            )
            self.assertNotIn("x-client-id", request.headers)
            self.assertNotIn("x-client-secret", request.headers)
            self.assertEqual(request.headers["content-type"], "application/json")
            return httpx.Response(200, json={"status": "ok"})

        with SetuClient(
            configured_settings(), transport=mock_transport(handler)
        ) as client:
            self.assertEqual(client.request("GET", "/health"), {"status": "ok"})

        self.assertEqual(len(requests), 1)

    def test_token_request_uses_documented_endpoint_headers_and_body(self):
        api_requests = []

        def handler(request):
            api_requests.append(request)
            return httpx.Response(200, json={"status": "ok"})

        token_requests = []
        def transport_handler(request):
            if str(request.url) == SETU_TOKEN_URL:
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.headers["client"], "bridge")
                self.assertEqual(request.headers["content-type"], "application/json")
                self.assertNotIn("x-product-instance-id", request.headers)
                self.assertEqual(
                    request.read(),
                    b'{"clientID":"test-client-id","grant_type":"client_credentials",'
                    b'"secret":"test-client-secret"}',
                )
                token_requests.append(request)
                return httpx.Response(200, json={"access_token": "issued-token"})
            return handler(request)

        with SetuClient(
            configured_settings(),
            transport=httpx.MockTransport(transport_handler),
        ) as client:
            self.assertEqual(client.request("GET", "/health"), {"status": "ok"})

        self.assertEqual(len(token_requests), 1)
        self.assertEqual(len(api_requests), 1)

    def test_fetches_a_token_for_each_fiu_request_without_assuming_ttl(self):
        token_requests = 0
        api_requests = 0

        def handler(request):
            nonlocal token_requests, api_requests
            if str(request.url) == SETU_TOKEN_URL:
                token_requests += 1
                return httpx.Response(
                    200, json={"access_token": f"issued-token-{token_requests}"}
                )
            api_requests += 1
            expected_token = f"Bearer issued-token-{api_requests}"
            self.assertEqual(request.headers["authorization"], expected_token)
            return httpx.Response(200, json={"status": "ok"})

        with SetuClient(
            configured_settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertEqual(client.request("GET", "/one"), {"status": "ok"})
            self.assertEqual(client.request("GET", "/two"), {"status": "ok"})

        self.assertEqual(token_requests, 2)

    def test_active_fip_list_uses_only_route_specific_client_headers(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url), f"{SETU_SANDBOX_BASE_URL}/v2/fips")
            self.assertEqual(request.headers["x-client-id"], "test-client-id")
            self.assertEqual(request.headers["x-client-secret"], "test-client-secret")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("x-product-instance-id", request.headers)
            return httpx.Response(200, json={"fips": []})

        with SetuClient(
            configured_settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertEqual(client.request("GET", "/v2/fips"), {"fips": []})

    def test_missing_credentials_are_rejected(self):
        with self.assertRaises(SetuConfigurationError):
            SetuClient(
                SetuSettings(_env_file=None),
                transport=httpx.MockTransport(lambda _: None),
            )

    def test_non_sandbox_environment_is_rejected(self):
        with self.assertRaises(SetuConfigurationError):
            SetuClient(configured_settings(environment="production"))

    def test_non_success_status_is_sanitized(self):
        transport = mock_transport(
            lambda _: httpx.Response(401, text="private")
        )
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuAPIError) as raised:
                client.request("GET", "/health")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("private", str(raised.exception))

    def test_timeout_is_controlled(self):
        def handler(_):
            raise httpx.ReadTimeout("private timeout detail")

        with SetuClient(
            configured_settings(), transport=mock_transport(handler)
        ) as client:
            with self.assertRaises(SetuTimeoutError) as raised:
                client.request("GET", "/health")

        self.assertNotIn("private timeout detail", str(raised.exception))

    def test_transport_error_is_controlled(self):
        def handler(_):
            raise httpx.ConnectError("private connection detail")

        with SetuClient(
            configured_settings(), transport=mock_transport(handler)
        ) as client:
            with self.assertRaises(SetuTransportError) as raised:
                client.request("GET", "/health")

        self.assertNotIn("private connection detail", str(raised.exception))

    def test_malformed_json_is_rejected(self):
        transport = mock_transport(lambda _: httpx.Response(200, text="<bad>"))
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuMalformedResponseError):
                client.request("GET", "/health")

    def test_non_object_json_is_rejected(self):
        transport = mock_transport(lambda _: httpx.Response(200, json=[]))
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuMalformedResponseError):
                client.request("GET", "/health")

    def test_empty_success_response_is_allowed_only_when_requested(self):
        transport = mock_transport(lambda _: httpx.Response(204))
        with SetuClient(configured_settings(), transport=transport) as client:
            self.assertEqual(
                client.request("POST", "/v2/sessions/id", allow_empty=True),
                {},
            )
            with self.assertRaises(SetuMalformedResponseError):
                client.request("POST", "/v2/sessions/id")

    def test_rejects_absolute_url_as_path(self):
        with SetuClient(
            configured_settings(),
            transport=mock_transport(lambda _: httpx.Response(200, json={})),
        ) as client:
            with self.assertRaises(SetuConfigurationError):
                client.request("GET", "https://other.example/path")

    def test_missing_access_token_is_rejected(self):
        transport = mock_transport(
            lambda _: httpx.Response(200, json={}),
            token_response=httpx.Response(200, json={"token": "wrong-field"}),
        )
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuMalformedResponseError):
                client.request("GET", "/health")
