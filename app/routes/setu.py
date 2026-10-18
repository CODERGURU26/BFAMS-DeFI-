"""Sandbox-only Setu integration endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import SetuError
from app.integrations.setu.service import (
    CreateConsentRequest,
    CreateDataSessionRequest,
    create_data_session,
    create_consent,
    fetch_data_session_summary,
    is_setu_sandbox_connection,
    refresh_consent_status,
)
from app.models import ConsentRecord, DataProvider, FinancialConnection, User
from database import get_test_engine

router = APIRouter(prefix="/api/setu", tags=["Setu sandbox"])


def get_setu_test_db():
    with Session(get_test_engine()) as session:
        if session.scalar(text("SELECT current_database()")) != "BFAMS-DeFI-test":
            raise HTTPException(
                status_code=503,
                detail="Setu endpoints require the TEST database.",
            )
        yield session


@router.get("/health")
def setu_health() -> dict[str, str | bool]:
    settings = SetuSettings()
    if settings.environment != "sandbox":
        raise HTTPException(
            status_code=403,
            detail="Setu endpoints are available only in the sandbox environment.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=503,
            detail="Setu sandbox configuration is incomplete.",
        )

    return {
        "status": "ready",
        "environment": "sandbox",
        "configured": True,
        "connectivity": "not_tested",
    }


@router.post("/consents")
def request_consent(
    request: CreateConsentRequest,
    session: Session = Depends(get_setu_test_db),
) -> dict[str, str]:
    settings = SetuSettings()
    if settings.environment != "sandbox":
        raise HTTPException(
            status_code=403,
            detail="Setu endpoints are available only in the sandbox environment.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=503,
            detail="Setu sandbox configuration is incomplete.",
        )

    connection_row = session.execute(
        select(FinancialConnection, DataProvider.provider_code)
        .join(DataProvider, DataProvider.id == FinancialConnection.provider_id)
        .where(FinancialConnection.id == request.connection_id)
    ).first()
    if connection_row is None:
        raise HTTPException(status_code=404, detail="Setu sandbox connection not found.")

    connection, provider_code = connection_row
    user = session.get(User, connection.user_id)
    user_connection_count = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.user_id == connection.user_id)
    )
    if (
        user is None
        or user_connection_count != 1
        or not is_setu_sandbox_connection(connection, provider_code)
    ):
        raise HTTPException(status_code=404, detail="Setu sandbox connection not found.")

    try:
        with SetuClient(settings) as client:
            return create_consent(session, client, request)
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu consent request failed.",
        ) from exc


@router.get("/consents/{consent_id}")
def get_consent_status(
    consent_id: str,
    session: Session = Depends(get_setu_test_db),
) -> dict[str, str]:
    settings = SetuSettings()
    if settings.environment != "sandbox":
        raise HTTPException(
            status_code=403,
            detail="Setu endpoints are available only in the sandbox environment.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=503,
            detail="Setu sandbox configuration is incomplete.",
        )

    consent_rows = session.execute(
        select(ConsentRecord, FinancialConnection, DataProvider.provider_code)
        .join(
            FinancialConnection,
            FinancialConnection.id == ConsentRecord.connection_id,
        )
        .join(DataProvider, DataProvider.id == FinancialConnection.provider_id)
        .where(ConsentRecord.external_consent_id == consent_id)
        .limit(2)
    ).all()
    if len(consent_rows) != 1:
        raise HTTPException(status_code=404, detail="Setu consent not found.")

    record, connection, provider_code = consent_rows[0]
    user = session.get(User, connection.user_id)
    user_connection_count = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.user_id == connection.user_id)
    )
    if (
        user is None
        or user_connection_count != 1
        or not is_setu_sandbox_connection(connection, provider_code)
    ):
        raise HTTPException(status_code=404, detail="Setu consent not found.")

    try:
        with SetuClient(settings) as client:
            return refresh_consent_status(session, client, record)
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu consent status could not be retrieved.",
        ) from exc


@router.post("/consents/{consent_id}/sessions")
def request_data_session(
    consent_id: str,
    request: CreateDataSessionRequest,
    session: Session = Depends(get_setu_test_db),
) -> dict[str, str]:
    settings = SetuSettings()
    if settings.environment != "sandbox":
        raise HTTPException(
            status_code=403,
            detail="Setu endpoints are available only in the sandbox environment.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=503,
            detail="Setu sandbox configuration is incomplete.",
        )

    consent_rows = session.execute(
        select(ConsentRecord, FinancialConnection, DataProvider.provider_code)
        .join(
            FinancialConnection,
            FinancialConnection.id == ConsentRecord.connection_id,
        )
        .join(DataProvider, DataProvider.id == FinancialConnection.provider_id)
        .where(ConsentRecord.external_consent_id == consent_id)
        .limit(2)
    ).all()
    if len(consent_rows) != 1:
        raise HTTPException(status_code=404, detail="Setu consent not found.")

    record, connection, provider_code = consent_rows[0]
    user = session.get(User, connection.user_id)
    user_connection_count = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.user_id == connection.user_id)
    )
    if (
        user is None
        or user_connection_count != 1
        or not is_setu_sandbox_connection(connection, provider_code)
    ):
        raise HTTPException(status_code=404, detail="Setu consent not found.")

    try:
        with SetuClient(settings) as client:
            consent_status = refresh_consent_status(session, client, record)
            if consent_status["status"] != "approved":
                raise HTTPException(
                    status_code=409,
                    detail="Setu consent is not approved.",
                )
            return create_data_session(client, record, request)
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu data session could not be created.",
        ) from exc


@router.get("/consents/{consent_id}/sessions/{session_id}/data")
def fetch_data_session(
    consent_id: str,
    session_id: str,
    session: Session = Depends(get_setu_test_db),
) -> dict[str, str | int]:
    settings = SetuSettings()
    if settings.environment != "sandbox":
        raise HTTPException(
            status_code=403,
            detail="Setu endpoints are available only in the sandbox environment.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=503,
            detail="Setu sandbox configuration is incomplete.",
        )

    consent_rows = session.execute(
        select(ConsentRecord, FinancialConnection, DataProvider.provider_code)
        .join(
            FinancialConnection,
            FinancialConnection.id == ConsentRecord.connection_id,
        )
        .join(DataProvider, DataProvider.id == FinancialConnection.provider_id)
        .where(ConsentRecord.external_consent_id == consent_id)
        .limit(2)
    ).all()
    if len(consent_rows) != 1:
        raise HTTPException(status_code=404, detail="Setu consent not found.")

    record, connection, provider_code = consent_rows[0]
    user = session.get(User, connection.user_id)
    user_connection_count = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.user_id == connection.user_id)
    )
    if (
        user is None
        or user_connection_count != 1
        or not is_setu_sandbox_connection(connection, provider_code)
    ):
        raise HTTPException(status_code=404, detail="Setu consent not found.")

    try:
        with SetuClient(settings) as client:
            consent_status = refresh_consent_status(session, client, record)
            if consent_status["status"] != "approved":
                raise HTTPException(
                    status_code=409,
                    detail="Setu consent is not approved.",
                )
            return fetch_data_session_summary(client, record, session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Setu financial data is not ready to fetch.",
        ) from exc
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu financial data could not be retrieved.",
        ) from exc
