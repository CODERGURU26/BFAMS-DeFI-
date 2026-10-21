"""Restricted Dhan client for reading account holdings."""

from typing import Any

import httpx

from app.integrations.dhan.settings import dhan_credentials


DHAN_HOLDINGS_URL = "https://api.dhan.co/v2/holdings"
DEFAULT_TIMEOUT_SECONDS = 30.0


class DhanConfigurationError(RuntimeError):
    """Raised when the required Dhan credentials are not configured."""


class DhanAPIError(RuntimeError):
    """Raised for sanitized Dhan API and response failures."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(
            f"Dhan holdings request failed (HTTP {status_code})."
            if status_code is not None
            else "Dhan holdings request returned an invalid response."
        )


class DhanClient:
    """Client limited to one GET request for Dhan holdings."""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not client_id.strip() or not access_token.strip():
            raise DhanConfigurationError(
                "Dhan client credentials are not configured."
            )
        if timeout <= 0:
            raise DhanConfigurationError("Dhan request timeout must be positive.")

        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self._headers = {
            "client-id": client_id,
            "access-token": access_token,
            "Accept": "application/json",
        }

    @classmethod
    def from_environment(
        cls,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> "DhanClient":
        client_id, access_token = dhan_credentials()
        return cls(client_id, access_token, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DhanClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def get_holdings(self) -> list[dict[str, Any]]:
        try:
            response = self._client.get(DHAN_HOLDINGS_URL, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise DhanAPIError() from exc
        except httpx.TransportError as exc:
            raise DhanAPIError() from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise DhanAPIError(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DhanAPIError() from exc
        if not isinstance(payload, list) or any(
            not isinstance(row, dict) for row in payload
        ):
            raise DhanAPIError()
        return payload
