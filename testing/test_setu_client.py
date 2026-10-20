import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import httpx
from pydantic import SecretStr

from app.integrations.setu.client import (
    SETU_SANDBOX_BASE_URL,
    SETU_TOKEN_URL,
    SetuClient,
    safe_configuration_diagnostics,
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
    def test_safe_configuration_diagnostics_never_include_secret_values(self):
        settings = configured_settings(vua="private-vua@setu")

        diagnostics = safe_configuration_diagnostics(settings)

        self.assertTrue(diagnostics["sandbox_environment"])
        self.assertEqual(
            diagnostics["expected_sandbox_base_url"],
            "https://fiu-sandbox.setu.co",
        )
        self.assertTrue(diagnostics["sandbox_base_url_is_expected"])
        self.assertEqual(diagnostics["create_consent_method"], "POST")
        self.assertEqual(diagnostics["create_consent_path"], "/consents")
        self.assertEqual(
            diagnostics["create_consent_header_names"],
            (
                "x-client_id",
                "x-client-secret",
                "x-product-instance-id",
                "Content-Type",
            ),
        )
        self.assertEqual(
            diagnostics["settings_present_nonblank"],
            {
                "SETU_CLIENT_ID": True,
                "SETU_CLIENT_SECRET": True,
                "SETU_PRODUCT_INSTANCE_ID": True,
                "SETU_VUA": True,
            },
        )
        serialized = repr(diagnostics)
        for secret in (
            "test-client-id",
            "test-client-secret",
            "test-product-id",
            "private-vua@setu",
        ):
            self.assertNotIn(secret, serialized)

    def test_create_consent_uses_aa_sandbox_credential_headers(self):
        requests = []
        captured_output = StringIO()

        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url),
                f"{SETU_SANDBOX_BASE_URL}/consents",
            )
            self.assertEqual(request.headers["x-client_id"], "test-client-id")
            self.assertEqual(
                request.headers["x-client-secret"],
                "test-client-secret",
            )
            self.assertEqual(
                request.headers["x-product-instance-id"],
                "test-product-id",
            )
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("x-client-id", request.headers)
            self.assertEqual(request.headers["content-type"], "application/json")
            return httpx.Response(200, json={"id": "consent-id"})

        with SetuClient(
            configured_settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            with redirect_stdout(captured_output), redirect_stderr(captured_output):
                self.assertEqual(
                    client.request(
                        "POST",
                        "/consents",
                        json={"vua": "private-vua@setu"},
                    ),
                    {"id": "consent-id"},
                )

        self.assertEqual(len(requests), 1)
        for secret in (
            "test-client-id",
            "test-client-secret",
            "test-product-id",
            "private-vua@setu",
        ):
            self.assertNotIn(secret, captured_output.getvalue())

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

    def test_blank_credentials_are_rejected_without_network_request(self):
        requests = []
        settings = configured_settings(
            client_id="  ",
            client_secret="",
            product_instance_id="\t",
        )

        with self.assertRaises(SetuConfigurationError):
            SetuClient(
                settings,
                transport=httpx.MockTransport(
                    lambda request: requests.append(request)
                ),
            )

        self.assertEqual(requests, [])

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

    def test_401_diagnostics_keep_safe_code_and_request_id_only(self):
        sensitive_values = (
            "test-client-secret",
            "issued-access-token",
            "private-vua@setu",
        )
        transport = mock_transport(
            lambda _: httpx.Response(
                401,
                json={
                    "errorCode": "InvalidRequest",
                    "errorMsg": "private-vua@setu test-client-secret",
                    "txnid": "request-123",
                    "details": "issued-access-token",
                },
            )
        )
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuAPIError) as raised:
                client.request("POST", "/consents", json={})

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.error_code, "InvalidRequest")
        self.assertIsNone(raised.exception.message)
        self.assertEqual(raised.exception.request_id, "request-123")
        for value in sensitive_values:
            self.assertNotIn(value, str(raised.exception))

    def test_401_diagnostics_allow_only_known_generic_provider_message(self):
        transport = mock_transport(
            lambda _: httpx.Response(
                401,
                json={
                    "errorCode": "InvalidRequest",
                    "errorMsg": "Invalid request body",
                    "traceId": "trace-456",
                },
            )
        )
        with SetuClient(configured_settings(), transport=transport) as client:
            with self.assertRaises(SetuAPIError) as raised:
                client.request("POST", "/consents", json={})

        self.assertEqual(raised.exception.message, "Invalid request body")
        self.assertEqual(raised.exception.request_id, "trace-456")

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
