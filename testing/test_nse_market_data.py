import unittest
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import SecurityDailyPrice
from app.nse_market_data import (
    DailyBar,
    validate_daily_bar,
    parse_equity_bhavcopy,
    parse_index_daily,
    upsert_security_daily_prices,
    _parse_bhavcopy_date,
)


class TestNseMarketDataValidation(unittest.TestCase):
    """Test OHLCV validation logic and boundary rules."""

    def test_valid_equity_bar(self):
        bar = DailyBar(
            symbol="RELIANCE",
            instrument_type="equity",
            exchange="NSE",
            trading_date=date(2024, 10, 11),
            open_price=Decimal("2740.00"),
            high_price=Decimal("2766.55"),
            low_price=Decimal("2737.65"),
            close_price=Decimal("2744.20"),
            volume=3990061,
        )
        errors = validate_daily_bar(bar)
        self.assertEqual(errors, [])

    def test_invalid_ohlc_high_below_open_or_close(self):
        # High lower than Open
        bar = DailyBar(
            symbol="TEST",
            instrument_type="equity",
            exchange="NSE",
            trading_date=date(2024, 10, 11),
            open_price=Decimal("100.00"),
            high_price=Decimal("95.00"),  # Invalid: High < Open
            low_price=Decimal("90.00"),
            close_price=Decimal("92.00"),
        )
        errors = validate_daily_bar(bar)
        self.assertTrue(any("High" in e and "Open" in e for e in errors))

    def test_invalid_ohlc_low_above_open_or_close(self):
        # Low higher than Close
        bar = DailyBar(
            symbol="TEST",
            instrument_type="equity",
            exchange="NSE",
            trading_date=date(2024, 10, 11),
            open_price=Decimal("100.00"),
            high_price=Decimal("110.00"),
            low_price=Decimal("102.00"),  # Invalid: Low > Close
            close_price=Decimal("98.00"),
        )
        errors = validate_daily_bar(bar)
        self.assertTrue(any("Low" in e and "Close" in e for e in errors))

    def test_negative_volume(self):
        bar = DailyBar(
            symbol="TEST",
            instrument_type="equity",
            exchange="NSE",
            trading_date=date(2024, 10, 11),
            open_price=Decimal("100.00"),
            high_price=Decimal("110.00"),
            low_price=Decimal("90.00"),
            close_price=Decimal("105.00"),
            volume=-500,
        )
        errors = validate_daily_bar(bar)
        self.assertTrue(any("Volume cannot be negative" in e for e in errors))

    def test_non_positive_prices(self):
        bar = DailyBar(
            symbol="TEST",
            instrument_type="equity",
            exchange="NSE",
            trading_date=date(2024, 10, 11),
            open_price=Decimal("0.00"),
            high_price=Decimal("10.00"),
            low_price=Decimal("0.00"),
            close_price=Decimal("5.00"),
        )
        errors = validate_daily_bar(bar)
        self.assertTrue(any("open_price must be positive" in e for e in errors))

    def test_date_parser_formats(self):
        self.assertEqual(_parse_bhavcopy_date("11-Oct-2024"), date(2024, 10, 11))
        self.assertEqual(_parse_bhavcopy_date("11-10-2024"), date(2024, 10, 11))
        self.assertEqual(_parse_bhavcopy_date("2024-10-11"), date(2024, 10, 11))
        with self.assertRaises(ValueError):
            _parse_bhavcopy_date("invalid-date")


class TestNseMarketDataIntegration(unittest.TestCase):
    """Test parser and idempotent upsert into BFAMS-DeFI-test."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()

    def test_equity_bhavcopy_parsing(self):
        sample_csv = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 11-Oct-2024, 2745.00, 2740.00, 2766.55, 2737.65, 2743.00, 2744.20, 2748.50, 3990061, 109668.50, 142100, 2100000, 52.63
TCS, EQ, 11-Oct-2024, 4150.00, 4143.00, 4241.95, 4112.45, 4148.00, 4149.20, 4180.00, 5759183, 240733.85, 215400, 3200000, 55.56
HDFCBANK, BL, 11-Oct-2024, 1600.00, 1605.00, 1610.00, 1600.00, 1608.00, 1608.00, 1605.00, 50000, 802.50, 100, 50000, 100.00
"""
        bars = parse_equity_bhavcopy(sample_csv, target_symbols=["RELIANCE", "TCS"], series_filter="EQ")
        self.assertEqual(len(bars), 2)
        rel = next(b for b in bars if b.symbol == "RELIANCE")
        self.assertEqual(rel.trading_date, date(2024, 10, 11))
        self.assertEqual(rel.open_price, Decimal("2740.00"))
        self.assertEqual(rel.high_price, Decimal("2766.55"))
        self.assertEqual(rel.close_price, Decimal("2744.20"))
        self.assertEqual(rel.volume, 3990061)

    def test_index_data_parsing(self):
        sample_index_csv = """Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield
Nifty 50,11-10-2024,24985.30,25028.65,24920.05,24964.25,-34.20,-0.14,210462717,21734.97,23.47,3.74,1.20
Nifty Bank,11-10-2024,51500.00,51700.00,51300.00,51650.00,150.00,0.29,150000000,15000.00,16.50,2.80,0.90
"""
        bars = parse_index_daily(sample_index_csv, target_indices=["Nifty 50"])
        self.assertEqual(len(bars), 1)
        idx = bars[0]
        self.assertEqual(idx.symbol, "Nifty 50")
        self.assertEqual(idx.instrument_type, "index")
        self.assertEqual(idx.trading_date, date(2024, 10, 11))
        self.assertEqual(idx.open_price, Decimal("24985.30"))
        self.assertEqual(idx.high_price, Decimal("25028.65"))
        self.assertEqual(idx.close_price, Decimal("24964.25"))

    def test_idempotent_upsert_and_type_separation(self):
        with Session(self.engine) as session:
            test_date = date(2024, 10, 11)
            bar_equity = DailyBar(
                symbol="TESTSTOCK",
                instrument_type="equity",
                exchange="NSE",
                trading_date=test_date,
                open_price=Decimal("100.00"),
                high_price=Decimal("105.00"),
                low_price=Decimal("99.00"),
                close_price=Decimal("102.50"),
                volume=10000,
                source="TEST_EQUITY",
            )
            bar_index = DailyBar(
                symbol="TESTINDEX",
                instrument_type="index",
                exchange="NSE",
                trading_date=test_date,
                open_price=Decimal("15000.00"),
                high_price=Decimal("15100.00"),
                low_price=Decimal("14900.00"),
                close_price=Decimal("15050.00"),
                volume=50000000,
                source="TEST_INDEX",
            )

            # Insert
            res1 = upsert_security_daily_prices(session, [bar_equity, bar_index])
            self.assertEqual(res1["inserted_or_updated"], 2)
            self.assertEqual(res1["rejected"], 0)

            # Re-insert with updated close price
            bar_equity_updated = DailyBar(
                symbol="TESTSTOCK",
                instrument_type="equity",
                exchange="NSE",
                trading_date=test_date,
                open_price=Decimal("100.00"),
                high_price=Decimal("105.00"),
                low_price=Decimal("99.00"),
                close_price=Decimal("104.00"),  # updated
                volume=12000,
                source="TEST_EQUITY_UPDATED",
            )
            res2 = upsert_security_daily_prices(session, [bar_equity_updated, bar_index])
            self.assertEqual(res2["inserted_or_updated"], 2)

            # Verify in DB
            stock_rec = session.scalars(
                select(SecurityDailyPrice).where(
                    SecurityDailyPrice.symbol == "TESTSTOCK",
                    SecurityDailyPrice.trading_date == test_date,
                )
            ).first()
            self.assertIsNotNone(stock_rec)
            self.assertEqual(stock_rec.close_price, Decimal("104.0000"))
            self.assertEqual(stock_rec.instrument_type, "equity")

            index_rec = session.scalars(
                select(SecurityDailyPrice).where(
                    SecurityDailyPrice.symbol == "TESTINDEX",
                    SecurityDailyPrice.trading_date == test_date,
                )
            ).first()
            self.assertIsNotNone(index_rec)
            self.assertEqual(index_rec.instrument_type, "index")

            # Clean up test rows
            session.delete(stock_rec)
            session.delete(index_rec)
            session.commit()


class TestNseBroadImporter(unittest.TestCase):
    """Test broad equity filtering and resumable manifest tracking."""

    def test_series_filtering_policy(self):
        sample_csv = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 11-Oct-2024, 2745.00, 2740.00, 2766.55, 2737.65, 2743.00, 2744.20, 2748.50, 3990061, 109668.50, 142100, 2100000, 52.63
20MICRONS, BE, 11-Oct-2024, 250.00, 252.00, 255.00, 248.00, 251.00, 251.50, 251.00, 50000, 125.50, 100, 50000, 100.00
SGBAPR28I, GB, 11-Oct-2024, 7500.00, 7550.00, 7600.00, 7500.00, 7580.00, 7580.00, 7560.00, 500, 37.80, 50, 500, 100.00
1018GS2026, GS, 11-Oct-2024, 100.00, 100.10, 100.20, 100.00, 100.15, 100.15, 100.10, 10000, 10.01, 10, 10000, 100.00
M&MFIN, N3, 11-Oct-2024, 1000.00, 1005.00, 1010.00, 1000.00, 1005.00, 1005.00, 1005.00, 200, 2.01, 5, 200, 100.00
"""
        from app.nse_market_data import DEFAULT_ELIGIBLE_EQUITY_SERIES

        bars = parse_equity_bhavcopy(sample_csv, eligible_series=DEFAULT_ELIGIBLE_EQUITY_SERIES)
        self.assertEqual(len(bars), 2)
        symbols = [b.symbol for b in bars]
        self.assertIn("RELIANCE", symbols)
        self.assertIn("20MICRONS", symbols)
        self.assertNotIn("SGBAPR28I", symbols)  # Sovereign gold bond excluded
        self.assertNotIn("1018GS2026", symbols)  # Govt security excluded
        self.assertNotIn("M&MFIN", symbols)     # Debt NCD excluded

    def test_importer_manifest_tracking(self):
        import tempfile
        import shutil
        from pathlib import Path
        from app.nse_market_data import NSEHistoricalImporter

        temp_dir = Path(tempfile.mkdtemp())
        try:
            importer = NSEHistoricalImporter(base_dir=temp_dir)
            self.assertEqual(importer.manifest, {})
            self.assertFalse(importer.is_date_processed(date(2024, 10, 11)))

            # Record a date in manifest
            importer.manifest["2024-10-11"] = {"status": "COMPLETED", "equity_rows_inserted": 2000}
            importer._save_manifest()

            # Reload and verify
            importer2 = NSEHistoricalImporter(base_dir=temp_dir)
            self.assertTrue(importer2.is_date_processed(date(2024, 10, 11)))
            self.assertFalse(importer2.is_date_processed(date(2024, 10, 12)))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

