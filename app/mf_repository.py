"""
app/mf_repository.py
====================
Read-only database queries and access layer for Mutual Fund Analytics.

Safety guarantees:
1. Verifies current_database() == settings.test_db_name before every query operation.
2. Read-only: Never performs INSERT, UPDATE, DELETE, or DDL.
3. Indexes utilized: Uses MfScheme.scheme_code (unique index) and
   MfNav.scheme_id + nav_date (composite primary key index).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from database import settings
from app.models import MfNav, MfScheme
from app.mf_analytics import (
    DrawdownResult,
    InsufficientDataError,
    InvalidDateRangeError,
    NavPoint,
    ReturnResult,
    RollingReturnsSummary,
    StaleDataError,
    calculate_max_drawdown,
    calculate_point_to_point,
    calculate_rolling_returns,
)


def verify_test_db(session: Session) -> None:
    """
    Enforce safety requirement: verify connected database is strictly BFAMS-DeFI-test.
    Aborts immediately if connected to any other database.
    """
    current_db = session.execute(text("SELECT current_database()")).scalar()
    expected_db = settings.test_db_name or "BFAMS-DeFI-test"
    if current_db != expected_db:
        raise RuntimeError(
            f"SAFETY ABORT: Connected to database '{current_db}', expected '{expected_db}'. "
            "Refusing to execute queries."
        )


def search_schemes(
    session: Session,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """
    Search schemes by scheme_code (exact/prefix) or scheme_name (case-insensitive substring).
    Returns (results, total_count).
    """
    verify_test_db(session)

    stmt = select(MfScheme)
    count_stmt = select(func.count()).select_from(MfScheme)

    if query and query.strip():
        q = query.strip()
        if q.isdigit():
            # Numeric query: match scheme_code prefix or exact
            filt = MfScheme.scheme_code.startswith(q)
        else:
            filt = MfScheme.scheme_name.ilike(f"%{q}%")
        stmt = stmt.where(filt)
        count_stmt = count_stmt.where(filt)

    total = session.scalar(count_stmt) or 0
    schemes = (
        session.scalars(
            stmt.order_by(MfScheme.scheme_name, MfScheme.scheme_code)
            .limit(limit)
            .offset(offset)
        ).all()
    )

    results = [
        {
            "id": str(s.id),
            "scheme_code": s.scheme_code,
            "scheme_name": s.scheme_name,
            "isin_primary": s.isin_primary,
            "isin_reinvestment": s.isin_reinvestment,
            "plan": s.plan,
            "option_name": s.option_name,
            "amc_name": s.amc_name,
            "scheme_type": s.scheme_type,
            "scheme_category": s.scheme_category,
        }
        for s in schemes
    ]
    return results, total


def get_scheme_by_code(session: Session, scheme_code: str) -> MfScheme:
    """
    Fetch single scheme by scheme_code. Raises InsufficientDataError if not found.
    """
    verify_test_db(session)
    scheme = session.scalars(
        select(MfScheme).where(MfScheme.scheme_code == scheme_code.strip())
    ).first()
    if scheme is None:
        raise InsufficientDataError(f"Scheme code '{scheme_code}' not found in database")
    return scheme


def get_nav_history(
    session: Session,
    scheme_code: str,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
) -> list[NavPoint]:
    """
    Fetch chronological NAV series for a scheme, ordered by nav_date ASC.
    """
    verify_test_db(session)
    scheme = get_scheme_by_code(session, scheme_code)

    stmt = (
        select(MfNav.nav_date, MfNav.nav)
        .where(MfNav.scheme_id == scheme.id)
        .order_by(MfNav.nav_date.asc())
    )

    if start_date is not None:
        stmt = stmt.where(MfNav.nav_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(MfNav.nav_date <= end_date)
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = session.execute(stmt).all()
    return [NavPoint(nav_date=r[0], nav=Decimal(str(r[1]))) for r in rows]


def get_nav_on_or_before(
    session: Session,
    scheme_id: uuid.UUID,
    target_date: date,
    max_staleness_days: int = 7,
) -> NavPoint:
    """
    Fetch latest NAV on or before target_date for a given scheme_id.
    Raises:
        InsufficientDataError: If no NAV observation exists on or before target_date.
        StaleDataError: If the latest NAV is older than max_staleness_days.
    """
    verify_test_db(session)
    row = session.execute(
        select(MfNav.nav_date, MfNav.nav)
        .where(MfNav.scheme_id == scheme_id, MfNav.nav_date <= target_date)
        .order_by(MfNav.nav_date.desc())
        .limit(1)
    ).first()

    if row is None:
        raise InsufficientDataError(
            f"No NAV observations found on or before {target_date}"
        )

    nav_date, nav_val = row
    staleness = (target_date - nav_date).days
    if staleness > max_staleness_days:
        raise StaleDataError(
            f"Latest available NAV is on {nav_date} ({staleness} days before requested {target_date}), "
            f"which exceeds the maximum allowed staleness of {max_staleness_days} days"
        )

    return NavPoint(nav_date=nav_date, nav=Decimal(str(nav_val)))


def get_scheme_returns(
    session: Session,
    scheme_code: str,
    start_date: date,
    end_date: date,
    max_staleness_days: int = 7,
) -> ReturnResult:
    """
    Compute point-to-point return and CAGR between two requested dates using staleness rules.
    """
    verify_test_db(session)
    if start_date >= end_date:
        raise InvalidDateRangeError(
            f"Requested start_date ({start_date}) must be before end_date ({end_date})"
        )

    scheme = get_scheme_by_code(session, scheme_code)

    start_point = get_nav_on_or_before(
        session, scheme.id, start_date, max_staleness_days=max_staleness_days
    )
    end_point = get_nav_on_or_before(
        session, scheme.id, end_date, max_staleness_days=max_staleness_days
    )

    return calculate_point_to_point(
        start_point=start_point,
        end_point=end_point,
        start_date_requested=start_date,
        end_date_requested=end_date,
    )


def get_scheme_drawdown(
    session: Session,
    scheme_code: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> DrawdownResult:
    """
    Compute maximum drawdown for a scheme over the requested date range.
    """
    verify_test_db(session)
    if start_date and end_date and start_date >= end_date:
        raise InvalidDateRangeError(f"start_date ({start_date}) must be before end_date ({end_date})")

    nav_series = get_nav_history(session, scheme_code, start_date=start_date, end_date=end_date)
    return calculate_max_drawdown(nav_series)


def get_scheme_rolling_returns(
    session: Session,
    scheme_code: str,
    start_date: date | None = None,
    end_date: date | None = None,
    window_days: int = 365,
    step_days: int = 30,
    max_staleness_days: int = 7,
) -> RollingReturnsSummary:
    """
    Compute rolling returns across the scheme's history.
    """
    verify_test_db(session)
    nav_series = get_nav_history(session, scheme_code, start_date=start_date, end_date=end_date)
    return calculate_rolling_returns(
        nav_series=nav_series,
        window_days=window_days,
        step_days=step_days,
        max_staleness_days=max_staleness_days,
    )


def compare_schemes(
    session: Session,
    scheme_codes: list[str],
    start_date: date,
    end_date: date,
    max_staleness_days: int = 7,
) -> dict[str, Any]:
    """
    Compare multiple schemes over the same period.
    Returns per-scheme performance metrics and isolated error reporting if a scheme lacks data.
    """
    verify_test_db(session)
    if start_date >= end_date:
        raise InvalidDateRangeError(f"start_date ({start_date}) must be before end_date ({end_date})")

    results = []
    for code in scheme_codes:
        code_clean = code.strip()
        try:
            scheme = get_scheme_by_code(session, code_clean)
            ret = get_scheme_returns(
                session, code_clean, start_date, end_date, max_staleness_days=max_staleness_days
            )
            dd = get_scheme_drawdown(
                session, code_clean, start_date=start_date, end_date=end_date
            )
            results.append({
                "scheme_code": scheme.scheme_code,
                "scheme_name": scheme.scheme_name,
                "amc_name": scheme.amc_name,
                "status": "success",
                "start_date_actual": ret.start_date_actual.isoformat(),
                "start_nav": str(ret.start_nav),
                "end_date_actual": ret.end_date_actual.isoformat(),
                "end_nav": str(ret.end_nav),
                "elapsed_days": ret.elapsed_days,
                "absolute_return_pct": str(ret.absolute_return_pct),
                "cagr_pct": str(ret.cagr_pct) if ret.cagr_pct is not None else None,
                "annualized": ret.annualized,
                "max_drawdown_pct": str(dd.max_drawdown_pct),
                "peak_date": dd.peak_date.isoformat(),
                "trough_date": dd.trough_date.isoformat(),
                "recovery_date": dd.recovery_date.isoformat() if dd.recovery_date else None,
                "total_observations": dd.total_observations,
            })
        except Exception as exc:
            results.append({
                "scheme_code": code_clean,
                "status": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })

    return {
        "start_date_requested": start_date.isoformat(),
        "end_date_requested": end_date.isoformat(),
        "schemes_evaluated": len(scheme_codes),
        "results": results,
    }
