"""Validation and TEST-database persistence for Dhan holdings snapshots."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demat_ingestion import _infer_asset_type, _is_valid_isin
from app.models import (
    DataSyncRun,
    FinancialAccount,
    InvestmentHolding,
)


class DhanHoldingsValidationError(ValueError):
    """Raised when the Dhan holdings response is malformed or ambiguous."""


def _quantity(value: Any, row_number: int) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DhanHoldingsValidationError(
            f"Dhan holdings row {row_number} has an invalid quantity."
        ) from exc
    if not quantity.is_finite() or quantity < 0:
        raise DhanHoldingsValidationError(
            f"Dhan holdings row {row_number} has an invalid quantity."
        )
    return quantity


def build_holding_rows(
    payload: list[dict[str, Any]],
    account_id: uuid.UUID,
    observed_at: datetime,
    metadata_by_isin: dict[str, InvestmentHolding],
) -> list[InvestmentHolding]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DhanHoldingsValidationError("Snapshot timestamp must be timezone-aware.")

    holdings: list[InvestmentHolding] = []
    seen_identifiers: set[str] = set()

    for row_number, item in enumerate(payload, start=1):
        symbol_value = item.get("tradingSymbol")
        if not isinstance(symbol_value, str) or not symbol_value.strip():
            raise DhanHoldingsValidationError(
                f"Dhan holdings row {row_number} has no trading symbol."
            )
        symbol = symbol_value.strip().upper()

        raw_isin = item.get("isin")
        isin = None
        if raw_isin not in (None, ""):
            if not isinstance(raw_isin, str) or not _is_valid_isin(raw_isin.strip()):
                raise DhanHoldingsValidationError(
                    f"Dhan holdings row {row_number} has an invalid ISIN."
                )
            isin = raw_isin.strip().upper()

        identifier = isin or symbol
        if identifier in seen_identifiers:
            raise DhanHoldingsValidationError(
                f"Dhan holdings response contains a duplicate identifier at row {row_number}."
            )
        seen_identifiers.add(identifier)

        if "totalQty" not in item:
            raise DhanHoldingsValidationError(
                f"Dhan holdings row {row_number} has no total quantity."
            )
        quantity = _quantity(item["totalQty"], row_number)
        metadata = metadata_by_isin.get(isin) if isin else None
        asset_name = metadata.asset_name if metadata else symbol
        asset_type = (
            metadata.asset_type
            if metadata
            else _infer_asset_type(symbol, isin, asset_name)
        )

        holdings.append(
            InvestmentHolding(
                account_id=account_id,
                asset_name=asset_name,
                asset_identifier=identifier,
                asset_type=asset_type,
                quantity=quantity,
                unit_price=None,
                market_value=None,
                currency="INR",
                observed_at=observed_at,
            )
        )

    return holdings


def persist_dhan_holdings(
    session: Session,
    account: FinancialAccount,
    payload: list[dict[str, Any]],
) -> int:
    observed_at = datetime.now(timezone.utc)
    isins = {
        item["isin"].strip().upper()
        for item in payload
        if isinstance(item.get("isin"), str)
        and _is_valid_isin(item["isin"].strip())
    }
    metadata_by_isin: dict[str, InvestmentHolding] = {}
    if isins:
        existing_rows = session.scalars(
            select(InvestmentHolding)
            .where(
                InvestmentHolding.account_id == account.id,
                InvestmentHolding.asset_identifier.in_(isins),
            )
            .order_by(InvestmentHolding.observed_at.desc())
        ).all()
        for row in existing_rows:
            metadata_by_isin.setdefault(row.asset_identifier, row)

    holdings = build_holding_rows(
        payload,
        account.id,
        observed_at,
        metadata_by_isin,
    )
    connection_id = account.connection_id
    session.add_all(holdings)
    session.add(
        DataSyncRun(
            connection_id=connection_id,
            sync_type="holdings_only",
            status="completed",
            started_at=observed_at,
            completed_at=observed_at,
            records_processed=len(holdings),
            error_message=None,
        )
    )
    session.commit()
    return len(holdings)
