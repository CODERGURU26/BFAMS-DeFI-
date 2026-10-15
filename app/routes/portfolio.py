"""
Portfolio routes — Dhan demat holdings & trades ingestion + valuation.

All write endpoints require an `account_id` query parameter pointing to an
existing financial_accounts row in the test database.  They are idempotent:
re-running the same CSV never creates duplicates.

Endpoints
---------
POST /portfolio/ingest/holdings
    Upload a Dhan Holdings CSV (multipart file or raw body).
    Query params: account_id (uuid), statement_date (YYYY-MM-DD), dry_run (bool)

POST /portfolio/ingest/trades
    Upload a Dhan Past Trades CSV.
    Query params: account_id (uuid), dry_run (bool)

GET  /portfolio/holdings/{account_id}
    List latest investment_holdings snapshot for the account.

GET  /portfolio/valuation/{account_id}
    Return per-holding and aggregate market value using NSE prices / MF NAVs.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.dependencies import get_mf_db
from app.models import FinancialAccount, InvestmentHolding
from app.demat_ingestion import (
    IngestionSummary,
    RowError,
    ingest_demat_holdings,
    ingest_demat_transactions,
    get_portfolio_valuation,
    parse_dhan_holdings_csv,
    parse_dhan_trades_csv,
    PortfolioSummary,
    PortfolioHoldingValuation,
)

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RowErrorSchema(BaseModel):
    row_number: int
    field: Optional[str]
    raw_value: Optional[str]
    reason: str


class IngestionResponse(BaseModel):
    mode: str
    dry_run: bool
    total_rows: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    errors: List[RowErrorSchema]


class HoldingRow(BaseModel):
    id: str
    asset_name: str
    asset_identifier: Optional[str]
    asset_type: str
    quantity: Optional[str]
    unit_price: Optional[str]
    market_value: Optional[str]
    currency: str
    observed_at: str


class HoldingValuationRow(BaseModel):
    asset_name: str
    asset_identifier: Optional[str]
    asset_type: str
    quantity: str
    close_price: Optional[str]
    price_date: Optional[str]
    market_value: Optional[str]
    currency: str
    data_age_days: Optional[int]
    is_stale: bool
    status: str
    notes: Optional[str]


class PortfolioValuationResponse(BaseModel):
    account_id: str
    as_of_date: str
    total_holdings: int
    valued_count: int
    stale_count: int
    no_price_count: int
    total_market_value: Optional[str]
    currency: str
    holdings: List[HoldingValuationRow]


# ---------------------------------------------------------------------------
# Helper: resolve & verify account
# ---------------------------------------------------------------------------


def _get_account(session: Session, account_id: uuid.UUID) -> FinancialAccount:
    """Raise 404 if account does not exist."""
    acc = session.get(FinancialAccount, account_id)
    if acc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Financial account {account_id} not found.",
        )
    return acc


def _verify_test_db(session: Session) -> None:
    """Abort if not connected to the test database."""
    db_name = session.execute(text("SELECT current_database()")).scalar()
    if db_name != "BFAMS-DeFI-test":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Safety check failed: connected to '{db_name}', expected 'BFAMS-DeFI-test'.",
        )


def _summary_to_response(summary: IngestionSummary) -> IngestionResponse:
    return IngestionResponse(
        mode=summary.mode,
        dry_run=summary.dry_run,
        total_rows=summary.total_rows,
        accepted_count=summary.accepted_count,
        rejected_count=summary.rejected_count,
        duplicate_count=summary.duplicate_count,
        errors=[
            RowErrorSchema(
                row_number=e.row_number,
                field=e.field,
                raw_value=str(e.raw_value) if e.raw_value is not None else None,
                reason=e.reason,
            )
            for e in summary.errors
        ],
    )


# ---------------------------------------------------------------------------
# POST /portfolio/ingest/holdings
# ---------------------------------------------------------------------------


@router.post(
    "/ingest/holdings",
    response_model=IngestionResponse,
    summary="Ingest Dhan demat holdings snapshot",
    description=(
        "Upload a Dhan Holdings CSV file.  "
        "Strips metadata header rows automatically. "
        "Pass dry_run=true to validate without writing to the database."
    ),
)
async def ingest_holdings(
    account_id: uuid.UUID = Query(..., description="Target financial account UUID"),
    statement_date: Optional[date] = Query(
        None, description="Holdings snapshot date (YYYY-MM-DD); defaults to today"
    ),
    dry_run: bool = Query(False, description="Validate without writing to database"),
    file: UploadFile = File(..., description="Dhan Holdings CSV file"),
    session: Session = Depends(get_mf_db),
) -> IngestionResponse:
    _verify_test_db(session)
    _get_account(session, account_id)

    csv_bytes = await file.read()
    try:
        csv_text = csv_bytes.decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        csv_text = csv_bytes.decode("latin-1")

    summary = ingest_demat_holdings(
        session=session,
        account_id=account_id,
        csv_text=csv_text,
        statement_date=statement_date,
        dry_run=dry_run,
        parser="dhan",
    )
    return _summary_to_response(summary)


# ---------------------------------------------------------------------------
# POST /portfolio/ingest/trades
# ---------------------------------------------------------------------------


@router.post(
    "/ingest/trades",
    response_model=IngestionResponse,
    summary="Ingest Dhan past trades",
    description=(
        "Upload a Dhan Past Trades CSV file. "
        "Each row is stored as a transaction (BUY->debit, SELL->credit). "
        "Idempotent: duplicate rows are detected and skipped."
    ),
)
async def ingest_trades(
    account_id: uuid.UUID = Query(..., description="Target financial account UUID"),
    dry_run: bool = Query(False, description="Validate without writing to database"),
    file: UploadFile = File(..., description="Dhan Past Trades CSV file"),
    session: Session = Depends(get_mf_db),
) -> IngestionResponse:
    _verify_test_db(session)
    _get_account(session, account_id)

    csv_bytes = await file.read()
    try:
        csv_text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        csv_text = csv_bytes.decode("latin-1")

    summary = ingest_demat_transactions(
        session=session,
        account_id=account_id,
        csv_text=csv_text,
        dry_run=dry_run,
        parser="dhan",
    )
    return _summary_to_response(summary)


# ---------------------------------------------------------------------------
# GET /portfolio/holdings/{account_id}
# ---------------------------------------------------------------------------


@router.get(
    "/holdings/{account_id}",
    response_model=List[HoldingRow],
    summary="List latest holdings snapshot",
    description="Returns all investment_holdings rows at the most recent observed_at timestamp.",
)
def get_holdings(
    account_id: uuid.UUID,
    session: Session = Depends(get_mf_db),
) -> List[HoldingRow]:
    _verify_test_db(session)
    _get_account(session, account_id)

    # Find most recent snapshot timestamp
    latest_ts = session.scalars(
        select(InvestmentHolding.observed_at)
        .where(InvestmentHolding.account_id == account_id)
        .order_by(InvestmentHolding.observed_at.desc())
        .limit(1)
    ).first()

    if latest_ts is None:
        return []

    rows = session.scalars(
        select(InvestmentHolding)
        .where(
            InvestmentHolding.account_id == account_id,
            InvestmentHolding.observed_at == latest_ts,
        )
        .order_by(InvestmentHolding.asset_name)
    ).all()

    return [
        HoldingRow(
            id=str(h.id),
            asset_name=h.asset_name,
            asset_identifier=h.asset_identifier,
            asset_type=h.asset_type,
            quantity=str(h.quantity) if h.quantity is not None else None,
            unit_price=str(h.unit_price) if h.unit_price is not None else None,
            market_value=str(h.market_value) if h.market_value is not None else None,
            currency=h.currency or "INR",
            observed_at=h.observed_at.isoformat(),
        )
        for h in rows
    ]


# ---------------------------------------------------------------------------
# GET /portfolio/valuation/{account_id}
# ---------------------------------------------------------------------------


@router.get(
    "/valuation/{account_id}",
    response_model=PortfolioValuationResponse,
    summary="Portfolio market valuation",
    description=(
        "Values each holding in the latest snapshot using NSE close prices "
        "(security_daily_prices) with Dhan snapshot price as fallback. "
        "Returns per-holding and total market value."
    ),
)
def get_valuation(
    account_id: uuid.UUID,
    as_of_date: Optional[date] = Query(
        None,
        description="Valuation date (YYYY-MM-DD); defaults to today",
    ),
    max_staleness_days: int = Query(
        7,
        ge=1,
        le=30,
        description="Maximum acceptable price age in days",
    ),
    session: Session = Depends(get_mf_db),
) -> PortfolioValuationResponse:
    _verify_test_db(session)
    _get_account(session, account_id)

    summary = get_portfolio_valuation(
        session=session,
        account_id=account_id,
        as_of_date=as_of_date,
        max_staleness_days=max_staleness_days,
    )

    return PortfolioValuationResponse(
        account_id=str(summary.account_id),
        as_of_date=str(summary.as_of_date),
        total_holdings=summary.total_holdings,
        valued_count=summary.valued_count,
        stale_count=summary.stale_count,
        no_price_count=summary.no_price_count,
        total_market_value=(
            str(summary.total_market_value)
            if summary.total_market_value is not None
            else None
        ),
        currency=summary.currency,
        holdings=[
            HoldingValuationRow(
                asset_name=h.asset_name,
                asset_identifier=h.asset_identifier,
                asset_type=h.asset_type,
                quantity=str(h.quantity),
                close_price=str(h.close_price) if h.close_price is not None else None,
                price_date=str(h.price_date) if h.price_date is not None else None,
                market_value=str(h.market_value) if h.market_value is not None else None,
                currency=h.currency,
                data_age_days=h.data_age_days,
                is_stale=h.is_stale,
                status=h.status,
                notes=h.notes,
            )
            for h in summary.holdings
        ],
    )
