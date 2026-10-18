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
            raise SetuAPIError(response.status_code)

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
