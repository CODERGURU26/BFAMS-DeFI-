"""Sandbox-only Setu integration endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings
from app.integrations.setu.exceptions import SetuError
from app.integrations.setu.service import (
    CreateConsentRequest,
    create_consent,
    is_setu_sandbox_connection,
)
from app.models import DataProvider, FinancialConnection, User
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
