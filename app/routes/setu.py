"""Sandbox-only Setu integration endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.authorization import get_owned_consent, get_owned_connection
from app.auth.deps import get_current_user
from app.auth.schemas import UserResponse
from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import (
    SetuDataSessionPollingTimeoutError,
    SetuDataSessionTerminalError,
    SetuAPIError,
    SetuError,
    SetuTestDatabaseError,
)
from app.integrations.setu.storage import (
    SETU_SANDBOX_EXTERNAL_CONNECTION_ID,
    store_setu_normalized_data,
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
    is_setu_sandbox_connection,
    poll_data_session,
    retrieve_data_session,
    retrieve_normalized_fi_data,
    request_fi_data_fetch,
    refresh_consent_status,
)
from app.models import ConsentRecord, DataProvider, FinancialConnection
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
    current_user: UserResponse = Depends(get_current_user),
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

    connection = get_owned_connection(
        session,
        request.connection_id,
        current_user.user_id,
    )
    provider_code = session.scalar(
        select(DataProvider.provider_code).where(
            DataProvider.id == connection.provider_id
        )
    )
    if not is_setu_sandbox_connection(connection, provider_code):
        raise HTTPException(status_code=404, detail="Resource not found.")

    try:
        with SetuClient(settings) as client:
            consent = create_consent(client, request)
        session.add(
            ConsentRecord(
                connection_id=connection.id,
                external_consent_id=consent.id,
                status=consent.status.lower(),
            )
        )
        session.commit()
        return consent
    except SetuAPIError as exc:
        detail = {
            "message": "Setu consent request failed.",
            "upstream_status": exc.status_code,
        }
        if exc.error_code:
            detail["error_code"] = exc.error_code
        if exc.message:
            detail["provider_message"] = exc.message
        if exc.request_id:
            detail["request_id"] = exc.request_id
        raise HTTPException(status_code=502, detail=detail) from exc
    except SetuError as exc:
        raise HTTPException(
            status_code=502,
            detail="Setu consent request failed.",
        ) from exc


@router.get("/consents/{consent_id}", response_model=ConsentResponse)
def get_consent_status(
    consent_id: str,
    current_user: UserResponse = Depends(get_current_user),
    session: Session = Depends(get_setu_test_db),
) -> ConsentResponse:
    get_owned_consent(session, consent_id, current_user.user_id)
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
    current_user: UserResponse = Depends(get_current_user),
    session: Session = Depends(get_setu_test_db),
) -> DataSessionResponse:
    get_owned_consent(session, consent_id, current_user.user_id)
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
    consent_id: str = Query(...),
    poll: bool = False,
    current_user: UserResponse = Depends(get_current_user),
    session: Session = Depends(get_setu_test_db),
) -> dict:
    get_owned_consent(session, consent_id, current_user.user_id)
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
                data_session = retrieve_data_session(client, session_id)
            else:
                data_session = poll_data_session(
                    client,
                    session_id,
                    interval_seconds=settings.session_poll_interval_seconds,
                    max_attempts=settings.session_poll_max_attempts,
                    timeout_seconds=settings.session_poll_timeout_seconds,
                )
        response_consent_id = data_session.get("consentId")
        if response_consent_id != consent_id:
            raise HTTPException(status_code=404, detail="Resource not found.")
        return data_session
    except HTTPException:
        raise
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
    current_user: UserResponse = Depends(get_current_user),
    session: Session = Depends(get_setu_test_db),
) -> dict[str, str | int]:
    record, connection = get_owned_consent(
        session,
        consent_id,
        current_user.user_id,
    )
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

    provider_code = session.scalar(
        select(DataProvider.provider_code)
        .join(FinancialConnection, FinancialConnection.provider_id == DataProvider.id)
        .where(FinancialConnection.id == connection.id)
    )
    if not is_setu_sandbox_connection(connection, provider_code):
        raise HTTPException(status_code=404, detail="Resource not found.")

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
    current_user: UserResponse = Depends(get_current_user),
    session: Session = Depends(get_setu_test_db),
) -> dict[str, str]:
    record, connection = get_owned_consent(
        session,
        consent_id,
        current_user.user_id,
    )
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

    provider_code = session.scalar(
        select(DataProvider.provider_code)
        .join(FinancialConnection, FinancialConnection.provider_id == DataProvider.id)
        .where(FinancialConnection.id == connection.id)
    )
    if not is_setu_sandbox_connection(connection, provider_code):
        raise HTTPException(status_code=404, detail="Resource not found.")

    try:
        with SetuClient(settings) as client:
            consent_status = refresh_consent_status(session, client, record)
            if consent_status["status"] != "approved":
                raise HTTPException(
                    status_code=409,
                    detail="Setu consent is not approved.",
                )
            data_session = retrieve_data_session(client, session_id)
            if data_session.get("consentId") != consent_id:
                raise HTTPException(status_code=404, detail="Resource not found.")
            return request_fi_data_fetch(client, record, session_id)
    except HTTPException:
        raise
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
    current_user: UserResponse = Depends(get_current_user),
    session: Session = Depends(get_setu_test_db),
) -> dict[str, int | str | dict[str, int]]:
    record, connection = get_owned_consent(
        session,
        consent_id,
        current_user.user_id,
    )
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

    provider_code = session.scalar(
        select(DataProvider.provider_code)
        .join(FinancialConnection, FinancialConnection.provider_id == DataProvider.id)
        .where(FinancialConnection.id == connection.id)
    )
    if (
        connection.external_connection_id != SETU_SANDBOX_EXTERNAL_CONNECTION_ID
        or provider_code != "setu_sandbox"
        or not is_setu_sandbox_connection(connection, provider_code)
    ):
        raise HTTPException(status_code=404, detail="Resource not found.")

    try:
        with SetuClient(settings) as client:
            consent = fetch_consent_status(client, consent_id)
            if consent.status.upper() != "ACTIVE":
                raise HTTPException(
                    status_code=409,
                    detail="Setu consent is not approved.",
                )
            data_session = retrieve_data_session(client, session_id)
            if (
                data_session.get("consentId") != consent_id
                or not isinstance(data_session.get("status"), str)
                or data_session["status"].upper() not in {"PARTIAL", "COMPLETED"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Setu financial data is not ready to sync.",
                )
            if data_session.get("format") != "json":
                raise HTTPException(
                    status_code=422,
                    detail="Setu session format is not supported for normalization.",
                )
            accounts, transactions_by_account, rejected = retrieve_normalized_fi_data(
                data_session
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

    try:
        run = store_setu_normalized_data(
            session,
            connection,
            provider_code,
            accounts,
            transactions_by_account,
            rejected,
        )
    except SetuTestDatabaseError as exc:
        raise HTTPException(
            status_code=503,
            detail="Setu sync requires the TEST database.",
        ) from exc
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
