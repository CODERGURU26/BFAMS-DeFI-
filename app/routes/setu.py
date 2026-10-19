"""Sandbox-only Setu integration endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import (
    SetuDataSessionPollingTimeoutError,
    SetuDataSessionTerminalError,
    SetuError,
)
from app.integrations.setu.service import (
    ConsentResponse,
    CreateConsentRequest,
    CreateDataSessionRequest,
    DataSessionResponse,
    create_data_session,
    create_consent,
    get_consent_status as fetch_consent_status,
    get_fi_fetch_status,
    fetch_data_session_summary,
    fetch_normalized_fi_data,
    is_setu_sandbox_connection,
    poll_data_session,
    retrieve_data_session,
    request_fi_data_fetch,
    refresh_consent_status,
)
from app.models import ConsentRecord, DataProvider, FinancialConnection, User
from app.store import store_sync
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


@router.post("/consents", response_model=ConsentResponse)
def request_consent(
    request: CreateConsentRequest,
    session: Session = Depends(get_setu_test_db),
) -> ConsentResponse:
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
            return create_consent(client, request)
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu consent request failed.",
        ) from exc


@router.get("/consents/{consent_id}", response_model=ConsentResponse)
def get_consent_status(
    consent_id: str,
) -> ConsentResponse:
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

    try:
        with SetuClient(settings) as client:
            return fetch_consent_status(client, consent_id)
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu consent status could not be retrieved.",
        ) from exc


@router.post(
    "/consents/{consent_id}/sessions",
    response_model=DataSessionResponse,
)
def request_data_session(
    consent_id: str,
    request: CreateDataSessionRequest,
) -> DataSessionResponse:
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

    try:
        with SetuClient(settings) as client:
            consent = fetch_consent_status(client, consent_id)
            if consent.status.upper() != "ACTIVE":
                raise HTTPException(
                    status_code=409,
                    detail="Setu consent is not approved.",
                )
            return create_data_session(client, consent, request)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Setu consent is not approved.",
        ) from exc
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu data session could not be created.",
        ) from exc


@router.get("/sessions/{session_id}")
def get_data_session(
    session_id: str,
    poll: bool = False,
) -> dict:
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

    try:
        with SetuClient(settings) as client:
            if not poll:
                return retrieve_data_session(client, session_id)
            return poll_data_session(
                client,
                session_id,
                interval_seconds=settings.session_poll_interval_seconds,
                max_attempts=settings.session_poll_max_attempts,
                timeout_seconds=settings.session_poll_timeout_seconds,
            )
    except SetuDataSessionTerminalError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Setu data session reached terminal status {exc.status}.",
        ) from exc
    except SetuDataSessionPollingTimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Setu data session did not become retrievable within the polling limit.",
        ) from exc
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu data session could not be retrieved.",
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
            fetch_status = get_fi_fetch_status(client, record)
            if fetch_status["status"] not in {"partial", "completed"}:
                raise HTTPException(
                    status_code=409,
                    detail="Setu financial data is not ready to fetch.",
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


@router.post("/consents/{consent_id}/sessions/{session_id}/fetch")
def request_fi_fetch(
    consent_id: str,
    session_id: str,
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
            return request_fi_data_fetch(client, record, session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Setu consent is not ready for a data fetch.",
        ) from exc
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu financial data fetch could not be requested.",
        ) from exc


@router.post("/consents/{consent_id}/sessions/{session_id}/sync")
def sync_fi_data(
    consent_id: str,
    session_id: str,
    session: Session = Depends(get_setu_test_db),
) -> dict[str, int | str | dict[str, int]]:
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
            fetch_status = get_fi_fetch_status(client, record)
            if fetch_status["status"] not in {"partial", "completed"}:
                raise HTTPException(
                    status_code=409,
                    detail="Setu financial data is not ready to sync.",
                )
            accounts, transactions_by_account, rejected = fetch_normalized_fi_data(
                client,
                record,
                session_id,
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Setu financial data is not ready to sync.",
        ) from exc
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu financial data could not be retrieved.",
        ) from exc

    if session.scalar(text("SELECT current_database()")) != "BFAMS-DeFI-test":
        raise HTTPException(
            status_code=503,
            detail="Setu sync requires the TEST database.",
        )

    run = store_sync(
        session,
        connection.id,
        accounts,
        transactions_by_account,
        rejected,
        sync_type="setu_aa",
    )
    rejection_counts: dict[str, int] = {}
    for item in rejected:
        reason = item.get("reason", "invalid_record")
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    return {
        "status": run.status,
        "accounts_normalized": len(accounts),
        "transactions_normalized": sum(
            len(items) for items in transactions_by_account.values()
        ),
        "records_processed": run.records_processed,
        "records_rejected": len(rejected),
        "rejection_counts": rejection_counts,
    }
