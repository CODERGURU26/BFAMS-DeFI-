"""Database-backed ownership checks shared by financial-data routes."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    ConsentRecord,
    FinancialAccount,
    FinancialConnection,
)


def assert_authorization_database(session: Session) -> None:
    database_name = session.execute(
        text("SELECT current_database()")
    ).scalar_one()
    if database_name != "BFAMS-DeFI-test":
        raise RuntimeError("Authorization requires BFAMS-DeFI-test.")


def get_owned_account(
    session: Session,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
) -> FinancialAccount:
    assert_authorization_database(session)
    account = session.execute(
        select(FinancialAccount)
        .join(
            FinancialConnection,
            FinancialConnection.id == FinancialAccount.connection_id,
        )
        .where(
            FinancialAccount.id == account_id,
            FinancialConnection.user_id == user_id,
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Resource not found.")
    return account


def get_owned_connection(
    session: Session,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
) -> FinancialConnection:
    assert_authorization_database(session)
    connection = session.scalar(
        select(FinancialConnection).where(
            FinancialConnection.id == connection_id,
            FinancialConnection.user_id == user_id,
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Resource not found.")
    return connection


def get_owned_consent(
    session: Session,
    external_consent_id: str,
    user_id: uuid.UUID,
) -> tuple[ConsentRecord, FinancialConnection]:
    assert_authorization_database(session)
    row = session.execute(
        select(ConsentRecord, FinancialConnection)
        .join(
            FinancialConnection,
            FinancialConnection.id == ConsentRecord.connection_id,
        )
        .where(
            ConsentRecord.external_consent_id == external_consent_id,
            FinancialConnection.user_id == user_id,
        )
        .limit(2)
    ).all()
    if len(row) != 1:
        raise HTTPException(status_code=404, detail="Resource not found.")
    return row[0]
