"""Read-only Dhan client for holdings, positions, and orders."""

import json
import logging
import re
from typing import Any

import httpx

from app.integrations.dhan.settings import dhan_credentials


DHAN_HOLDINGS_URL = "https://api.dhan.co/v2/holdings"
DHAN_POSITIONS_URL = "https://api.dhan.co/v2/positions"
DHAN_ORDERS_URL = "https://api.dhan.co/v2/orders"
DEFAULT_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger(__name__)
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "authorization",
    "clientid",
    "client_id",
    "isin",
    "symbol",
    "security",
    "quantity",
    "qty",
    "price",
    "value",
    "holding",
    "account",
    "phone",
    "email",
    "name",
)


def _safe_response_body(
    body: str | None,
    *,
    secrets: tuple[str, ...] = (),
) -> str | None:
    """Keep useful provider error text while suppressing credentials/portfolio data."""
    if body is None:
        return None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        safe_body = body
    else:
        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: (
                        "[REDACTED]"
                        if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
                        else scrub(item)
                    )
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, str):
                return value
            return value

        safe_body = json.dumps(scrub(parsed), ensure_ascii=True)

    for secret in secrets:
        if secret:
            safe_body = safe_body.replace(secret, "[REDACTED]")
    safe_body = re.sub(
        r"(?i)\b(bearer|access-token|client-id|client_id)\s*[:=]?\s*[^\s,\"'}]+",
        r"\1 [REDACTED]",
        safe_body,
    )
    safe_body = re.sub(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", "[REDACTED]", safe_body)
    safe_body = re.sub(r"\b\d{10,}\b", "[REDACTED]", safe_body)
    safe_body = re.sub(r"[\r\n\t]+", " ", safe_body)
    return safe_body[:4000]


def _log_request_result(
    *,
    operation: str,
    url: str,
    status_code: int,
    response_body: str,
    headers: dict[str, str],
    payload_summary: dict[str, Any] | None = None,
    secrets: tuple[str, ...] = (),
) -> None:
    logger.info(
        "Dhan API request operation=%s url=%s status_code=%s "
        "response_body=%s payload=%s client_id_present=%s access_token_present=%s",
        operation,
        url,
        status_code,
        _safe_response_body(response_body, secrets=secrets),
        payload_summary,
        bool(headers.get("client-id")),
        bool(headers.get("access-token")),
    )


class DhanConfigurationError(RuntimeError):
    """Raised when the required Dhan credentials are not configured."""


class DhanAPIError(RuntimeError):
    """Raised for sanitized Dhan API and response failures."""

    def __init__(
        self,
        status_code: int | None = None,
        *,
        operation: str = "API",
        response_body: str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        self.safe_response_body = _safe_response_body(
            response_body,
            secrets=secrets,
        )
        safe_operation = {
            "holdings": "holdings",
            "positions": "positions",
            "orders": "orders",
        }.get(operation, "API")
        self.operation = safe_operation
        message = (
            f"Dhan {safe_operation} request failed (HTTP {status_code})."
            if status_code is not None
            else f"Dhan {safe_operation} request returned an invalid response."
        )
        if self.safe_response_body:
            message = f"{message} Provider response: {self.safe_response_body}"
        super().__init__(message)


class DhanClient:
    """Read-only client for Dhan holdings and market valuation data."""

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
        self._secrets = (client_id, access_token)

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
        return self._get_list(
            DHAN_HOLDINGS_URL,
            operation="holdings",
        )

    def get_positions(self) -> list[dict[str, Any]]:
        return self._get_list(
            DHAN_POSITIONS_URL,
            operation="positions",
        )

    def get_orders(self) -> list[dict[str, Any]]:
        return self._get_list(
            DHAN_ORDERS_URL,
            operation="orders",
        )

    def _get_list(self, url: str, *, operation: str) -> list[dict[str, Any]]:
        try:
            response = self._client.get(url, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise DhanAPIError(operation=operation) from exc
        except httpx.TransportError as exc:
            raise DhanAPIError(operation=operation) from exc

        _log_request_result(
            operation=operation,
            url=url,
            status_code=response.status_code,
            response_body=response.text,
            headers=self._headers,
            secrets=self._secrets,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise DhanAPIError(
                response.status_code,
                operation=operation,
                response_body=response.text,
                secrets=self._secrets,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DhanAPIError(operation=operation) from exc
        if not isinstance(payload, list) or any(
            not isinstance(row, dict) for row in payload
        ):
            raise DhanAPIError(operation=operation)
        return payload
