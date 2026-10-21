"""Validation, reconciliation, and TEST-database persistence for Dhan holdings."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demat_ingestion import _infer_asset_type, _is_valid_isin
from app.models import DataSyncRun, FinancialAccount, InvestmentHolding


class DhanHoldingsValidationError(ValueError):
    """Raised when the Dhan holdings response is malformed or ambiguous."""


@dataclass
class DhanHoldingsPlan:
    holdings: list[InvestmentHolding]
    fetched: int
    to_insert: int
    skipped_zero_qty: int
    same_quantity: int
    different_quantity: int
    new_in_api: int
    absent_from_api: int
    inferred_from_symbol_only: int
    without_isin: int

    def counts(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "to_insert": self.to_insert,
            "skipped_zero_qty": self.skipped_zero_qty,
            "same_quantity": self.same_quantity,
            "different_quantity": self.different_quantity,
            "new_in_api": self.new_in_api,
            "absent_from_api": self.absent_from_api,
            "inferred_from_symbol_only": self.inferred_from_symbol_only,
            "without_isin": self.without_isin,
        }


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


def analyze_dhan_holdings(
    session: Session,
    account: FinancialAccount,
    payload: list[dict[str, Any]],
    *,
    observed_at: datetime | None = None,
) -> DhanHoldingsPlan:
    observed_at = observed_at or datetime.now(timezone.utc)
    latest_observed_at = session.scalar(
        select(InvestmentHolding.observed_at)
        .where(InvestmentHolding.account_id == account.id)
        .order_by(InvestmentHolding.observed_at.desc())
        .limit(1)
    )
    latest_snapshot = (
        session.scalars(
            select(InvestmentHolding).where(
                InvestmentHolding.account_id == account.id,
                InvestmentHolding.observed_at == latest_observed_at,
            )
        ).all()
        if latest_observed_at is not None
        else []
    )
    previous_by_identifier = {
        row.asset_identifier: row
        for row in latest_snapshot
        if row.asset_identifier
    }

    valid_isins = {
        item["isin"].strip().upper()
        for item in payload
        if isinstance(item.get("isin"), str)
        and _is_valid_isin(item["isin"].strip())
    }
    metadata_by_isin: dict[str, InvestmentHolding] = {}
    if valid_isins:
        metadata_rows = session.scalars(
            select(InvestmentHolding)
            .where(
                InvestmentHolding.account_id == account.id,
                InvestmentHolding.asset_identifier.in_(valid_isins),
            )
            .order_by(InvestmentHolding.observed_at.desc())
        ).all()
        for row in metadata_rows:
            metadata_by_isin.setdefault(row.asset_identifier, row)

    holdings = build_holding_rows(
        payload,
        account.id,
        observed_at,
        metadata_by_isin,
    )
    api_by_identifier = {row.asset_identifier: row for row in holdings}
    prior_ids = set(previous_by_identifier)
    api_ids = set(api_by_identifier)
    shared_ids = prior_ids & api_ids

    inferred_from_symbol_only = sum(
        1
        for row in holdings
        if row.asset_identifier not in prior_ids
        and not _is_valid_isin(row.asset_identifier)
    )
    return DhanHoldingsPlan(
        holdings=holdings,
        fetched=len(payload),
        to_insert=sum(row.quantity > 0 for row in holdings),
        skipped_zero_qty=sum(row.quantity == 0 for row in holdings),
        same_quantity=sum(
            api_by_identifier[key].quantity == previous_by_identifier[key].quantity
            for key in shared_ids
        ),
        different_quantity=sum(
            api_by_identifier[key].quantity != previous_by_identifier[key].quantity
            for key in shared_ids
        ),
        new_in_api=len(api_ids - prior_ids),
        absent_from_api=len(prior_ids - api_ids),
        inferred_from_symbol_only=inferred_from_symbol_only,
        without_isin=sum(
            not _is_valid_isin(row.asset_identifier) for row in holdings
        ),
    )


def persist_dhan_holdings(
    session: Session,
    account: FinancialAccount,
    payload: list[dict[str, Any]],
) -> dict[str, int]:
    plan = analyze_dhan_holdings(session, account, payload)
    holdings_to_insert = [row for row in plan.holdings if row.quantity > 0]
    observed_at = (
        holdings_to_insert[0].observed_at
        if holdings_to_insert
        else datetime.now(timezone.utc)
    )
    session.add_all(holdings_to_insert)
    session.add(
        DataSyncRun(
            connection_id=account.connection_id,
            sync_type="holdings_only",
            status="completed",
            started_at=observed_at,
            completed_at=datetime.now(timezone.utc),
            records_processed=len(holdings_to_insert),
            error_message=None,
        )
    )
    session.commit()
    return plan.counts()
