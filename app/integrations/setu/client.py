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
            base_url=SETU_SANDBOX_BASE_URL,
            headers={
                "Content-Type": "application/json",
                "x-client-id": settings.client_id.get_secret_value(),
                "x-client-secret": settings.client_secret.get_secret_value(),
                "x-product-instance-id": settings.product_instance_id.get_secret_value(),
            },
            timeout=timeout,
            transport=transport,
        )

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
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise SetuConfigurationError("Setu API path must be an absolute API path.")

        try:
            response = self._client.request(
                method,
                path,
                json=json,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise SetuTimeoutError("Setu API request timed out.") from exc
        except httpx.RequestError as exc:
            raise SetuTransportError("Setu API request could not be completed.") from exc

        if not response.is_success:
            raise SetuAPIError(response.status_code)

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
