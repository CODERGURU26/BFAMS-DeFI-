"""Setu consent creation and persistence."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.integrations.setu.client import SetuClient
from app.integrations.setu.exceptions import SetuMalformedResponseError
from app.models import ConsentRecord, FinancialConnection

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
    session: Session,
    client: SetuClient,
    request: CreateConsentRequest,
) -> dict[str, str]:
    payload = {
        "consentDuration": request.consent_duration.model_dump(),
        "vua": request.vua.get_secret_value(),
    }
    if request.data_range is not None:
        payload["dataRange"] = request.data_range.model_dump(
            by_alias=True, mode="json"
        )

    response = client.request("POST", "/consents", json=payload)
    consent_id = response.get("id")
    approval_url = response.get("url")
    status = response.get("status")
    if (
        not isinstance(consent_id, str)
        or not consent_id.strip()
        or not isinstance(approval_url, str)
        or not isinstance(status, str)
        or status.upper() != "PENDING"
    ):
        raise SetuMalformedResponseError(
            "Setu consent response is missing a valid pending consent."
        )

    consent_id = consent_id.strip()
    approval_url = approval_url.strip()
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

    existing = session.scalars(
        select(ConsentRecord).where(
            ConsentRecord.connection_id == request.connection_id,
            ConsentRecord.external_consent_id == consent_id,
        )
    ).first()
    if existing is None:
        record = ConsentRecord(
            connection_id=request.connection_id,
            external_consent_id=consent_id,
            status="pending",
        )
        session.add(record)
        try:
            _verify_test_database(session)
            session.commit()
        except (SQLAlchemyError, RuntimeError):
            session.rollback()
            raise

    return {
        "consent_id": consent_id,
        "approval_url": approval_url,
        "status": "pending",
    }
