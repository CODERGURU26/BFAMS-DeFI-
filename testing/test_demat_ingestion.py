import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import InvestmentHolding, FinancialAccount, FinancialConnection, User, DataProvider
from app.demat_ingestion import (
    parse_holdings_csv,
    parse_transactions_csv,
    ingest_demat_holdings,
    RowError,
)


class TestDematIngestionParser(unittest.TestCase):
    """Test CSV parsing, normalization, and row-level validation."""

    def test_valid_holdings_csv_standard_columns(self):
        csv_data = """symbol,isin,asset_name,quantity,price,market_value
RELIANCE,INE002A01018,Reliance Industries Ltd,100,2740.00,274000.00
TCS,INE467B01029,Tata Consultancy Services,50,4140.00,207000.00
INFY,INE009A01021,Infosys Limited,200,1930.00,386000.00
"""
        stmt_date = date(2024, 10, 11)
        accepted, errors, dups = parse_holdings_csv(csv_data, statement_date=stmt_date)

        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(errors), 0)
        self.assertEqual(dups, 0)

        rel = accepted[0]
        self.assertEqual(rel.symbol, "RELIANCE")
        self.assertEqual(rel.isin, "INE002A01018")
        self.assertEqual(rel.quantity, Decimal("100"))
        self.assertEqual(rel.unit_price, Decimal("2740.00"))
        self.assertEqual(rel.market_value, Decimal("274000.00"))
        self.assertEqual(rel.asset_type, "EQUITY")
        self.assertEqual(rel.currency, "INR")

    def test_alternate_broker_column_names(self):
        # Zerodha / Groww style columns: 'Stock Symbol', 'Holding Qty', 'Buy Avg', 'Cur Val'
        csv_data = """Stock Symbol,Instrument ISIN,Company Name,Holding Qty,Buy Avg,Cur Val
HDFCBANK,INE040A01034,HDFC Bank Limited,75,1600.50,120037.50
TATAMOTORS,INE155A01022,Tata Motors Limited,120,950.00,114000.00
"""
        accepted, errors, dups = parse_holdings_csv(csv_data)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(errors), 0)
        self.assertEqual(accepted[0].symbol, "HDFCBANK")
        self.assertEqual(accepted[0].quantity, Decimal("75"))
        self.assertEqual(accepted[0].unit_price, Decimal("1600.50"))

    def test_invalid_dates_and_numeric_values(self):
        csv_data = """symbol,quantity,price
RELIANCE,-50,2740.00
TCS,not_a_number,4140.00
INFY,100,invalid_price
"""
        accepted, errors, dups = parse_holdings_csv(csv_data)
        self.assertEqual(len(accepted), 1)  # INFY accepted with null price
        self.assertEqual(len(errors), 2)

        # Row 2 (RELIANCE): negative quantity
        err_row2 = next(e for e in errors if e.row_number == 2)
        self.assertEqual(err_row2.field, "quantity")
        self.assertIn("Invalid or negative quantity", err_row2.reason)

        # Row 3 (TCS): not a number quantity
        err_row3 = next(e for e in errors if e.row_number == 3)
        self.assertEqual(err_row3.field, "quantity")

    def test_missing_identifiers(self):
        csv_data = """symbol,isin,asset_name,quantity,price
,,,100,2500.00
RELIANCE,INE002A01018,Reliance Industries,50,2700.00
"""
        accepted, errors, dups = parse_holdings_csv(csv_data)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].row_number, 2)
        self.assertIn("Missing symbol, ISIN, and asset name", errors[0].reason)

    def test_duplicate_rows_in_file(self):
        csv_data = """symbol,isin,quantity,price
RELIANCE,INE002A01018,100,2740.00
RELIANCE,INE002A01018,50,2750.00
"""
        accepted, errors, dups = parse_holdings_csv(csv_data)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(dups, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Duplicate holding row in file", errors[0].reason)

    def test_parse_transactions_csv(self):
        csv_txns = """date,type,symbol,quantity,price,amount
11-10-2024,BUY,RELIANCE,10,2740.00,27400.00
11-10-2024,SELL,TCS,5,4150.00,20750.00
12-10-2024,INVALID_TYPE,INFY,10,1930.00,19300.00
"""
        accepted, errors, dups = parse_transactions_csv(csv_txns)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].row_number, 4)
        self.assertIn("Unknown transaction type", errors[0].reason)

        self.assertEqual(accepted[0].transaction_type, "BUY")
        self.assertEqual(accepted[0].amount, Decimal("27400.00"))
        self.assertEqual(accepted[1].transaction_type, "SELL")


class TestDematIngestionDatabaseSafety(unittest.TestCase):
    """Test dry-run mode and database idempotency against BFAMS-DeFI-test."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        # Find or create a test financial account
        with Session(cls.engine) as session:
            acc = session.scalars(select(FinancialAccount)).first()
            if acc:
                cls.account_id = acc.id
            else:
                # Create fixture account
                user = User()
                provider = DataProvider(provider_code="DEMAT_TEST", provider_name="Demat Provider")
                session.add_all([user, provider])
                session.flush()
                conn = FinancialConnection(user_id=user.id, provider_id=provider.id)
                session.add(conn)
                session.flush()
                acc = FinancialAccount(
                    connection_id=conn.id,
                    account_type="DEMAT",
                    account_name="Test Demat Account",
                )
                session.add(acc)
                session.commit()
                cls.account_id = acc.id

    def test_dry_run_makes_zero_database_writes(self):
        csv_data = """symbol,isin,asset_name,quantity,price,market_value
SYNTHETIC_STOCK,INE999X01011,Synthetic Stock Corp,100,500.00,50000.00
"""
        with Session(self.engine) as session:
            # Count before
            count_before = session.scalar(
                select(InvestmentHolding.id).where(
                    InvestmentHolding.asset_identifier == "INE999X01011"
                )
            )
            self.assertIsNone(count_before)

            # Dry-run execution
            summary = ingest_demat_holdings(
                session=session,
                account_id=self.account_id,
                csv_text=csv_data,
                statement_date=date(2024, 10, 11),
                dry_run=True,
            )

            self.assertTrue(summary.dry_run)
            self.assertEqual(summary.accepted_count, 1)

            # Verify zero DB writes occurred
            count_after = session.scalar(
                select(InvestmentHolding.id).where(
                    InvestmentHolding.asset_identifier == "INE999X01011"
                )
            )
            self.assertIsNone(count_after)

    def test_idempotent_ingestion_and_cleanup(self):
        csv_data = """symbol,isin,asset_name,quantity,price,market_value
IDEMP_STOCK,INE888Y01012,Idempotent Test Corp,25,200.00,5000.00
"""
        stmt_date = date(2024, 10, 11)
        with Session(self.engine) as session:
            # 1. First run: should insert
            sum1 = ingest_demat_holdings(
                session=session,
                account_id=self.account_id,
                csv_text=csv_data,
                statement_date=stmt_date,
                dry_run=False,
            )
            self.assertEqual(sum1.accepted_count, 1)

            # 2. Second run: should detect duplicate and insert 0
            sum2 = ingest_demat_holdings(
                session=session,
                account_id=self.account_id,
                csv_text=csv_data,
                statement_date=stmt_date,
                dry_run=False,
            )
            self.assertEqual(sum2.accepted_count, 0)
            self.assertEqual(sum2.duplicate_count, 1)

            # Clean up test row
            holding = session.scalars(
                select(InvestmentHolding).where(
                    InvestmentHolding.account_id == self.account_id,
                    InvestmentHolding.asset_identifier == "INE888Y01012",
                )
            ).first()
            if holding:
                session.delete(holding)
                session.commit()


if __name__ == "__main__":
    unittest.main()
