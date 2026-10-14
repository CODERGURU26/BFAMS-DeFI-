import unittest
from datetime import date, timedelta
from decimal import Decimal
import uuid

from sqlalchemy import select, inspect
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import InvestmentHolding, MfNav, MfScheme
from app.mf_resolver import (
    resolve_mf_scheme,
    ResolutionStatus,
)
from app.mf_valuation import (
    value_mf_holding,
    value_investment_holding_record,
    ValuationStatus,
)


class TestInvestmentHoldingModel(unittest.TestCase):
    """Test ORM model compatibility with physical PostgreSQL schema."""

    def test_schema_columns_and_indices(self):
        engine = get_test_engine()
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("investment_holdings")}

        expected_cols = [
            "id",
            "account_id",
            "mf_scheme_id",
            "asset_name",
            "asset_identifier",
            "asset_type",
            "quantity",
            "unit_price",
            "market_value",
            "currency",
            "observed_at",
            "source_updated_at",
            "created_at",
        ]
        for col in expected_cols:
            self.assertIn(col, columns, f"Missing column {col} in investment_holdings")

        indices = [idx["name"] for idx in inspector.get_indexes("investment_holdings")]
        self.assertIn("idx_investment_holdings_mf_scheme", indices)
        self.assertIn("idx_investment_holdings_account_observed", indices)


class TestSchemeResolver(unittest.TestCase):
    """Test deterministic mutual fund scheme resolver."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()

    def test_exact_amfi_scheme_code_match(self):
        with Session(self.engine) as session:
            # Scheme 100033 is Aditya Birla Sun Life Advantage Fund
            res = resolve_mf_scheme(session, identifier="100033")
            self.assertEqual(res.status, ResolutionStatus.RESOLVED_SCHEME_CODE)
            self.assertEqual(res.scheme_code, "100033")
            self.assertIsNotNone(res.scheme_id)
            self.assertTrue(res.is_resolved)

    def test_unambiguous_isin_match(self):
        with Session(self.engine) as session:
            # INF209K01157 is an unambiguous ISIN in the dataset
            res = resolve_mf_scheme(session, identifier="INF209K01157")
            self.assertEqual(res.status, ResolutionStatus.RESOLVED_ISIN)
            self.assertEqual(res.matched_by, "isin")
            self.assertTrue(res.is_resolved)
            self.assertIsNotNone(res.scheme_id)

    def test_ambiguous_isin_match_requires_manual_mapping(self):
        with Session(self.engine) as session:
            # INF173K01189 is shared across 2 schemes
            res = resolve_mf_scheme(session, identifier="INF173K01189")
            self.assertEqual(res.status, ResolutionStatus.AMBIGUOUS_ISIN)
            self.assertFalse(res.is_resolved)
            self.assertIsNone(res.scheme_id)
            self.assertGreater(len(res.candidate_scheme_codes), 1)
            self.assertIn("Manual mapping required", res.reason)

    def test_missing_and_invalid_identifiers(self):
        with Session(self.engine) as session:
            # Empty / None
            res_none = resolve_mf_scheme(session, identifier=None)
            self.assertEqual(res_none.status, ResolutionStatus.INVALID_IDENTIFIER)

            # Non-existent scheme code
            res_fake = resolve_mf_scheme(session, identifier="99999999")
            self.assertEqual(res_fake.status, ResolutionStatus.UNRESOLVED)
            self.assertIn("Fuzzy scheme name matching is disabled", res_fake.reason)


class TestHoldingValuation(unittest.TestCase):
    """Test read-only mutual fund holding valuation service."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()

    def test_correct_quantity_times_nav_calculation(self):
        with Session(self.engine) as session:
            # Valuation for 100033 as of 2024-10-11
            val_date = date(2024, 10, 11)
            quantity = Decimal("150.500")

            valuation = value_mf_holding(
                session=session,
                scheme="100033",
                quantity=quantity,
                as_of_date=val_date,
                max_staleness_days=7,
            )

            self.assertEqual(valuation.status, ValuationStatus.CURRENT)
            self.assertEqual(valuation.scheme_code, "100033")
            self.assertEqual(valuation.quantity, quantity)
            self.assertIsNotNone(valuation.nav)
            self.assertGreater(valuation.nav, Decimal("0"))
            
            # Check market value precision
            expected_mv = (quantity * valuation.nav).quantize(Decimal("0.0001"))
            self.assertEqual(valuation.market_value, expected_mv)
            self.assertFalse(valuation.is_stale)
            self.assertTrue(valuation.is_reliable)

    def test_decimal_precision_multiplication(self):
        with Session(self.engine) as session:
            val_date = date(2024, 10, 11)
            # 6 decimal places quantity (e.g. fractional mutual fund units)
            quantity = Decimal("123.456789")

            valuation = value_mf_holding(
                session=session,
                scheme="100033",
                quantity=quantity,
                as_of_date=val_date,
            )

            expected_mv = (quantity * valuation.nav).quantize(Decimal("0.0001"))
            self.assertEqual(valuation.market_value, expected_mv)

    def test_historical_valuation_date_nav_selection(self):
        with Session(self.engine) as session:
            # 2024-10-13 was a Sunday. Latest NAV should be from Friday 2024-10-11.
            val_date = date(2024, 10, 13)
            valuation = value_mf_holding(
                session=session,
                scheme="100033",
                quantity=Decimal("100"),
                as_of_date=val_date,
            )

            self.assertEqual(valuation.nav_date, date(2024, 10, 11))
            self.assertEqual(valuation.data_age_days, 2)
            self.assertFalse(valuation.is_stale)
            self.assertEqual(valuation.status, ValuationStatus.CURRENT)

    def test_stale_nav_detection(self):
        with Session(self.engine) as session:
            # Latest imported NAV for 100033 is 2026-10-13.
            # Requesting valuation as of 2026-10-25 (12 days later) with max_staleness_days=7 triggers STALE.
            val_date = date(2026, 10, 25)
            valuation = value_mf_holding(
                session=session,
                scheme="100033",
                quantity=Decimal("100"),
                as_of_date=val_date,
                max_staleness_days=7,
            )

            self.assertTrue(valuation.is_stale)
            self.assertEqual(valuation.status, ValuationStatus.STALE)
            self.assertEqual(valuation.data_age_days, 12)
            self.assertFalse(valuation.is_reliable)
            self.assertIn("NAV is stale", valuation.notes)

    def test_missing_nav_before_first_date(self):
        with Session(self.engine) as session:
            # Date before historical data begins (e.g., year 1990)
            val_date = date(1990, 1, 1)
            valuation = value_mf_holding(
                session=session,
                scheme="100033",
                quantity=Decimal("100"),
                as_of_date=val_date,
            )

            self.assertEqual(valuation.status, ValuationStatus.MISSING_NAV)
            self.assertIsNone(valuation.nav)
            self.assertIsNone(valuation.market_value)
            self.assertFalse(valuation.is_reliable)

    def test_zero_quantity_handling(self):
        with Session(self.engine) as session:
            valuation = value_mf_holding(
                session=session,
                scheme="100033",
                quantity=Decimal("0"),
                as_of_date=date(2024, 10, 11),
            )
            self.assertEqual(valuation.status, ValuationStatus.ZERO_QUANTITY)
            self.assertEqual(valuation.market_value, Decimal("0.0000"))

    def test_value_investment_holding_record_read_only(self):
        with Session(self.engine) as session:
            # Test in-memory / detached InvestmentHolding instance
            holding = InvestmentHolding(
                account_id=uuid.uuid4(),
                asset_name="Aditya Birla Sun Life Advantage Fund",
                asset_identifier="100033",
                asset_type="MUTUAL_FUND",
                quantity=Decimal("50.0"),
                currency="INR",
            )
            val = value_investment_holding_record(
                session=session,
                holding=holding,
                as_of_date=date(2024, 10, 11),
            )
            self.assertEqual(val.status, ValuationStatus.CURRENT)
            self.assertEqual(val.scheme_code, "100033")
            self.assertIsNotNone(val.market_value)


if __name__ == "__main__":
    unittest.main()
