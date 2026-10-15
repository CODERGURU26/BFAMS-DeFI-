"""
test_dhan_portfolio.py
======================
Comprehensive tests for Dhan demat CSV parsing, transaction ingestion,
equity/ETF price lookup, portfolio valuation, and FastAPI endpoints.

Safety: all database tests run against BFAMS-DeFI-test only.
"""
import io
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import (
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    InvestmentHolding,
    Transaction,
    User,
)
from app.demat_ingestion import (
    _strip_dhan_metadata_header,
    parse_dhan_holdings_csv,
    parse_dhan_trades_csv,
    ingest_demat_holdings,
    ingest_demat_transactions,
    value_equity_holding,
    get_portfolio_valuation,
    ISIN_TO_NSE_SYMBOL,
)


# ---------------------------------------------------------------------------
# Shared CSV fixtures
# ---------------------------------------------------------------------------

DHAN_HOLDINGS_CSV = """\
Holding summary,For 14-10-2026
Name,Gururaj
UCC,FDCP32115J
Mobile,9372036702
Email ID,gs6029907@gmail.com

Scrip Name,ISIN Code,Free Holding,Locked In,Safe Keep,MTF Pledge,Margin Pledge,CUSA Pledge,Closing Price,Valuation
"Nippon Nifty 50 ETF (NIFTYBEES)","INF204KB14I2","150.00","0.00","0.00","0.00","0.00","0.00","259.20","38880.00"
"Cupid","INE509F01029","10.00","0.00","0.00","0.00","0.00","0.00","353.10","3531.00"
"SBI Gold ETF","INF200KA16D8","500.00","0.00","0.00","0.00","0.00","0.00","124.84","62420.00"

Valuation,104831.00,Total Number of Securities,3

NOTE : This sheet was downloaded at 15/10/2026 12:47 pm
"""

DHAN_TRADES_CSV = """\
Date,Side,Scrip Name,Exchange,Segment,Order Type,Quantity,Trade Price,Trade Value
2026-01-16,B,TATA Silver ETF,NSE,Cash,Intraday,500,27.04,13520.00
2026-01-13,S,TATA Silver ETF,NSE,Cash,Intraday,500,25.33,12666.00
2025-12-12,B,SBI Gold ETF,NSE,Cash,Delivery,99,112.22,11109.78
2025-12-12,B,Nippon Nifty 50 ETF (NIFTYBEES),NSE,Cash,Delivery,50,292.15,14607.50
"""

# Actual holdings file path (relative to project root)
HOLDINGS_FILE = Path("data/user_holdings/Dhan_Demat_Holding_14-10-2026.csv")
TRADES_FILE   = Path("data/user_holdings/trades.csv")


# ---------------------------------------------------------------------------
# Test group 1: Metadata header stripping
# ---------------------------------------------------------------------------

class TestDhanMetadataStripping(unittest.TestCase):

    def test_strip_holdings_metadata_finds_scrip_name_header(self):
        stripped = _strip_dhan_metadata_header(DHAN_HOLDINGS_CSV)
        first_line = stripped.splitlines()[0]
        self.assertIn("Scrip Name", first_line)
        self.assertNotIn("Holding summary", stripped.splitlines()[0])

    def test_strip_trades_no_metadata_leaves_header_intact(self):
        """Trades CSV has no metadata rows; first line is the real header."""
        stripped = _strip_dhan_metadata_header(DHAN_TRADES_CSV)
        self.assertTrue(stripped.startswith("Date,Side"))

    def test_strip_empty_csv_returns_same(self):
        stripped = _strip_dhan_metadata_header("")
        self.assertEqual(stripped, "")


# ---------------------------------------------------------------------------
# Test group 2: Dhan holdings CSV parsing
# ---------------------------------------------------------------------------

class TestParseDhanHoldings(unittest.TestCase):

    def test_parses_all_data_rows_skip_footer(self):
        accepted, errors, dups = parse_dhan_holdings_csv(
            DHAN_HOLDINGS_CSV, statement_date=date(2026, 10, 14)
        )
        # Footer rows ("Valuation,...", "NOTE:...") should be rejected/ignored
        # 3 data rows should be accepted
        self.assertEqual(len(accepted), 3, f"Expected 3 accepted, errors={errors}")
        self.assertEqual(dups, 0)

    def test_isin_extracted_correctly(self):
        accepted, _, _ = parse_dhan_holdings_csv(DHAN_HOLDINGS_CSV, statement_date=date(2026, 10, 14))
        isins = {h.isin for h in accepted}
        self.assertIn("INF204KB14I2", isins)  # NIFTYBEES
        self.assertIn("INE509F01029", isins)  # Cupid
        self.assertIn("INF200KA16D8", isins)  # SBI Gold ETF

    def test_quantity_and_price_parsed(self):
        accepted, _, _ = parse_dhan_holdings_csv(DHAN_HOLDINGS_CSV, statement_date=date(2026, 10, 14))
        niftybees = next(h for h in accepted if h.isin == "INF204KB14I2")
        self.assertEqual(niftybees.quantity, Decimal("150.00"))
        self.assertEqual(niftybees.unit_price, Decimal("259.20"))
        self.assertEqual(niftybees.market_value, Decimal("38880.00"))

    def test_asset_type_classification(self):
        accepted, _, _ = parse_dhan_holdings_csv(DHAN_HOLDINGS_CSV, statement_date=date(2026, 10, 14))
        by_isin = {h.isin: h for h in accepted}
        # INF prefix -> ETF
        self.assertEqual(by_isin["INF204KB14I2"].asset_type, "ETF")
        # INE509F01029 -> EQUITY (Cupid)
        self.assertEqual(by_isin["INE509F01029"].asset_type, "EQUITY")

    def test_observed_at_uses_statement_date(self):
        stmt = date(2026, 10, 14)
        accepted, _, _ = parse_dhan_holdings_csv(DHAN_HOLDINGS_CSV, statement_date=stmt)
        for h in accepted:
            self.assertEqual(h.observed_at.date(), stmt)

    def test_actual_holdings_file_parses_15_securities(self):
        """Real file must yield exactly 15 accepted holdings, 0 duplicates."""
        if not HOLDINGS_FILE.exists():
            self.skipTest(f"Holdings file not found: {HOLDINGS_FILE}")
        csv_text = HOLDINGS_FILE.read_text(encoding="utf-8-sig")
        accepted, errors, dups = parse_dhan_holdings_csv(
            csv_text, statement_date=date(2026, 10, 14)
        )
        self.assertEqual(len(accepted), 15, f"errors={errors}")
        self.assertEqual(dups, 0)


# ---------------------------------------------------------------------------
# Test group 3: Dhan trades CSV parsing
# ---------------------------------------------------------------------------

class TestParseDhanTrades(unittest.TestCase):

    def test_all_valid_rows_accepted(self):
        accepted, errors, dups = parse_dhan_trades_csv(DHAN_TRADES_CSV)
        self.assertEqual(len(accepted), 4, f"errors={errors}")
        self.assertEqual(len(errors), 0)
        self.assertEqual(dups, 0)

    def test_buy_sell_normalization(self):
        accepted, _, _ = parse_dhan_trades_csv(DHAN_TRADES_CSV)
        sides = [t.transaction_type for t in accepted]
        self.assertIn("BUY", sides)
        self.assertIn("SELL", sides)

    def test_trade_price_and_value_parsed(self):
        accepted, _, _ = parse_dhan_trades_csv(DHAN_TRADES_CSV)
        first = accepted[0]  # 2026-01-16 BUY TATA Silver ETF
        self.assertEqual(first.price, Decimal("27.04"))
        self.assertEqual(first.amount, Decimal("13520.00"))
        self.assertEqual(first.quantity, Decimal("500"))

    def test_actual_trades_file_parses_27_rows(self):
        """Real trades file must have exactly 27 valid rows (per spec)."""
        if not TRADES_FILE.exists():
            self.skipTest(f"Trades file not found: {TRADES_FILE}")
        csv_text = TRADES_FILE.read_text(encoding="utf-8-sig")
        accepted, errors, dups = parse_dhan_trades_csv(csv_text)
        self.assertEqual(len(accepted), 27, f"errors={errors}")
        self.assertEqual(dups, 0)

    def test_duplicate_trade_in_file_detected(self):
        dup_csv = """\
Date,Side,Scrip Name,Exchange,Segment,Order Type,Quantity,Trade Price,Trade Value
2026-01-16,B,TATA Silver ETF,NSE,Cash,Intraday,500,27.04,13520.00
2026-01-16,B,TATA Silver ETF,NSE,Cash,Intraday,500,27.04,13520.00
"""
        accepted, errors, dups = parse_dhan_trades_csv(dup_csv)
        self.assertEqual(dups, 1)
        self.assertEqual(len(accepted), 1)


# ---------------------------------------------------------------------------
# Test group 4: ISIN→NSE symbol resolution table
# ---------------------------------------------------------------------------

class TestIsinResolutionTable(unittest.TestCase):

    def test_known_etf_isins_resolve(self):
        self.assertIn("INF204KB14I2", ISIN_TO_NSE_SYMBOL)
        self.assertEqual(ISIN_TO_NSE_SYMBOL["INF204KB14I2"], ("NIFTYBEES", "ETF"))

    def test_known_equity_isins_resolve(self):
        self.assertIn("INE509F01029", ISIN_TO_NSE_SYMBOL)
        self.assertEqual(ISIN_TO_NSE_SYMBOL["INE509F01029"][1], "EQUITY")

    def test_reit_isins_present(self):
        # REITs: Embassy, Mindspace, Brookfield
        self.assertIn("INE041025011", ISIN_TO_NSE_SYMBOL)
        self.assertEqual(ISIN_TO_NSE_SYMBOL["INE041025011"][1], "REIT")

    def test_all_15_holdings_isins_covered(self):
        """Every ISIN from the actual holdings file must be in the table."""
        if not HOLDINGS_FILE.exists():
            self.skipTest(f"Holdings file not found: {HOLDINGS_FILE}")
        csv_text = HOLDINGS_FILE.read_text(encoding="utf-8-sig")
        accepted, _, _ = parse_dhan_holdings_csv(csv_text, statement_date=date(2026, 10, 14))
        missing = [h.isin for h in accepted if h.isin not in ISIN_TO_NSE_SYMBOL]
        self.assertEqual(missing, [], f"ISINs not in resolution table: {missing}")


# ---------------------------------------------------------------------------
# Test group 5: equity valuation (unit — no DB required)
# ---------------------------------------------------------------------------

class TestEquityValuationNoDb(unittest.TestCase):
    """Pure-Python tests that verify valuation logic without a DB session."""

    def _mock_session(self):
        """Minimal mock: value_equity_holding falls back to dhan_closing_price."""
        # We pass dhan_closing_price so no DB query is needed
        return None  # session not used when NSE lookup returns None

    def test_fallback_to_dhan_snapshot_price(self):
        from unittest.mock import MagicMock, patch
        mock_session = MagicMock()

        # Force lookup_equity_price_by_symbol to return (None, None)
        with patch("app.demat_ingestion.lookup_equity_price_by_symbol", return_value=(None, None)):
            ev = value_equity_holding(
                session=mock_session,
                isin="INF204KB14I2",
                asset_name="Nippon Nifty 50 ETF",
                asset_type="ETF",
                quantity=Decimal("150"),
                as_of_date=date(2026, 10, 14),
                dhan_closing_price=Decimal("259.20"),
            )

        self.assertEqual(ev.status, "current")
        self.assertEqual(ev.market_value, Decimal("38880.0000"))
        self.assertEqual(ev.close_price, Decimal("259.20"))
        self.assertIn("Dhan snapshot", ev.notes)

    def test_zero_quantity_returns_zero_market_value(self):
        from unittest.mock import MagicMock
        mock_session = MagicMock()
        ev = value_equity_holding(
            session=mock_session,
            isin="INF204KB14I2",
            asset_name="Nippon Nifty 50 ETF",
            asset_type="ETF",
            quantity=Decimal("0"),
            as_of_date=date(2026, 10, 14),
        )
        self.assertEqual(ev.status, "zero_quantity")
        self.assertEqual(ev.market_value, Decimal("0.0000"))

    def test_no_price_status_when_both_sources_missing(self):
        from unittest.mock import MagicMock, patch
        mock_session = MagicMock()
        with patch("app.demat_ingestion.lookup_equity_price_by_symbol", return_value=(None, None)):
            ev = value_equity_holding(
                session=mock_session,
                isin="INF204KB14I2",
                asset_name="Nippon Nifty 50 ETF",
                asset_type="ETF",
                quantity=Decimal("150"),
                as_of_date=date(2026, 10, 14),
                dhan_closing_price=None,  # no fallback
            )
        self.assertEqual(ev.status, "no_price")
        self.assertIsNone(ev.market_value)

    def test_market_value_decimal_precision(self):
        from unittest.mock import MagicMock, patch
        mock_session = MagicMock()
        with patch(
            "app.demat_ingestion.lookup_equity_price_by_symbol",
            return_value=(Decimal("259.1667"), date(2026, 10, 14)),
        ):
            ev = value_equity_holding(
                session=mock_session,
                isin="INF204KB14I2",
                asset_name="Nippon Nifty 50 ETF",
                asset_type="ETF",
                quantity=Decimal("150"),
                as_of_date=date(2026, 10, 14),
            )
        # 150 * 259.1667 = 38875.005 -> rounded to 4dp
        self.assertEqual(ev.market_value, Decimal("38875.0050"))


# ---------------------------------------------------------------------------
# Test group 6: database integration (BFAMS-DeFI-test)
# ---------------------------------------------------------------------------

class TestDhanDatabaseIntegration(unittest.TestCase):
    """Integration tests against BFAMS-DeFI-test. All cleanup on teardown."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        # Verify we are on the test database
        with Session(cls.engine) as session:
            db_name = session.execute(text("SELECT current_database()")).scalar()
            assert db_name == "BFAMS-DeFI-test", f"Wrong DB: {db_name}"

        # Create a dedicated test financial account
        with Session(cls.engine) as session:
            user = User()
            provider = DataProvider(
                provider_code="DHAN_TEST_PORTFOLIO",
                provider_name="Dhan Test Provider",
                provider_type="manual_import",
            )
            session.add_all([user, provider])
            session.flush()
            conn = FinancialConnection(user_id=user.id, provider_id=provider.id, status="active")
            session.add(conn)
            session.flush()
            acc = FinancialAccount(
                connection_id=conn.id,
                account_type="DEMAT",
                account_name="Dhan Portfolio Test Account",
                status="active",
            )
            session.add(acc)
            session.commit()
            cls.account_id = acc.id
            cls.connection_id = conn.id
            cls.user_id = user.id
            cls.provider_id = provider.id

    @classmethod
    def tearDownClass(cls):
        """Remove all test data inserted during tests."""
        with Session(cls.engine) as session:
            # Delete holdings and transactions tied to our test account
            for row in session.scalars(
                select(InvestmentHolding).where(
                    InvestmentHolding.account_id == cls.account_id
                )
            ).all():
                session.delete(row)
            for row in session.scalars(
                select(Transaction).where(
                    Transaction.account_id == cls.account_id
                )
            ).all():
                session.delete(row)
            session.commit()
            # Delete account, connection, provider, user
            acc = session.get(FinancialAccount, cls.account_id)
            if acc:
                session.delete(acc)
                session.commit()
            conn = session.get(FinancialConnection, cls.connection_id)
            if conn:
                session.delete(conn)
                session.commit()
            provider = session.get(DataProvider, cls.provider_id)
            if provider:
                session.delete(provider)
                session.commit()
            user = session.get(User, cls.user_id)
            if user:
                session.delete(user)
                session.commit()

    def test_holdings_dry_run_writes_nothing(self):
        with Session(self.engine) as session:
            before = session.scalar(
                select(InvestmentHolding.id).where(
                    InvestmentHolding.account_id == self.account_id
                )
            )
            self.assertIsNone(before)

            summary = ingest_demat_holdings(
                session=session,
                account_id=self.account_id,
                csv_text=DHAN_HOLDINGS_CSV,
                statement_date=date(2026, 10, 14),
                dry_run=True,
                custom_aliases={
                    "asset_name": ["Scrip Name"],
                    "isin": ["ISIN Code"],
                    "quantity": ["Free Holding"],
                    "price": ["Closing Price"],
                    "market_value": ["Valuation"],
                },
            )
            self.assertTrue(summary.dry_run)
            self.assertEqual(summary.accepted_count, 3)

            after = session.scalar(
                select(InvestmentHolding.id).where(
                    InvestmentHolding.account_id == self.account_id
                )
            )
            self.assertIsNone(after)

    def test_holdings_live_ingest_and_idempotency(self):
        with Session(self.engine) as session:
            # First run: insert 3 rows
            sum1 = ingest_demat_holdings(
                session=session,
                account_id=self.account_id,
                csv_text=DHAN_HOLDINGS_CSV,
                statement_date=date(2026, 10, 14),
                dry_run=False,
                custom_aliases={
                    "asset_name": ["Scrip Name"],
                    "isin": ["ISIN Code"],
                    "quantity": ["Free Holding"],
                    "price": ["Closing Price"],
                    "market_value": ["Valuation"],
                },
            )
            self.assertEqual(sum1.accepted_count, 3)

            # Second run: 3 duplicates, 0 inserted
            sum2 = ingest_demat_holdings(
                session=session,
                account_id=self.account_id,
                csv_text=DHAN_HOLDINGS_CSV,
                statement_date=date(2026, 10, 14),
                dry_run=False,
                custom_aliases={
                    "asset_name": ["Scrip Name"],
                    "isin": ["ISIN Code"],
                    "quantity": ["Free Holding"],
                    "price": ["Closing Price"],
                    "market_value": ["Valuation"],
                },
            )
            self.assertEqual(sum2.accepted_count, 0)
            self.assertEqual(sum2.duplicate_count, 3)

    def test_transactions_dry_run_writes_nothing(self):
        with Session(self.engine) as session:
            before = session.scalar(
                select(Transaction.id).where(
                    Transaction.account_id == self.account_id
                )
            )
            self.assertIsNone(before)

            summary = ingest_demat_transactions(
                session=session,
                account_id=self.account_id,
                csv_text=DHAN_TRADES_CSV,
                dry_run=True,
                parser="dhan",
            )
            self.assertTrue(summary.dry_run)
            self.assertEqual(summary.accepted_count, 4)
            self.assertEqual(summary.rejected_count, 0)

            after = session.scalar(
                select(Transaction.id).where(
                    Transaction.account_id == self.account_id
                )
            )
            self.assertIsNone(after)

    def test_transactions_live_ingest_and_idempotency(self):
        with Session(self.engine) as session:
            # First run
            sum1 = ingest_demat_transactions(
                session=session,
                account_id=self.account_id,
                csv_text=DHAN_TRADES_CSV,
                dry_run=False,
                parser="dhan",
            )
            self.assertEqual(sum1.accepted_count, 4)

            # Second run: all duplicates
            sum2 = ingest_demat_transactions(
                session=session,
                account_id=self.account_id,
                csv_text=DHAN_TRADES_CSV,
                dry_run=False,
                parser="dhan",
            )
            self.assertEqual(sum2.accepted_count, 0)
            self.assertEqual(sum2.duplicate_count, 4)

    def test_portfolio_valuation_uses_dhan_fallback(self):
        """After ingesting holdings, portfolio valuation returns a total > 0."""
        with Session(self.engine) as session:
            summary = get_portfolio_valuation(
                session=session,
                account_id=self.account_id,
                as_of_date=date(2026, 10, 14),
            )
            self.assertEqual(summary.total_holdings, 3)
            self.assertIsNotNone(summary.total_market_value)
            self.assertGreater(summary.total_market_value, Decimal("0"))
            # Every holding must have a status
            for h in summary.holdings:
                self.assertIn(h.status, {"current", "stale", "no_price", "zero_quantity", "snapshot"})

    def test_portfolio_empty_account_returns_zero(self):
        """An account with no holdings returns an empty PortfolioSummary."""
        # Create a second empty account
        with Session(self.engine) as session:
            acc2 = session.scalars(
                select(FinancialAccount).where(
                    FinancialAccount.id == self.account_id
                )
            ).first()
            # Create sibling account on same connection
            empty_acc = FinancialAccount(
                connection_id=acc2.connection_id,
                account_type="DEMAT",
                account_name="Empty Account",
            )
            session.add(empty_acc)
            session.commit()
            empty_id = empty_acc.id

        with Session(self.engine) as session:
            summary = get_portfolio_valuation(session=session, account_id=empty_id)
            self.assertEqual(summary.total_holdings, 0)
            self.assertEqual(summary.total_market_value, Decimal("0"))

        # Cleanup
        with Session(self.engine) as session:
            a = session.get(FinancialAccount, empty_id)
            if a:
                session.delete(a)
                session.commit()


# ---------------------------------------------------------------------------
# Test group 7: FastAPI endpoint integration
# ---------------------------------------------------------------------------

class TestPortfolioEndpoints(unittest.TestCase):
    """Test portfolio HTTP endpoints via TestClient."""

    @classmethod
    def setUpClass(cls):
        from main import app
        cls.client = TestClient(app)

        cls.engine = get_test_engine()
        with Session(cls.engine) as session:
            user = User()
            provider = DataProvider(
                provider_code="DHAN_ENDPOINT_TEST",
                provider_name="Dhan Endpoint Provider",
                provider_type="manual_import",
            )
            session.add_all([user, provider])
            session.flush()
            conn = FinancialConnection(user_id=user.id, provider_id=provider.id, status="active")
            session.add(conn)
            session.flush()
            acc = FinancialAccount(
                connection_id=conn.id,
                account_type="DEMAT",
                account_name="Endpoint Test Account",
            )
            session.add(acc)
            session.commit()
            cls.account_id = acc.id
            cls.connection_id = conn.id
            cls.user_id = user.id
            cls.provider_id = provider.id

    @classmethod
    def tearDownClass(cls):
        with Session(cls.engine) as session:
            for row in session.scalars(
                select(InvestmentHolding).where(InvestmentHolding.account_id == cls.account_id)
            ).all():
                session.delete(row)
            for row in session.scalars(
                select(Transaction).where(Transaction.account_id == cls.account_id)
            ).all():
                session.delete(row)
            session.commit()
            for model, pk in [
                (FinancialAccount, cls.account_id),
                (FinancialConnection, cls.connection_id),
                (DataProvider, cls.provider_id),
                (User, cls.user_id),
            ]:
                obj = session.get(model, pk)
                if obj:
                    session.delete(obj)
                    session.commit()

    def test_ingest_holdings_dry_run_returns_200(self):
        resp = self.client.post(
            f"/portfolio/ingest/holdings"
            f"?account_id={self.account_id}&dry_run=true&statement_date=2026-10-14",
            files={"file": ("holdings.csv", DHAN_HOLDINGS_CSV.encode(), "text/csv")},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["accepted_count"], 3)
        self.assertEqual(body["rejected_count"], 0)

    def test_ingest_holdings_404_for_unknown_account(self):
        fake_id = uuid.uuid4()
        resp = self.client.post(
            f"/portfolio/ingest/holdings?account_id={fake_id}&dry_run=true",
            files={"file": ("holdings.csv", DHAN_HOLDINGS_CSV.encode(), "text/csv")},
        )
        self.assertEqual(resp.status_code, 404)

    def test_ingest_trades_dry_run_returns_200(self):
        resp = self.client.post(
            f"/portfolio/ingest/trades?account_id={self.account_id}&dry_run=true",
            files={"file": ("trades.csv", DHAN_TRADES_CSV.encode(), "text/csv")},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["accepted_count"], 4)

    def test_get_holdings_empty_before_ingest(self):
        resp = self.client.get(f"/portfolio/holdings/{self.account_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_get_holdings_after_ingest_returns_rows(self):
        # Ingest (live) first
        self.client.post(
            f"/portfolio/ingest/holdings"
            f"?account_id={self.account_id}&statement_date=2026-10-14",
            files={"file": ("holdings.csv", DHAN_HOLDINGS_CSV.encode(), "text/csv")},
        )
        resp = self.client.get(f"/portfolio/holdings/{self.account_id}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 3)
        asset_names = {r["asset_name"] for r in rows}
        self.assertTrue(any("Nifty 50" in name for name in asset_names))

    def test_valuation_endpoint_returns_non_zero_total(self):
        resp = self.client.get(
            f"/portfolio/valuation/{self.account_id}?as_of_date=2026-10-14"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_holdings"], 3)
        total = Decimal(body["total_market_value"])
        self.assertGreater(total, Decimal("0"))

    def test_valuation_endpoint_404_unknown_account(self):
        resp = self.client.get(f"/portfolio/valuation/{uuid.uuid4()}")
        self.assertEqual(resp.status_code, 404)

    def test_ingest_trades_live_then_idempotent(self):
        # Live ingest
        r1 = self.client.post(
            f"/portfolio/ingest/trades?account_id={self.account_id}",
            files={"file": ("trades.csv", DHAN_TRADES_CSV.encode(), "text/csv")},
        )
        self.assertEqual(r1.status_code, 200)
        b1 = r1.json()
        self.assertEqual(b1["accepted_count"], 4)

        # Idempotent re-run: 0 inserted, 4 duplicates
        r2 = self.client.post(
            f"/portfolio/ingest/trades?account_id={self.account_id}",
            files={"file": ("trades.csv", DHAN_TRADES_CSV.encode(), "text/csv")},
        )
        self.assertEqual(r2.status_code, 200)
        b2 = r2.json()
        self.assertEqual(b2["accepted_count"], 0)
        self.assertEqual(b2["duplicate_count"], 4)


if __name__ == "__main__":
    unittest.main()
