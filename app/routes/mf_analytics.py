"""
app/routes/mf_analytics.py
==========================
FastAPI routes for Mutual Fund Analytics.

Endpoints:
  GET  /mf/schemes                         - Search mutual fund schemes
  GET  /mf/schemes/{scheme_code}           - Scheme details
  GET  /mf/schemes/{scheme_code}/navs      - Historical NAV series
  GET  /mf/schemes/{scheme_code}/returns   - Point-to-point return and CAGR
  GET  /mf/schemes/{scheme_code}/rolling-returns - Rolling returns analysis
  GET  /mf/schemes/{scheme_code}/drawdown  - Maximum drawdown analysis
  POST /mf/compare                         - Cross-scheme performance comparison
"""

from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.dependencies import get_mf_db
from app.mf_analytics import (
    AnalyticsError,
    InsufficientDataError,
    InvalidDateRangeError,
    NonPositiveNavError,
    StaleDataError,
)
from app.mf_repository import (
    compare_schemes,
    get_nav_history,
    get_scheme_by_code,
    get_scheme_drawdown,
    get_scheme_returns,
    get_scheme_rolling_returns,
    search_schemes,
)
from app.mf_schemas import (
    CompareRequest,
    CompareResponse,
    DrawdownResponse,
    NavHistoryResponse,
    NavPointResponse,
    ReturnsResponse,
    RollingReturnPointResponse,
    RollingReturnsResponse,
    SchemeSearchResponse,
    SchemeSummary,
)

router = APIRouter(prefix="/mf", tags=["Mutual Fund Analytics"])


@router.get("/schemes", response_model=SchemeSearchResponse)
def list_or_search_schemes(
    q: str | None = Query(default=None, description="Search query by scheme code or scheme name"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_mf_db),
):
    """Search mutual fund schemes by scheme code or name."""
    results, total = search_schemes(db, query=q, limit=limit, offset=offset)
    return SchemeSearchResponse(
        total=total,
        limit=limit,
        offset=offset,
        schemes=[SchemeSummary(**s) for s in results],
    )


@router.get("/schemes/{scheme_code}", response_model=SchemeSummary)
def get_scheme_details(
    scheme_code: str,
    db: Session = Depends(get_mf_db),
):
    """Get metadata for a specific scheme code."""
    try:
        s = get_scheme_by_code(db, scheme_code)
        return SchemeSummary(
            id=str(s.id),
            scheme_code=s.scheme_code,
            scheme_name=s.scheme_name,
            isin_primary=s.isin_primary,
            isin_reinvestment=s.isin_reinvestment,
            plan=s.plan,
            option_name=s.option_name,
            amc_name=s.amc_name,
            scheme_type=s.scheme_type,
            scheme_category=s.scheme_category,
        )
    except InsufficientDataError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/schemes/{scheme_code}/navs", response_model=NavHistoryResponse)
def get_scheme_nav_history(
    scheme_code: str,
    start_date: date | None = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: date | None = Query(default=None, description="End date (YYYY-MM-DD)"),
    limit: int | None = Query(default=None, ge=1, le=5000),
    db: Session = Depends(get_mf_db),
):
    """Retrieve chronological NAV observations for a scheme."""
    try:
        scheme = get_scheme_by_code(db, scheme_code)
        pts = get_nav_history(
            db, scheme_code, start_date=start_date, end_date=end_date, limit=limit
        )
        return NavHistoryResponse(
            scheme_code=scheme.scheme_code,
            scheme_name=scheme.scheme_name,
            total_observations=len(pts),
            navs=[NavPointResponse(nav_date=p.nav_date, nav=p.nav) for p in pts],
        )
    except InsufficientDataError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/schemes/{scheme_code}/returns", response_model=ReturnsResponse)
def get_point_to_point_returns(
    scheme_code: str,
    start_date: date = Query(..., description="Target start date (YYYY-MM-DD)"),
    end_date: date = Query(..., description="Target end date (YYYY-MM-DD)"),
    max_staleness_days: int = Query(
        default=7, ge=0, le=90, description="Max allowed days before target date for NAV"
    ),
    db: Session = Depends(get_mf_db),
):
    """
    Calculate point-to-point absolute return and CAGR between two dates.

    Uses latest observed NAV on or before each requested date subject to max_staleness_days.
    CAGR is computed using Actual/365 day count convention for periods of >= 365 days.
    """
    try:
        scheme = get_scheme_by_code(db, scheme_code)
        ret = get_scheme_returns(
            db, scheme_code, start_date, end_date, max_staleness_days=max_staleness_days
        )
        return ReturnsResponse(
            scheme_code=scheme.scheme_code,
            scheme_name=scheme.scheme_name,
            start_date_requested=ret.start_date_requested,
            start_date_actual=ret.start_date_actual,
            start_nav=ret.start_nav,
            end_date_requested=ret.end_date_requested,
            end_date_actual=ret.end_date_actual,
            end_nav=ret.end_nav,
            elapsed_days=ret.elapsed_days,
            absolute_return_pct=ret.absolute_return_pct,
            cagr_pct=ret.cagr_pct,
            annualized=ret.annualized,
        )
    except InsufficientDataError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (InvalidDateRangeError, NonPositiveNavError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except StaleDataError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/schemes/{scheme_code}/rolling-returns", response_model=RollingReturnsResponse)
def get_rolling_returns(
    scheme_code: str,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    window_days: int = Query(default=365, ge=7, le=3650, description="Window size in days"),
    step_days: int = Query(default=30, ge=1, le=365, description="Step size in days"),
    max_staleness_days: int = Query(default=7, ge=0, le=90),
    db: Session = Depends(get_mf_db),
):
    """
    Calculate rolling returns over the series.

    Annualized CAGR% is returned for windows >= 365 days; absolute return% for shorter windows.
    """
    try:
        scheme = get_scheme_by_code(db, scheme_code)
        summary = get_scheme_rolling_returns(
            db,
            scheme_code,
            start_date=start_date,
            end_date=end_date,
            window_days=window_days,
            step_days=step_days,
            max_staleness_days=max_staleness_days,
        )
        return RollingReturnsResponse(
            scheme_code=scheme.scheme_code,
            scheme_name=scheme.scheme_name,
            window_days=summary.window_days,
            step_days=summary.step_days,
            total_periods=summary.total_periods,
            annualized=summary.annualized,
            mean_pct=summary.mean_pct,
            median_pct=summary.median_pct,
            min_pct=summary.min_pct,
            max_pct=summary.max_pct,
            std_dev_pct=summary.std_dev_pct,
            observations=[
                RollingReturnPointResponse(
                    start_date=o.start_date,
                    end_date=o.end_date,
                    start_nav=o.start_nav,
                    end_nav=o.end_nav,
                    return_pct=o.return_pct,
                )
                for o in summary.observations
            ],
        )
    except InsufficientDataError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (InvalidDateRangeError, NonPositiveNavError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except StaleDataError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/schemes/{scheme_code}/drawdown", response_model=DrawdownResponse)
def get_drawdown(
    scheme_code: str,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_mf_db),
):
    """Calculate maximum drawdown, peak date, trough date, and recovery date."""
    try:
        scheme = get_scheme_by_code(db, scheme_code)
        dd = get_scheme_drawdown(db, scheme_code, start_date=start_date, end_date=end_date)
        return DrawdownResponse(
            scheme_code=scheme.scheme_code,
            scheme_name=scheme.scheme_name,
            max_drawdown_pct=dd.max_drawdown_pct,
            peak_date=dd.peak_date,
            peak_nav=dd.peak_nav,
            trough_date=dd.trough_date,
            trough_nav=dd.trough_nav,
            recovery_date=dd.recovery_date,
            is_recovered=dd.is_recovered,
            total_observations=dd.total_observations,
        )
    except InsufficientDataError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (InvalidDateRangeError, NonPositiveNavError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/compare", response_model=CompareResponse)
def compare_multiple_schemes(
    req: CompareRequest,
    db: Session = Depends(get_mf_db),
):
    """Compare performance across multiple mutual fund schemes over a requested period."""
    try:
        res = compare_schemes(
            db,
            req.scheme_codes,
            req.start_date,
            req.end_date,
            max_staleness_days=req.max_staleness_days,
        )
        return CompareResponse(**res)
    except (InvalidDateRangeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
