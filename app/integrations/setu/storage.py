"""TEST-only storage integration for normalized Setu AA records."""

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.integrations.setu.exceptions import (
    SetuConfigurationError,
    SetuTestDatabaseError,
)
from app.models import DataSyncRun, FinancialConnection
from app.schemas import NormalizedAccount, NormalizedTransaction
from app.store import store_sync

TEST_DATABASE_NAME = "BFAMS-DeFI-test"
SETU_SANDBOX_PROVIDER_CODE = "setu_sandbox"
SETU_SANDBOX_EXTERNAL_CONNECTION_ID = "SETU-SANDBOX-1"


def store_setu_normalized_data(
    session: Session,
    connection: FinancialConnection,
    provider_code: str,
    accounts: list[NormalizedAccount],
    transactions_by_account: dict[str, list[NormalizedTransaction]],
    rejected: list[dict[str, Any]],
) -> DataSyncRun:
    if (
        provider_code != SETU_SANDBOX_PROVIDER_CODE
        or connection.external_connection_id
        != SETU_SANDBOX_EXTERNAL_CONNECTION_ID
    ):
        raise SetuConfigurationError(
            "Setu data can only be stored on its dedicated sandbox connection."
        )

    connection_count = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.user_id == connection.user_id)
    )
    if connection_count != 1:
        raise SetuConfigurationError(
            "Setu sandbox user must have exactly one connection."
        )

    account_ids = {
        account.external_account_id
        for account in accounts
        if account.external_account_id.strip()
    }
    if len(account_ids) != len(accounts):
        raise SetuConfigurationError(
            "Setu normalized accounts must have unique external identifiers."
        )
    for account_id, transactions in transactions_by_account.items():
        if not account_id.strip() or account_id not in account_ids:
            raise SetuConfigurationError(
                "Setu transactions must reference a normalized account."
            )
        if any(
            not transaction.external_transaction_id.strip()
            for transaction in transactions
        ):
            raise SetuConfigurationError(
                "Setu normalized transactions must have external identifiers."
            )

    if session.scalar(text("SELECT current_database()")) != TEST_DATABASE_NAME:
        raise SetuTestDatabaseError("Setu sync requires the TEST database.")

    return store_sync(
        session,
        connection.id,
        accounts,
        transactions_by_account,
        rejected,
        sync_type="setu_aa",
    )
