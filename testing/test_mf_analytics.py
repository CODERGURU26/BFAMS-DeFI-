"""
testing/test_mf_analytics.py
============================
Automated test suite for Mutual Fund Analytics:
1. Pure unit tests with hand-calculated examples (CAGR, absolute return, drawdown, rolling returns).
2. Boundary and error conditions (zero NAV, invalid dates, insufficient history, staleness).
3. Live database integration against BFAMS-DeFI-test using real records (e.g. scheme 100033).
4. Read-only and isolation safety (verifying zero row changes, no personal finance table access).
"""

from datetime import date
from decimal import Decimal
import unittest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import FinancialAccount, MfNav, MfScheme, Transaction, User
from app.mf_analytics import (
    DrawdownResult,
    InsufficientDataError,
    InvalidDateRangeError,
    NavPoint,
    NonPositiveNavError,
    ReturnResult,
    RollingReturnsSummary,
    StaleDataError,
    calculate_max_drawdown,
    calculate_point_to_point,
    calculate_rolling_returns,
)
from app.mf_repository import (
    compare_schemes,
    get_nav_history,
    get_scheme_by_code,
    get_scheme_drawdown,
    get_scheme_returns,
    get_scheme_rolling_returns,
    search_schemes,
    verify_test_db,
)


class TestPureAnalyticsCalculations(unittest.TestCase):
    """1. Pure unit tests with hand-calculated formulas — no database required."""

    def test_hand_calculated_cagr_two_years(self):
        """
        Hand-calculated 2-year example:
        Start: 2022-01-01, NAV = 100.00
        End:   2024-01-01, NAV = 121.00 (730 days, exactly 2 standard years)
        Absolute return: (121 - 100) / 100 = 0.2100 -> +21.0000%
        CAGR: (121 / 100) ** (365 / 730) - 1 = (1.21) ** 0.5 - 1 = 1.10 - 1 = +10.0000%
        """
        p0 = NavPoint(nav_date=date(2022, 1, 1), nav=Decimal("100.00"))
        p1 = NavPoint(nav_date=date(2024, 1, 1), nav=Decimal("121.00"))

        res = calculate_point_to_point(p0, p1)

        self.assertEqual(res.elapsed_days, 730)
        self.assertTrue(res.annualized)
        self.assertEqual(res.absolute_return_pct, Decimal("21.0000"))
        self.assertEqual(res.cagr_pct, Decimal("10.0000"))

    def test_hand_calculated_under_one_year_no_cagr(self):
        """
        Returns under 365 days must NOT be annualized per AMFI guidelines.
        Start: 2023-01-01, NAV = 100.00
        End:   2023-07-02, NAV = 105.00 (182 days)
        Absolute return: +5.0000%
        CAGR: None
        """
        p0 = NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("100.00"))
        p1 = NavPoint(nav_date=date(2023, 7, 2), nav=Decimal("105.00"))

        res = calculate_point_to_point(p0, p1)

        self.assertEqual(res.elapsed_days, 182)
        self.assertFalse(res.annualized)
        self.assertIsNone(res.cagr)
        self.assertIsNone(res.cagr_pct)
        self.assertEqual(res.absolute_return_pct, Decimal("5.0000"))

    def test_hand_calculated_max_drawdown(self):
        """
        Hand-calculated drawdown scenario:
        Day 1: 100.00  (peak=100)
        Day 2: 120.00  (new peak=120)
        Day 3: 110.00  (dd = -8.3333%)
        Day 4:  90.00  (dd = -25.0000%)
        Day 5:  80.00  (dd = (80-120)/120 = -40/120 = -33.3333% -> trough)
        Day 6: 115.00  (dd = -4.1667%)
        Day 7: 125.00  (recovery: 125 >= 120)
        """
        series = [
            NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("100.00")),
            NavPoint(nav_date=date(2023, 1, 2), nav=Decimal("120.00")),
            NavPoint(nav_date=date(2023, 1, 3), nav=Decimal("110.00")),
            NavPoint(nav_date=date(2023, 1, 4), nav=Decimal("90.00")),
            NavPoint(nav_date=date(2023, 1, 5), nav=Decimal("80.00")),
            NavPoint(nav_date=date(2023, 1, 6), nav=Decimal("115.00")),
            NavPoint(nav_date=date(2023, 1, 7), nav=Decimal("125.00")),
        ]

        dd = calculate_max_drawdown(series)

        self.assertEqual(dd.max_drawdown_pct, Decimal("-33.3333"))
        self.assertEqual(dd.peak_date, date(2023, 1, 2))
        self.assertEqual(dd.peak_nav, Decimal("120.00"))
        self.assertEqual(dd.trough_date, date(2023, 1, 5))
        self.assertEqual(dd.trough_nav, Decimal("80.00"))
        self.assertEqual(dd.recovery_date, date(2023, 1, 7))
        self.assertTrue(dd.is_recovered)
        self.assertEqual(dd.total_observations, 7)

    def test_drawdown_no_recovery(self):
        """Drawdown where series ends before recovering to prior peak."""
        series = [
            NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("100.00")),
            NavPoint(nav_date=date(2023, 1, 2), nav=Decimal("150.00")),
            NavPoint(nav_date=date(2023, 1, 3), nav=Decimal("105.00")),  # -30%
            NavPoint(nav_date=date(2023, 1, 4), nav=Decimal("120.00")),  # partial bounce
        ]

        dd = calculate_max_drawdown(series)

        self.assertEqual(dd.max_drawdown_pct, Decimal("-30.0000"))
        self.assertEqual(dd.peak_nav, Decimal("150.00"))
        self.assertEqual(dd.trough_nav, Decimal("105.00"))
        self.assertIsNone(dd.recovery_date)
        self.assertFalse(dd.is_recovered)

    def test_drawdown_strictly_increasing(self):
        """Series with no decline has max drawdown 0%."""
        series = [
            NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("10.00")),
            NavPoint(nav_date=date(2023, 1, 2), nav=Decimal("11.00")),
            NavPoint(nav_date=date(2023, 1, 3), nav=Decimal("12.00")),
        ]
        dd = calculate_max_drawdown(series)
        self.assertEqual(dd.max_drawdown_pct, Decimal("0.0000"))


class TestBoundaryAndErrorConditions(unittest.TestCase):
    """2. Boundary conditions, zero NAVs, and invalid inputs."""

    def test_non_positive_nav_raises(self):
        p_zero = NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("0.00"))
        p_pos = NavPoint(nav_date=date(2023, 1, 2), nav=Decimal("10.00"))

        with self.assertRaises(NonPositiveNavError):
            calculate_point_to_point(p_zero, p_pos)

        with self.assertRaises(NonPositiveNavError):
            calculate_point_to_point(p_pos, p_zero)

        p_neg = NavPoint(nav_date=date(2023, 1, 3), nav=Decimal("-5.00"))
        with self.assertRaises(NonPositiveNavError):
            calculate_point_to_point(p_pos, p_neg)

    def test_invalid_date_order_raises(self):
        p1 = NavPoint(nav_date=date(2023, 5, 10), nav=Decimal("50.00"))
        p2 = NavPoint(nav_date=date(2023, 5, 10), nav=Decimal("55.00"))

        with self.assertRaises(InvalidDateRangeError):
            calculate_point_to_point(p1, p2)

        p3 = NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("40.00"))
        with self.assertRaises(InvalidDateRangeError):
            calculate_point_to_point(p1, p3)

    def test_insufficient_history_for_drawdown(self):
        with self.assertRaises(InsufficientDataError):
            calculate_max_drawdown([])

        with self.assertRaises(InsufficientDataError):
            calculate_max_drawdown([NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("10.00"))])

    def test_rolling_returns_insufficient_span(self):
        series = [
            NavPoint(nav_date=date(2023, 1, 1), nav=Decimal("10.00")),
            NavPoint(nav_date=date(2023, 3, 1), nav=Decimal("11.00")),
        ]
        with self.assertRaises(InsufficientDataError):
            calculate_rolling_returns(series, window_days=365)


class TestLiveDatabaseIntegration(unittest.TestCase):
    """3. Live database integration against BFAMS-DeFI-test."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        cls.session = Session(cls.engine)
        verify_test_db(cls.session)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_database_identity(self):
        """Confirm connected DB is strictly BFAMS-DeFI-test."""
        current_db = self.session.execute(select(func.current_database())).scalar()
        self.assertEqual(current_db, "BFAMS-DeFI-test")

    def test_live_scheme_search(self):
        results, total = search_schemes(self.session, query="100033", limit=10)
        self.assertGreaterEqual(total, 1)
        self.assertTrue(any(s["scheme_code"] == "100033" for s in results))

        results, total = search_schemes(self.session, query="Large & Mid Cap", limit=10)
        self.assertGreaterEqual(total, 1)
        self.assertTrue(all("Large & Mid Cap" in s["scheme_name"] for s in results))

    def test_live_nav_history_retrieval(self):
        history = get_nav_history(
            self.session,
            scheme_code="100033",
            start_date=date(2021, 10, 14),
            end_date=date(2021, 10, 20),
        )
        self.assertGreaterEqual(len(history), 2)
        for i in range(len(history) - 1):
            self.assertLess(history[i].nav_date, history[i + 1].nav_date)

        self.assertEqual(history[0].nav_date, date(2021, 10, 14))
        self.assertEqual(history[0].nav, Decimal("719.8100000000"))

    def test_live_point_to_point_and_cagr(self):
        """
        Scheme 100033:
        Start: 2021-10-14, NAV = 719.81
        End:   2022-10-14, NAV = 597.12
        Elapsed: exactly 365 days.
        Abs Return: -17.0448%
        CAGR: -17.0448%
        """
        ret = get_scheme_returns(
            self.session,
            scheme_code="100033",
            start_date=date(2021, 10, 14),
            end_date=date(2022, 10, 14),
        )

        self.assertEqual(ret.start_date_actual, date(2021, 10, 14))
        self.assertEqual(ret.start_nav, Decimal("719.8100000000"))
        self.assertEqual(ret.end_date_actual, date(2022, 10, 14))
        self.assertEqual(ret.end_nav, Decimal("597.1200000000"))
        self.assertEqual(ret.elapsed_days, 365)
        self.assertTrue(ret.annualized)
        self.assertEqual(ret.absolute_return_pct, Decimal("-17.0448"))
        self.assertEqual(ret.cagr_pct, Decimal("-17.0448"))

    def test_live_three_year_cagr(self):
        """
        Scheme 100033:
        Start: 2021-10-14, NAV = 719.81
        End:   2024-10-14, NAV = 928.26
        Elapsed: 1,096 days.
        Abs Return: +28.9590%
        CAGR: +8.8517%
        """
        ret = get_scheme_returns(
            self.session,
            scheme_code="100033",
            start_date=date(2021, 10, 14),
            end_date=date(2024, 10, 14),
        )

        self.assertEqual(ret.elapsed_days, 1096)
        self.assertTrue(ret.annualized)
        self.assertEqual(ret.absolute_return_pct, Decimal("28.9590"))
        self.assertEqual(ret.cagr_pct, Decimal("8.8388"))

    def test_live_max_drawdown(self):
        dd = get_scheme_drawdown(
            self.session,
            scheme_code="100033",
            start_date=date(2021, 10, 14),
            end_date=date(2023, 10, 14),
        )
        self.assertLess(dd.max_drawdown_pct, Decimal("0"))
        self.assertGreater(dd.total_observations, 100)
        self.assertLessEqual(dd.peak_date, dd.trough_date)

    def test_live_rolling_returns(self):
        rolling = get_scheme_rolling_returns(
            self.session,
            scheme_code="100051",
            start_date=date(2021, 10, 14),
            end_date=date(2023, 10, 14),
            window_days=365,
            step_days=60,
        )
        self.assertGreaterEqual(rolling.total_periods, 5)
        self.assertTrue(rolling.annualized)
        self.assertTrue(rolling.min_pct <= rolling.median_pct <= rolling.max_pct)

    def test_live_scheme_comparison(self):
        comp = compare_schemes(
            self.session,
            scheme_codes=["100033", "100051", "NONEXISTENT_SCHEME"],
            start_date=date(2022, 1, 1),
            end_date=date(2024, 1, 1),
        )
        self.assertEqual(comp["schemes_evaluated"], 3)
        results = comp["results"]

        successes = [r for r in results if r["status"] == "success"]
        self.assertEqual(len(successes), 2)

        errors = [r for r in results if r["status"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["scheme_code"], "NONEXISTENT_SCHEME")


class TestDatabaseSafetyAndIsolation(unittest.TestCase):
    """4. Read-only safety and personal finance isolation verification."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        cls.session = Session(cls.engine)
        verify_test_db(cls.session)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_analytics_are_strictly_read_only(self):
        schemes_before = self.session.scalar(select(func.count()).select_from(MfScheme))
        navs_before = self.session.scalar(select(func.count()).select_from(MfNav))

        search_schemes(self.session, query="Axis", limit=20)
        get_scheme_returns(
            self.session, "100033", date(2022, 1, 1), date(2023, 1, 1)
        )
        get_scheme_drawdown(
            self.session, "100033", date(2022, 1, 1), date(2023, 1, 1)
        )

        schemes_after = self.session.scalar(select(func.count()).select_from(MfScheme))
        navs_after = self.session.scalar(select(func.count()).select_from(MfNav))

        self.assertEqual(schemes_before, schemes_after)
        self.assertEqual(navs_before, navs_after)

    def test_personal_financial_tables_untouched(self):
        acc_count = self.session.scalar(select(func.count()).select_from(FinancialAccount))
        txn_count = self.session.scalar(select(func.count()).select_from(Transaction))
        user_count = self.session.scalar(select(func.count()).select_from(User))

        compare_schemes(
            self.session,
            scheme_codes=["100033"],
            start_date=date(2022, 1, 1),
            end_date=date(2023, 1, 1),
        )

        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(FinancialAccount)), acc_count
        )
        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(Transaction)), txn_count
        )
        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(User)), user_count
        )


class TestAnalyticsRegressionFixes(unittest.TestCase):
    """5. Regression tests for Fix A, Fix B, and Fix C."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        cls.session = Session(cls.engine)
        verify_test_db(cls.session)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_fix_a_rolling_returns_weekend_deduplication(self):
        """
        Fix A: Stepping with step_days=1 over non-trading days (weekends)
        must NOT produce duplicate actual (start_date, end_date) observation pairs.
        """
        pts = [
            NavPoint(date(2023, 1, 2), Decimal("100.00")),  # Mon
            NavPoint(date(2023, 1, 3), Decimal("101.00")),  # Tue
            NavPoint(date(2023, 1, 4), Decimal("102.00")),  # Wed
            NavPoint(date(2023, 1, 5), Decimal("103.00")),  # Thu
            NavPoint(date(2023, 1, 6), Decimal("104.00")),  # Fri
            # Sat/Sun gap
            NavPoint(date(2023, 1, 9), Decimal("105.00")),  # Mon
            NavPoint(date(2023, 1, 10), Decimal("106.00")), # Tue
            NavPoint(date(2023, 1, 11), Decimal("107.00")), # Wed
            NavPoint(date(2023, 1, 12), Decimal("108.00")), # Thu
            NavPoint(date(2023, 1, 13), Decimal("109.00")), # Fri
            # Sat/Sun gap
            NavPoint(date(2023, 1, 16), Decimal("110.00")), # Mon
            NavPoint(date(2023, 1, 17), Decimal("111.00")), # Tue
        ]

        summary = calculate_rolling_returns(pts, window_days=7, step_days=1)

        # Check deduplication: all observation windows must have unique actual start and end dates
        observed_windows = [(o.start_date, o.end_date) for o in summary.observations]
        self.assertEqual(len(observed_windows), len(set(observed_windows)))

        # Prior to fix, Friday 2023-01-06 -> 2023-01-13 was recorded 3 times (Fri, Sat, Sun)
        friday_pairs = [w for w in observed_windows if w == (date(2023, 1, 6), date(2023, 1, 13))]
        self.assertEqual(len(friday_pairs), 1)

        # Confirm summary statistics use deduplicated count
        self.assertEqual(summary.total_periods, len(summary.observations))
        pct_vals = [o.return_pct for o in summary.observations]
        self.assertEqual(summary.min_pct, min(pct_vals))
        self.assertEqual(summary.max_pct, max(pct_vals))

    def test_fix_b_repository_rolling_returns_reversed_dates(self):
        """
        Fix B: get_scheme_rolling_returns must reject start_date >= end_date with InvalidDateRangeError.
        """
        with self.assertRaises(InvalidDateRangeError):
            get_scheme_rolling_returns(
                self.session,
                "100033",
                start_date=date(2023, 1, 1),
                end_date=date(2022, 1, 1),
            )

        with self.assertRaises(InvalidDateRangeError):
            get_scheme_rolling_returns(
                self.session,
                "100033",
                start_date=date(2022, 1, 1),
                end_date=date(2022, 1, 1),
            )

    def test_fix_c_comparison_drawdown_date_alignment(self):
        """
        Fix C: compare_schemes must pass the actual selected start date (ret.start_date_actual)
        into get_scheme_drawdown so the drawdown series matches the returns calculation.
        """
        comp = compare_schemes(
            self.session,
            ["100033"],
            start_date=date(2022, 1, 1),  # Saturday -> adjusted to 2021-12-31
            end_date=date(2024, 1, 1),
        )

        res = comp["results"][0]
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["start_date_actual"], "2021-12-31")
        self.assertEqual(res["end_date_actual"], "2024-01-01")

        # Prior to fix, total_observations was 492 (omitted 2021-12-31).
        # With fix, total_observations is 493 (includes the actual start date 2021-12-31).
        self.assertEqual(res["total_observations"], 493)
        self.assertEqual(res["max_drawdown_pct"], "-28.0313")


if __name__ == "__main__":
    unittest.main()
