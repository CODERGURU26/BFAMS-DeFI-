"""Setu consent and financial-data service operations."""

from datetime import datetime
from math import isfinite
from time import monotonic, sleep
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.integrations.setu.client import SetuClient
from app.integrations.setu.exceptions import SetuMalformedResponseError
from app.integrations.setu.exceptions import (
    SetuDataSessionPollingTimeoutError,
    SetuDataSessionTerminalError,
)
from app.integrations.setu.normalizer import normalize_setu_fi_response
from app.models import ConsentRecord, FinancialConnection
from app.schemas import NormalizedAccount, NormalizedTransaction

TEST_DATABASE_NAME = "BFAMS-DeFI-test"


class ConsentDuration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["DAY", "MONTH", "YEAR"]
    value: int = Field(gt=0)


class ConsentDataRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_date: datetime = Field(alias="from")
    to_date: datetime = Field(alias="to")

    @field_validator("from_date", "to_date")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Consent data range timestamps must include a timezone.")
        return value

    @field_validator("to_date")
    @classmethod
    def require_ordered_range(cls, value: datetime, info) -> datetime:
        start = info.data.get("from_date")
        if start is not None and value <= start:
            raise ValueError("Consent data range end must be after its start.")
        return value


class CreateConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    vua: SecretStr
    consent_duration: ConsentDuration = Field(alias="consentDuration")
    data_range: ConsentDataRange | None = Field(default=None, alias="dataRange")

    @field_validator("vua")
    @classmethod
    def require_vua(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("VUA must not be empty.")
        return value


class ConsentResponse(BaseModel):
    id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    url: str | None = None


class CreateDataSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_range: ConsentDataRange = Field(alias="dataRange")
    format: Literal["json", "xml"]


class DataSessionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    consentId: str = Field(min_length=1)
    status: str = Field(min_length=1)
    format: Literal["json", "xml"]


def is_setu_sandbox_connection(
    connection: FinancialConnection,
    provider_code: str | None,
) -> bool:
    return (
        provider_code == "setu_sandbox"
        and (connection.external_connection_id or "").startswith("SETU-SANDBOX-")
    )


def _verify_test_database(session: Session) -> None:
    if session.scalar(text("SELECT current_database()")) != TEST_DATABASE_NAME:
        raise RuntimeError("Setu consent persistence requires the TEST database.")


def create_consent(
    client: SetuClient,
    request: CreateConsentRequest,
) -> ConsentResponse:
    payload = {
        "consentDuration": request.consent_duration.model_dump(),
        "vua": request.vua.get_secret_value(),
    }
    if request.data_range is not None:
        payload["dataRange"] = request.data_range.model_dump(
            by_alias=True, mode="json"
        )

    response = client.request("POST", "/v2/consents", json=payload)
    if (
        not isinstance(response.get("id"), str)
        or not response["id"].strip()
        or not isinstance(response.get("url"), str)
        or not isinstance(response.get("status"), str)
        or response["status"].upper() != "PENDING"
    ):
        raise SetuMalformedResponseError(
            "Setu consent response is missing a valid pending consent."
        )

    consent_id = response["id"].strip()
    approval_url = response["url"].strip()
    status = response["status"].strip()
    parsed_url = urlsplit(approval_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or not (
            parsed_url.hostname == "setu.co"
            or parsed_url.hostname.endswith(".setu.co")
        )
    ):
        raise SetuMalformedResponseError(
            "Setu consent response contains an invalid approval URL."
        )

    return ConsentResponse(id=consent_id, status=status, url=approval_url)


def get_consent_status(
    client: SetuClient,
    consent_id: str,
) -> ConsentResponse:
    consent_id = consent_id.strip()
    if not consent_id:
        raise SetuMalformedResponseError("Setu consent ID must not be empty.")

    response = client.request(
        "GET",
        f"/v2/consents/{quote(consent_id, safe='')}",
    )
    provider_id = response.get("id")
    provider_status = response.get("status")
    approval_url = response.get("url")
    if (
        not isinstance(provider_id, str)
        or provider_id != consent_id
        or not isinstance(provider_status, str)
        or provider_status.upper() not in _CONSENT_STATUS_MAP
        or (
            approval_url is not None
            and (
                not isinstance(approval_url, str)
                or not approval_url.strip()
            )
        )
    ):
        raise SetuMalformedResponseError(
            "Setu consent status response is invalid."
        )
    if isinstance(approval_url, str):
        parsed_url = urlsplit(approval_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or not (
                parsed_url.hostname == "setu.co"
                or parsed_url.hostname.endswith(".setu.co")
            )
        ):
            raise SetuMalformedResponseError(
                "Setu consent response contains an invalid approval URL."
            )

    return ConsentResponse(
        id=provider_id,
        status=provider_status.strip(),
        url=approval_url,
    )


_CONSENT_STATUS_MAP = {
    "PENDING": "pending",
    "ACTIVE": "approved",
    "REJECTED": "rejected",
    "REVOKED": "revoked",
    "EXPIRED": "expired",
}


def refresh_consent_status(
    session: Session,
    client: SetuClient,
    record: ConsentRecord,
) -> dict[str, str]:
    if not record.external_consent_id:
        raise SetuMalformedResponseError(
            "Stored Setu consent reference is missing."
        )
    response = client.request(
        "GET",
        f"/v2/consents/{quote(record.external_consent_id, safe='')}",
    )
    provider_id = response.get("id")
    provider_status = response.get("status")
    if (
        not isinstance(provider_status, str)
        or provider_status.upper() not in _CONSENT_STATUS_MAP
        or provider_id != record.external_consent_id
    ):
        raise SetuMalformedResponseError(
            "Setu consent status response is invalid."
        )

    status = _CONSENT_STATUS_MAP[provider_status.upper()]
    detail = response.get("detail")
    expires_at = None
    if isinstance(detail, dict) and detail.get("consentExpiry") is not None:
        expiry_value = detail["consentExpiry"]
        if not isinstance(expiry_value, str):
            raise SetuMalformedResponseError(
                "Setu consent expiry is invalid."
            )
        try:
            expires_at = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SetuMalformedResponseError(
                "Setu consent expiry is invalid."
            ) from exc
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise SetuMalformedResponseError(
                "Setu consent expiry must include a timezone."
            )

    if record.status != status or (
        expires_at is not None and record.expires_at != expires_at
    ):
        record.status = status
        if expires_at is not None:
            record.expires_at = expires_at
        try:
            _verify_test_database(session)
            session.commit()
        except (SQLAlchemyError, RuntimeError):
            session.rollback()
            raise

    return {"consent_id": record.external_consent_id, "status": status}


def create_data_session(
    client: SetuClient,
    consent: ConsentResponse,
    request: CreateDataSessionRequest,
) -> DataSessionResponse:
    consent_id = consent.id.strip()
    if not consent_id:
        raise SetuMalformedResponseError("Setu consent reference is missing.")
    if consent.status.upper() != "ACTIVE":
        raise ValueError("Setu consent must be active before creating a session.")

    response = client.request(
        "POST",
        "/v2/sessions",
        json={
            "consentId": consent_id,
            "dataRange": request.data_range.model_dump(
                by_alias=True, mode="json"
            ),
            "format": request.format,
        },
    )
    try:
        session = DataSessionResponse.model_validate(response)
    except ValueError as exc:
        raise SetuMalformedResponseError(
            "Setu data session response is invalid."
        ) from exc
    if session.consentId != consent_id or session.format != request.format:
        raise SetuMalformedResponseError(
            "Setu data session response is invalid."
        )
    return session


def request_fi_data_fetch(
    client: SetuClient,
    record: ConsentRecord,
    session_id: str,
) -> dict[str, str]:
    if record.status != "approved":
        raise ValueError("Setu consent must be approved before requesting data.")
    if not record.external_consent_id or not session_id.strip():
        raise SetuMalformedResponseError("Setu data session reference is invalid.")

    client.request(
        "POST",
        f"/v2/sessions/{quote(session_id.strip(), safe='')}",
        allow_empty=True,
    )
    return {"status": "fetch_requested"}


def get_fi_fetch_status(
    client: SetuClient,
    record: ConsentRecord,
) -> dict[str, str]:
    if not record.external_consent_id:
        raise SetuMalformedResponseError("Stored Setu consent reference is missing.")

    response = client.request(
        "GET",
        f"/v2/consents/{quote(record.external_consent_id, safe='')}/fetch/status",
    )
    provider_status = response.get("status")
    response_consent_id = response.get("consentId")
    if (
        not isinstance(provider_status, str)
        or provider_status.upper() not in _DATA_SESSION_STATUSES
        or (
            response_consent_id is not None
            and response_consent_id != record.external_consent_id
        )
    ):
        raise SetuMalformedResponseError("Setu fetch status response is invalid.")

    return {"status": provider_status.upper().lower()}


_DATA_SESSION_STATUSES = {
    "PENDING",
    "PARTIAL",
    "COMPLETED",
    "EXPIRED",
    "FAILED",
}


def retrieve_data_session(
    client: SetuClient,
    session_id: str,
) -> dict[str, Any]:
    session_id = session_id.strip()
    if not session_id:
        raise SetuMalformedResponseError("Setu data session ID must not be empty.")

    response = client.request(
        "GET",
        f"/v2/sessions/{quote(session_id, safe='')}",
    )
    response_id = response.get("id")
    status = response.get("status")
    if (
        response_id != session_id
        or not isinstance(status, str)
        or status.upper() not in _DATA_SESSION_STATUSES
    ):
        raise SetuMalformedResponseError("Setu data session response is invalid.")
    return response


def poll_data_session(
    client: SetuClient,
    session_id: str,
    *,
    interval_seconds: float = 1.0,
    max_attempts: int = 30,
    timeout_seconds: float = 30.0,
    _sleep=sleep,
    _monotonic=monotonic,
) -> dict[str, Any]:
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts <= 0
        or not isfinite(interval_seconds)
        or interval_seconds <= 0
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("Setu session polling bounds must be positive.")

    deadline = _monotonic() + timeout_seconds
    for attempt in range(max_attempts):
        response = retrieve_data_session(client, session_id)
        if _monotonic() >= deadline:
            break
        status = response["status"].upper()
        if status in {"PARTIAL", "COMPLETED"}:
            return response
        if status in {"EXPIRED", "FAILED"}:
            raise SetuDataSessionTerminalError(status)
        if attempt == max_attempts - 1:
            break

        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        _sleep(min(interval_seconds, remaining))
        if _monotonic() >= deadline:
            break

    raise SetuDataSessionPollingTimeoutError()


def fetch_data_session_summary(
    client: SetuClient,
    record: ConsentRecord,
    session_id: str,
) -> dict[str, str | int]:
    if record.status != "approved":
        raise ValueError("Setu consent must be approved before fetching data.")
    if not record.external_consent_id:
        raise SetuMalformedResponseError("Stored Setu consent reference is missing.")
    if not session_id.strip():
        raise SetuMalformedResponseError("Setu data session reference is invalid.")

    response = client.request(
        "GET",
        f"/v2/sessions/{quote(session_id.strip(), safe='')}",
    )
    provider_status = response.get("status")
    if (
        response.get("id") != session_id.strip()
        or response.get("consentId") != record.external_consent_id
        or not isinstance(provider_status, str)
        or provider_status.upper() not in _DATA_SESSION_STATUSES
        or response.get("format") not in {"json", "xml"}
    ):
        raise SetuMalformedResponseError("Setu data session response is invalid.")

    status = provider_status.upper()
    if status not in {"PARTIAL", "COMPLETED"}:
        raise ValueError("Setu financial data is not ready to fetch.")

    fips = response["fips"]
    accounts, transactions_by_account, rejected = retrieve_normalized_fi_data(
        response
    )

    return {
        "status": status.lower(),
        "fi_provider_count": len(fips),
        "account_count": len(accounts),
        "transaction_count": sum(
            len(transactions)
            for transactions in transactions_by_account.values()
        ),
        "rejected_count": len(rejected),
    }


def retrieve_normalized_fi_data(
    response: dict[str, Any],
) -> tuple[
    list[NormalizedAccount],
    dict[str, list[NormalizedTransaction]],
    list[dict[str, Any]],
]:
    fips = response.get("fips")
    if not isinstance(fips, list):
        raise SetuMalformedResponseError("Setu financial data response is invalid.")
    status = response.get("status")
    if not isinstance(status, str) or status.upper() not in {"PARTIAL", "COMPLETED"}:
        raise ValueError("Setu financial data is not ready to fetch.")

    return normalize_setu_fi_response(response)


def fetch_normalized_fi_data(
    client: SetuClient,
    record: ConsentRecord,
    session_id: str,
) -> tuple[
    list[NormalizedAccount],
    dict[str, list[NormalizedTransaction]],
    list[dict[str, Any]],
]:
    if record.status != "approved":
        raise ValueError("Setu consent must be approved before fetching data.")
    if not record.external_consent_id or not session_id.strip():
        raise SetuMalformedResponseError("Setu data session reference is invalid.")

    response = client.request(
        "GET",
        f"/v2/sessions/{quote(session_id.strip(), safe='')}",
    )
    provider_status = response.get("status")
    if (
        response.get("id") != session_id.strip()
        or response.get("consentId") != record.external_consent_id
        or not isinstance(provider_status, str)
        or provider_status.upper() not in {"PARTIAL", "COMPLETED"}
        or response.get("format") != "json"
    ):
        if (
            isinstance(provider_status, str)
            and provider_status.upper() in _DATA_SESSION_STATUSES
            and provider_status.upper() not in {"PARTIAL", "COMPLETED"}
        ):
            raise ValueError("Setu financial data is not ready to fetch.")
        raise SetuMalformedResponseError("Setu data session response is invalid.")

    return retrieve_normalized_fi_data(response)
