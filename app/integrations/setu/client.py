"""HTTP client for the Setu Account Aggregator sandbox."""

from collections.abc import Mapping
from typing import Any

import httpx

from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import (
    SetuAPIError,
    SetuConfigurationError,
    SetuMalformedResponseError,
    SetuTimeoutError,
    SetuTransportError,
)

SETU_SANDBOX_BASE_URL = "https://fiu-sandbox.setu.co"
SETU_TOKEN_URL = "https://orgservice-prod.setu.co/v1/users/login"
SETU_TOKEN_CLIENT_HEADER = "bridge"
DEFAULT_TIMEOUT_SECONDS = 10.0


def safe_configuration_diagnostics(settings: SetuSettings) -> dict[str, object]:
    configured = {
        "SETU_CLIENT_ID": settings.client_id,
        "SETU_CLIENT_SECRET": settings.client_secret,
        "SETU_PRODUCT_INSTANCE_ID": settings.product_instance_id,
        "SETU_VUA": settings.vua,
    }
    return {
        "sandbox_environment": settings.environment == "sandbox",
        "settings_present_nonblank": {
            name: value is not None and bool(value.get_secret_value().strip())
            for name, value in configured.items()
        },
        "expected_sandbox_base_url": SETU_SANDBOX_BASE_URL,
        "sandbox_base_url_is_expected": (
            SETU_SANDBOX_BASE_URL == "https://fiu-sandbox.setu.co"
        ),
        "create_consent_method": "POST",
        "create_consent_path": "/consents",
        "create_consent_header_names": (
            "x-client_id",
            "x-client-secret",
            "x-product-instance-id",
            "Content-Type",
        ),
        "v2_bearer_header_names": (
            "Authorization",
            "x-product-instance-id",
            "Content-Type",
        ),
    }


class SetuClient:
    def __init__(
        self,
        settings: SetuSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if settings.environment != "sandbox":
            raise SetuConfigurationError(
                "Setu API client is restricted to the sandbox environment."
            )
        if not settings.has_credentials:
            raise SetuConfigurationError("Setu sandbox credentials are not configured.")
        if timeout <= 0:
            raise SetuConfigurationError("Setu request timeout must be positive.")

        self._client = httpx.Client(
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._client_id = settings.client_id.get_secret_value()
        self._client_secret = settings.client_secret.get_secret_value()
        self._product_instance_id = settings.product_instance_id.get_secret_value()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SetuClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise SetuConfigurationError("Setu API path must be an absolute API path.")

        if method.upper() == "GET" and path == "/v2/fips":
            headers = {
                "x-client-id": self._client_id,
                "x-client-secret": self._client_secret,
            }
        elif method.upper() == "POST" and path == "/consents":
            headers = {
                "x-client_id": self._client_id,
                "x-client-secret": self._client_secret,
                "x-product-instance-id": self._product_instance_id,
            }
        else:
            headers = {
                "Authorization": f"Bearer {self._get_access_token()}",
                "x-product-instance-id": self._product_instance_id,
            }

        return self._send_request(
            method,
            f"{SETU_SANDBOX_BASE_URL}{path}",
            headers=headers,
            json=json,
            params=params,
            allow_empty=allow_empty,
        )

    def _get_access_token(self) -> str:
        payload = self._send_request(
            "POST",
            SETU_TOKEN_URL,
            headers={"client": SETU_TOKEN_CLIENT_HEADER},
            json={
                "clientID": self._client_id,
                "grant_type": "client_credentials",
                "secret": self._client_secret,
            },
        )
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise SetuMalformedResponseError(
                "Setu token response is missing an access token."
            )
        return access_token.strip()

    def _send_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                url,
                headers=headers,
                json=json,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise SetuTimeoutError("Setu API request timed out.") from exc
        except httpx.RequestError as exc:
            raise SetuTransportError("Setu API request could not be completed.") from exc

        if not response.is_success:
            error_payload: dict[str, Any] = {}
            try:
                parsed_error = response.json()
                if isinstance(parsed_error, dict):
                    error_payload = parsed_error
            except ValueError:
                pass

            request_id = next(
                (
                    response.headers.get(header)
                    for header in (
                        "x-request-id",
                        "request-id",
                        "x-correlation-id",
                        "x-amzn-trace-id",
                    )
                    if response.headers.get(header)
                ),
                None,
            )
            if request_id is None:
                request_id = error_payload.get("txnid") or error_payload.get(
                    "traceId"
                )
            safe_error_code = error_payload.get("errorCode")
            if not isinstance(safe_error_code, str):
                safe_error_code = None
            safe_request_id = request_id if isinstance(request_id, str) else None
            safe_message = error_payload.get("errorMsg")
            if not isinstance(safe_message, str):
                safe_message = None
            raise SetuAPIError(
                response.status_code,
                error_code=safe_error_code,
                message=safe_message,
                request_id=safe_request_id,
            )

        if not response.content and allow_empty:
            return {}

        try:
            payload = response.json()
        except ValueError as exc:
            raise SetuMalformedResponseError(
                "Setu API returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise SetuMalformedResponseError(
                "Setu API response must be a JSON object."
            )
        return payload
