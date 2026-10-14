from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional, Union
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MfNav, MfScheme, InvestmentHolding
from app.mf_resolver import resolve_mf_scheme


class ValuationStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    MISSING_NAV = "missing_nav"
    ZERO_NAV = "zero_nav"
    ZERO_QUANTITY = "zero_quantity"
    INVALID_INPUT = "invalid_input"


@dataclass
class HoldingValuation:
    scheme_id: Optional[uuid.UUID]
    scheme_code: Optional[str]
    scheme_name: Optional[str]
    quantity: Decimal
    nav: Optional[Decimal]
    nav_date: Optional[date]
    as_of_date: date
    market_value: Optional[Decimal]
    currency: str
    data_age_days: Optional[int]
    max_staleness_days: int
    is_stale: bool
    status: ValuationStatus
    notes: Optional[str] = None

    @property
    def is_reliable(self) -> bool:
        """True if valuation has a current, non-stale positive NAV."""
        return self.status == ValuationStatus.CURRENT and self.market_value is not None


def value_mf_holding(
    session: Session,
    scheme: Union[MfScheme, uuid.UUID, str],
    quantity: Union[Decimal, float, int, str],
    as_of_date: Optional[date] = None,
    max_staleness_days: int = 7,
    currency: str = "INR",
) -> HoldingValuation:
    """
    Read-only mutual fund holding valuation service.
    
    - Retrieves latest valid NAV on or before requested valuation date.
    - Multiplies units by NAV using Decimal-safe arithmetic.
    - Evaluates data age against max_staleness_days threshold.
    - Never mutates database records.
    """
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()

    # Convert quantity to Decimal safely
    try:
        dec_qty = Decimal(str(quantity))
    except Exception as exc:
        return HoldingValuation(
            scheme_id=None,
            scheme_code=None,
            scheme_name=None,
            quantity=Decimal("0"),
            nav=None,
            nav_date=None,
            as_of_date=as_of_date,
            market_value=None,
            currency=currency,
            data_age_days=None,
            max_staleness_days=max_staleness_days,
            is_stale=False,
            status=ValuationStatus.INVALID_INPUT,
            notes=f"Invalid quantity: {exc}",
        )

    # Resolve scheme object
    scheme_obj = None
    if isinstance(scheme, MfScheme):
        scheme_obj = scheme
    elif isinstance(scheme, uuid.UUID):
        scheme_obj = session.get(MfScheme, scheme)
    elif isinstance(scheme, str):
        # Could be scheme_code or string UUID
        try:
            val_uuid = uuid.UUID(scheme)
            scheme_obj = session.get(MfScheme, val_uuid)
        except ValueError:
            scheme_obj = session.scalars(
                select(MfScheme).where(MfScheme.scheme_code == scheme.strip())
            ).first()

    if scheme_obj is None:
        return HoldingValuation(
            scheme_id=None,
            scheme_code=None,
            scheme_name=None,
            quantity=dec_qty,
            nav=None,
            nav_date=None,
            as_of_date=as_of_date,
            market_value=None,
            currency=currency,
            data_age_days=None,
            max_staleness_days=max_staleness_days,
            is_stale=False,
            status=ValuationStatus.INVALID_INPUT,
            notes=f"Mutual fund scheme not found: {scheme}",
        )

    if dec_qty == Decimal("0"):
        return HoldingValuation(
            scheme_id=scheme_obj.id,
            scheme_code=scheme_obj.scheme_code,
            scheme_name=scheme_obj.scheme_name,
            quantity=Decimal("0"),
            nav=None,
            nav_date=None,
            as_of_date=as_of_date,
            market_value=Decimal("0.0000"),
            currency=currency,
            data_age_days=None,
            max_staleness_days=max_staleness_days,
            is_stale=False,
            status=ValuationStatus.ZERO_QUANTITY,
            notes="Holding quantity is zero.",
        )

    # Query latest NAV on or before as_of_date
    latest_nav_record = session.scalars(
        select(MfNav)
        .where(
            MfNav.scheme_id == scheme_obj.id,
            MfNav.nav_date <= as_of_date,
        )
        .order_by(MfNav.nav_date.desc())
        .limit(1)
    ).first()

    if latest_nav_record is None:
        return HoldingValuation(
            scheme_id=scheme_obj.id,
            scheme_code=scheme_obj.scheme_code,
            scheme_name=scheme_obj.scheme_name,
            quantity=dec_qty,
            nav=None,
            nav_date=None,
            as_of_date=as_of_date,
            market_value=None,
            currency=currency,
            data_age_days=None,
            max_staleness_days=max_staleness_days,
            is_stale=False,
            status=ValuationStatus.MISSING_NAV,
            notes=f"No NAV records found on or before {as_of_date}",
        )

    nav_val = latest_nav_record.nav
    nav_dt = latest_nav_record.nav_date
    age_days = (as_of_date - nav_dt).days

    if nav_val <= Decimal("0"):
        return HoldingValuation(
            scheme_id=scheme_obj.id,
            scheme_code=scheme_obj.scheme_code,
            scheme_name=scheme_obj.scheme_name,
            quantity=dec_qty,
            nav=nav_val,
            nav_date=nav_dt,
            as_of_date=as_of_date,
            market_value=Decimal("0.0000"),
            currency=currency,
            data_age_days=age_days,
            max_staleness_days=max_staleness_days,
            is_stale=age_days > max_staleness_days,
            status=ValuationStatus.ZERO_NAV,
            notes=f"Latest NAV on {nav_dt} is zero or negative ({nav_val})",
        )

    # Decimal-safe calculation: quantity * nav
    raw_mv = dec_qty * nav_val
    market_val = raw_mv.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    is_stale = age_days > max_staleness_days

    status = ValuationStatus.STALE if is_stale else ValuationStatus.CURRENT
    notes = (
        f"NAV is stale ({age_days} days old, threshold is {max_staleness_days} days)"
        if is_stale
        else None
    )

    return HoldingValuation(
        scheme_id=scheme_obj.id,
        scheme_code=scheme_obj.scheme_code,
        scheme_name=scheme_obj.scheme_name,
        quantity=dec_qty,
        nav=nav_val,
        nav_date=nav_dt,
        as_of_date=as_of_date,
        market_value=market_val,
        currency=currency,
        data_age_days=age_days,
        max_staleness_days=max_staleness_days,
        is_stale=is_stale,
        status=status,
        notes=notes,
    )


def value_investment_holding_record(
    session: Session,
    holding: InvestmentHolding,
    as_of_date: Optional[date] = None,
    max_staleness_days: int = 7,
) -> HoldingValuation:
    """
    Values an InvestmentHolding ORM instance. If holding.mf_scheme_id is unset,
    attempts deterministic resolution via asset_identifier or asset_name.
    """
    scheme_id = holding.mf_scheme_id

    if not scheme_id and holding.asset_identifier:
        res = resolve_mf_scheme(session, identifier=holding.asset_identifier)
        if res.is_resolved:
            scheme_id = res.scheme_id

    if not scheme_id:
        return HoldingValuation(
            scheme_id=None,
            scheme_code=None,
            scheme_name=None,
            quantity=holding.quantity or Decimal("0"),
            nav=None,
            nav_date=None,
            as_of_date=as_of_date or datetime.now(timezone.utc).date(),
            market_value=None,
            currency=holding.currency or "INR",
            data_age_days=None,
            max_staleness_days=max_staleness_days,
            is_stale=False,
            status=ValuationStatus.INVALID_INPUT,
            notes="Holding is not linked to an MF scheme and identifier could not be resolved.",
        )

    return value_mf_holding(
        session=session,
        scheme=scheme_id,
        quantity=holding.quantity or Decimal("0"),
        as_of_date=as_of_date,
        max_staleness_days=max_staleness_days,
        currency=holding.currency or "INR",
    )
