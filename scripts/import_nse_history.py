import argparse
from datetime import date, datetime
import logging
import os
from pathlib import Path
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text, func
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import SecurityDailyPrice
from app.nse_market_data import (
    DEFAULT_ELIGIBLE_EQUITY_SERIES,
    DEFAULT_BROAD_INDICES,
    NSEHistoricalImporter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nse_history_importer")


def parse_args():
    parser = argparse.ArgumentParser(description="BFAMS-DeFi Resumable NSE Historical Data Importer")
    parser.add_argument(
        "--start-date",
        type=str,
        default="2024-10-01",
        help="Start date in YYYY-MM-DD format (default: 2024-10-01)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default="2024-10-15",
        help="End date in YYYY-MM-DD format (default: 2024-10-15)",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=None,
        help="Maximum trading days to import in this execution batch (default: all in date range)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.25,
        help="Pause in seconds between downloads for polite rate limiting (default: 0.25)",
    )
    parser.add_argument(
        "--series",
        type=str,
        default="EQ,BE",
        help="Comma-separated eligible equity series (default: EQ,BE)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reprocess dates even if already recorded in manifest",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    start_dt = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    series_list = [s.strip().upper() for s in args.series.split(",") if s.strip()]

    print("=" * 70)
    print("BFAMS-DeFi — Resumable NSE Historical Market Data Importer")
    print(f"Date Range    : {start_dt} to {end_dt}")
    print(f"Max Days      : {args.max_days or 'Unlimited'}")
    print(f"Equity Series : {series_list}")
    print(f"Indices       : {list(DEFAULT_BROAD_INDICES)}")
    print("=" * 70)

    # 1. Database Safety Verification
    engine = get_test_engine()
    with engine.connect() as conn:
        connected_db = conn.execute(text("SELECT current_database()")).scalar()
        if connected_db != "BFAMS-DeFI-test":
            print(f"CRITICAL SAFETY ABORT: Connected DB is '{connected_db}', expected 'BFAMS-DeFI-test'", file=sys.stderr)
            sys.exit(1)
        print(f"Verified Database: {connected_db} (STRICTLY ISOLATED TEST DB)")

    # 2. Baseline Record Counts
    with Session(engine) as session:
        count_equity_before = session.scalar(
            select(func.count()).select_from(SecurityDailyPrice).where(SecurityDailyPrice.instrument_type == "equity")
        )
        count_index_before = session.scalar(
            select(func.count()).select_from(SecurityDailyPrice).where(SecurityDailyPrice.instrument_type == "index")
        )
        print(f"Baseline rows in security_daily_prices: Equity={count_equity_before}, Index={count_index_before}")

    # 3. Initialize Importer
    importer = NSEHistoricalImporter(
        base_dir=Path("data/nse"),
        eligible_series=series_list,
        indices_to_track=DEFAULT_BROAD_INDICES,
    )
    print(f"Manifest path: {importer.manifest_path} ({len(importer.manifest)} dates currently tracked)")

    # 4. Execute Batch
    print("\nStarting bounded batch processing...")
    with Session(engine) as session:
        batch_summary = importer.import_bounded_batch(
            session=session,
            start_date=start_dt,
            end_date=end_dt,
            max_trading_days=args.max_days,
            pause_seconds=args.pause,
        )

    # 5. Post-Import Verification
    with Session(engine) as session:
        count_equity_after = session.scalar(
            select(func.count()).select_from(SecurityDailyPrice).where(SecurityDailyPrice.instrument_type == "equity")
        )
        count_index_after = session.scalar(
            select(func.count()).select_from(SecurityDailyPrice).where(SecurityDailyPrice.instrument_type == "index")
        )
        distinct_securities = session.scalar(
            select(func.count(func.distinct(SecurityDailyPrice.symbol))).where(SecurityDailyPrice.instrument_type == "equity")
        )
        distinct_series = session.scalars(
            select(func.distinct(SecurityDailyPrice.series)).where(SecurityDailyPrice.instrument_type == "equity")
        ).all()
        min_date, max_date = session.execute(
            text("SELECT min(trading_date), max(trading_date) FROM security_daily_prices")
        ).fetchone()

    print("\n" + "=" * 70)
    print("BATCH EXECUTION SUMMARY")
    print("=" * 70)
    print(f"Calendar Days Inspected    : {batch_summary['days_inspected']}")
    print(f"Trading Days Imported      : {batch_summary['trading_days_imported']}")
    print(f"Non-Trading Days (Holiday) : {batch_summary['non_trading_days']}")
    print(f"Skipped (Already in Cache) : {batch_summary['skipped_existing']}")
    print(f"Equity Records Inserted    : {batch_summary['total_equity_rows']}")
    print(f"Index Records Inserted     : {batch_summary['total_index_rows']}")
    print(f"Failed Dates Count         : {batch_summary['failed_count']}")
    if batch_summary['failed_dates']:
        print(f"Failed Dates Detail        : {batch_summary['failed_dates']}")

    print("\nDATABASE TOTALS (security_daily_prices):")
    print(f"Total Equity Rows          : {count_equity_after} (+{count_equity_after - count_equity_before})")
    print(f"Total Index Rows           : {count_index_after} (+{count_index_after - count_index_before})")
    print(f"Distinct Equity Symbols    : {distinct_securities}")
    print(f"Distinct Series Stored     : {distinct_series}")
    print(f"Date Span in Database      : {min_date} to {max_date}")
    print("=" * 70)


if __name__ == "__main__":
    main()
