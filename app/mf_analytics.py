"""
app/mf_analytics.py
====================
Deterministic Mutual Fund Analytics — pure computation layer.

All functions accept plain Python values (Decimal NAVs, date objects, or sequences)
so they can be thoroughly tested with hand-calculated examples without database dependencies.
Database retrieval is handled separately in app/mf_repository.py.

Annualization convention: Actual/365
Rounding convention:
  - Display returns / CAGR / Drawdown percentages: 4 decimal places (e.g. 14.2531%)
  - Raw ratio returns: Decimal/float with full precision
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import math
from typing import Sequence


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AnalyticsError(Exception):
    """Base exception for mutual fund analytics calculations."""


class InsufficientDataError(AnalyticsError):
    """Raised when there are not enough data points to perform the calculation."""


class InvalidDateRangeError(AnalyticsError):
    """Raised when start_date is on or after end_date."""


class NonPositiveNavError(AnalyticsError):
    """Raised when a zero or negative NAV is encountered in return calculation."""


class StaleDataError(AnalyticsError):
    """Raised when the latest available NAV exceeds the maximum allowed staleness."""


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavPoint:
    """A single date and NAV observation."""
    nav_date: date
    nav: Decimal

    def __post_init__(self):
        if not isinstance(self.nav_date, date):
            raise TypeError(f"nav_date must be a datetime.date, got {type(self.nav_date)}")
        if not isinstance(self.nav, Decimal):
            try:
                object.__setattr__(self, "nav", Decimal(str(self.nav)))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise TypeError(f"nav must be convertible to Decimal: {exc}") from exc


@dataclass(frozen=True)
class ReturnResult:
    """Result of a point-to-point and/or CAGR calculation."""
    start_date_requested: date
    start_date_actual: date
    start_nav: Decimal
    end_date_requested: date
    end_date_actual: date
    end_nav: Decimal
    elapsed_days: int
    absolute_return: Decimal        # (end_nav - start_nav) / start_nav
    absolute_return_pct: Decimal    # absolute_return * 100, rounded to 4 places
    cagr: Decimal | None            # ((end_nav / start_nav) ** (365 / elapsed_days)) - 1
    cagr_pct: Decimal | None        # cagr * 100, rounded to 4 places, or None if elapsed_days < 365
    annualized: bool                # True if CAGR was computed (elapsed_days >= 365)


@dataclass(frozen=True)
class RollingReturnPoint:
    """A single rolling return observation."""
    start_date: date
    end_date: date
    start_nav: Decimal
    end_nav: Decimal
    return_pct: Decimal             # CAGR% if window >= 365 else absolute return%


@dataclass(frozen=True)
class RollingReturnsSummary:
    """Summary statistics for rolling returns across a time series."""
    window_days: int
    step_days: int
    total_periods: int
    annualized: bool
    mean_pct: Decimal
    median_pct: Decimal
    min_pct: Decimal
    max_pct: Decimal
    std_dev_pct: Decimal
    observations: list[RollingReturnPoint]


@dataclass(frozen=True)
class DrawdownResult:
    """Result of maximum drawdown calculation."""
    max_drawdown_pct: Decimal       # e.g. -15.4321%
    peak_date: date
    peak_nav: Decimal
    trough_date: date
    trough_nav: Decimal
    recovery_date: date | None      # First date NAV reached or exceeded peak_nav after trough
    is_recovered: bool
    total_observations: int


# ---------------------------------------------------------------------------
# Core Analytical Functions
# ---------------------------------------------------------------------------


def calculate_point_to_point(
    start_point: NavPoint,
    end_point: NavPoint,
    start_date_requested: date | None = None,
    end_date_requested: date | None = None,
) -> ReturnResult:
    """
    Calculate point-to-point return and CAGR between two observations.

    Formulas:
        Absolute Return = (End NAV - Start NAV) / Start NAV
        Absolute Return % = Absolute Return * 100
        Elapsed Days = (End Date - Start Date).days

        If Elapsed Days >= 365:
            CAGR = (End NAV / Start NAV) ** (365 / Elapsed Days) - 1
            CAGR % = CAGR * 100
        Else:
            CAGR = None (per AMFI & standard financial practice, returns under 1 year are not annualized)

    All results are computed with Decimal arithmetic, rounded to 4 decimal places for percentages.
    """
    if start_point.nav <= Decimal("0"):
        raise NonPositiveNavError(
            f"Start NAV on {start_point.nav_date} is non-positive: {start_point.nav}"
        )
    if end_point.nav <= Decimal("0"):
        raise NonPositiveNavError(
            f"End NAV on {end_point.nav_date} is non-positive: {end_point.nav}"
        )
    if start_point.nav_date >= end_point.nav_date:
        raise InvalidDateRangeError(
            f"Start date ({start_point.nav_date}) must be strictly before end date ({end_point.nav_date})"
        )

    elapsed_days = (end_point.nav_date - start_point.nav_date).days
    if elapsed_days <= 0:
        raise InvalidDateRangeError(f"Elapsed days must be > 0, got {elapsed_days}")

    # Absolute return
    abs_return = (end_point.nav - start_point.nav) / start_point.nav
    abs_return_pct = (abs_return * Decimal("100")).quantize(Decimal("0.0001"))

    # CAGR: Only for periods of at least 365 calendar days
    cagr: Decimal | None = None
    cagr_pct: Decimal | None = None
    annualized = False

    if elapsed_days >= 365:
        # High precision calculation for exponent
        ratio = float(end_point.nav / start_point.nav)
        exponent = 365.0 / elapsed_days
        cagr_val = math.pow(ratio, exponent) - 1.0
        cagr = Decimal(str(round(cagr_val, 8)))
        cagr_pct = Decimal(str(round(cagr_val * 100.0, 4))).quantize(Decimal("0.0001"))
        annualized = True

    return ReturnResult(
        start_date_requested=start_date_requested or start_point.nav_date,
        start_date_actual=start_point.nav_date,
        start_nav=start_point.nav,
        end_date_requested=end_date_requested or end_point.nav_date,
        end_date_actual=end_point.nav_date,
        end_nav=end_point.nav,
        elapsed_days=elapsed_days,
        absolute_return=abs_return,
        absolute_return_pct=abs_return_pct,
        cagr=cagr,
        cagr_pct=cagr_pct,
        annualized=annualized,
    )


def calculate_max_drawdown(nav_series: Sequence[NavPoint]) -> DrawdownResult:
    """
    Calculate maximum peak-to-trough drawdown from an observed time series of NAVs.

    The series must be sorted in ascending chronological order.

    Algorithm:
        1. Maintain the running peak NAV and peak date.
        2. At each observation, calculate drawdown = (current_nav - running_peak) / running_peak.
        3. Identify the minimum drawdown across all observations.
        4. Track the peak date associated with that trough and the subsequent recovery date (if any).
    """
    if not nav_series or len(nav_series) < 2:
        raise InsufficientDataError("Maximum drawdown requires at least 2 NAV observations")

    # Validate sort order and positive NAVs
    for i in range(len(nav_series) - 1):
        if nav_series[i].nav_date >= nav_series[i + 1].nav_date:
            raise InvalidDateRangeError("NAV series must be in strictly ascending chronological order")

    running_peak_nav = nav_series[0].nav
    running_peak_date = nav_series[0].nav_date

    max_dd = Decimal("0")
    trough_date = nav_series[0].nav_date
    trough_nav = nav_series[0].nav
    peak_for_max_dd_nav = nav_series[0].nav
    peak_for_max_dd_date = nav_series[0].nav_date

    for pt in nav_series:
        if pt.nav <= Decimal("0"):
            raise NonPositiveNavError(f"Non-positive NAV encountered on {pt.nav_date}: {pt.nav}")

        if pt.nav > running_peak_nav:
            running_peak_nav = pt.nav
            running_peak_date = pt.nav_date
        else:
            # Drawdown relative to current running peak
            dd = (pt.nav - running_peak_nav) / running_peak_nav
            if dd < max_dd:
                max_dd = dd
                trough_date = pt.nav_date
                trough_nav = pt.nav
                peak_for_max_dd_nav = running_peak_nav
                peak_for_max_dd_date = running_peak_date

    # Find recovery date: first point after trough_date where NAV >= peak_for_max_dd_nav
    recovery_date: date | None = None
    if max_dd < Decimal("0"):
        trough_idx = next(i for i, pt in enumerate(nav_series) if pt.nav_date == trough_date)
        for pt in nav_series[trough_idx + 1 :]:
            if pt.nav >= peak_for_max_dd_nav:
                recovery_date = pt.nav_date
                break

    max_dd_pct = (max_dd * Decimal("100")).quantize(Decimal("0.0001"))

    return DrawdownResult(
        max_drawdown_pct=max_dd_pct,
        peak_date=peak_for_max_dd_date,
        peak_nav=peak_for_max_dd_nav,
        trough_date=trough_date,
        trough_nav=trough_nav,
        recovery_date=recovery_date,
        is_recovered=(recovery_date is not None),
        total_observations=len(nav_series),
    )


def calculate_rolling_returns(
    nav_series: Sequence[NavPoint],
    window_days: int = 365,
    step_days: int = 30,
    max_staleness_days: int = 7,
) -> RollingReturnsSummary:
    """
    Calculate rolling returns across a chronologically sorted NAV series.

    Parameters:
        nav_series: Chronologically sorted list of NavPoint.
        window_days: Calendar day window for each rolling period (default: 365 days / 1 year).
        step_days: Step size in calendar days between successive rolling start dates (default: 30 days).
        max_staleness_days: Maximum calendar days allowed between target rolling date and nearest observed NAV.

    Rules:
        - window_days must be >= 7.
        - step_days must be >= 1.
        - If window_days >= 365, returns are annualized (CAGR%).
        - If window_days < 365, returns are absolute percentage returns.
        - For each window start date T_start, the target end date is T_start + window_days.
        - A valid observation on or before the target date within max_staleness_days must exist.
    """
    if window_days < 7:
        raise ValueError(f"window_days must be at least 7, got {window_days}")
    if step_days < 1:
        raise ValueError(f"step_days must be at least 1, got {step_days}")
    if not nav_series or len(nav_series) < 2:
        raise InsufficientDataError("Rolling returns require at least 2 NAV observations")

    first_date = nav_series[0].nav_date
    last_date = nav_series[-1].nav_date
    total_span = (last_date - first_date).days

    if total_span < window_days:
        raise InsufficientDataError(
            f"Series span of {total_span} days is less than requested window of {window_days} days"
        )

    # Fast binary search / dictionary lookup for latest NAV on or before target date
    dates = [pt.nav_date for pt in nav_series]
    navs_by_date = {pt.nav_date: pt.nav for pt in nav_series}

    import bisect

    def get_latest_nav_on_or_before(target: date) -> tuple[date, Decimal] | None:
        idx = bisect.bisect_right(dates, target) - 1
        if idx < 0:
            return None
        obs_date = dates[idx]
        if (target - obs_date).days > max_staleness_days:
            return None
        return obs_date, navs_by_date[obs_date]

    observations: list[RollingReturnPoint] = []
    from datetime import timedelta

    current_start = first_date
    latest_possible_start = last_date - timedelta(days=window_days)
    annualized = window_days >= 365

    while current_start <= latest_possible_start:
        target_end = current_start + timedelta(days=window_days)

        start_obs = get_latest_nav_on_or_before(current_start)
        end_obs = get_latest_nav_on_or_before(target_end)

        if start_obs and end_obs:
            actual_start_date, start_nav = start_obs
            actual_end_date, end_nav = end_obs

            if actual_end_date > actual_start_date and start_nav > Decimal("0") and end_nav > Decimal("0"):
                elapsed = (actual_end_date - actual_start_date).days
                if annualized and elapsed >= 365:
                    ratio = float(end_nav / start_nav)
                    cagr_val = (math.pow(ratio, 365.0 / elapsed) - 1.0) * 100.0
                    ret_pct = Decimal(str(round(cagr_val, 4))).quantize(Decimal("0.0001"))
                else:
                    abs_ret = ((end_nav - start_nav) / start_nav) * Decimal("100")
                    ret_pct = abs_ret.quantize(Decimal("0.0001"))

                observations.append(
                    RollingReturnPoint(
                        start_date=actual_start_date,
                        end_date=actual_end_date,
                        start_nav=start_nav,
                        end_nav=end_nav,
                        return_pct=ret_pct,
                    )
                )

        current_start += timedelta(days=step_days)

    if not observations:
        raise InsufficientDataError(
            f"No valid rolling windows found for window_days={window_days}, step_days={step_days}"
        )

    # Compute statistics
    pct_values = [pt.return_pct for pt in observations]
    min_pct = min(pct_values)
    max_pct = max(pct_values)

    # Float conversion for mean, median, std_dev
    f_vals = [float(v) for v in pct_values]
    mean_val = sum(f_vals) / len(f_vals)
    sorted_f = sorted(f_vals)
    n = len(sorted_f)
    if n % 2 == 1:
        median_val = sorted_f[n // 2]
    else:
        median_val = (sorted_f[n // 2 - 1] + sorted_f[n // 2]) / 2.0

    if n > 1:
        variance = sum((x - mean_val) ** 2 for x in f_vals) / (n - 1)
        std_dev_val = math.sqrt(variance)
    else:
        std_dev_val = 0.0

    return RollingReturnsSummary(
        window_days=window_days,
        step_days=step_days,
        total_periods=len(observations),
        annualized=annualized,
        mean_pct=Decimal(str(round(mean_val, 4))).quantize(Decimal("0.0001")),
        median_pct=Decimal(str(round(median_val, 4))).quantize(Decimal("0.0001")),
        min_pct=min_pct,
        max_pct=max_pct,
        std_dev_pct=Decimal(str(round(std_dev_val, 4))).quantize(Decimal("0.0001")),
        observations=observations,
    )
