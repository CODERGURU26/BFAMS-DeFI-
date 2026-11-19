"""Persist daily Dhan portfolio valuations in the guarded staging database."""

import argparse
from datetime import date
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.portfolio_history import capture_daily_portfolio_snapshots
from database import engine, settings


TARGET_DATABASE = "BFAMS-DeFI"


def _verify_staging_database(session: Session) -> None:
    database_name = session.execute(
        text("SELECT current_database()")
    ).scalar_one()
    if settings.db_name != TARGET_DATABASE or database_name != TARGET_DATABASE:
        raise RuntimeError(
            "Daily portfolio snapshots are restricted to BFAMS-DeFI staging."
        )


def create_snapshots(as_of_date: date) -> int:
    with Session(engine) as session:
        _verify_staging_database(session)
        try:
            results = capture_daily_portfolio_snapshots(session, as_of_date)
            session.commit()
        except Exception:
            session.rollback()
            raise

    if not results:
        print("No active Dhan accounts with holdings; no snapshots created.")
        return 0

    for result in results:
        print(
            f"{result['valuation_date']} | account {result['account_id']} | "
            f"{result['priced_holdings']}/{result['total_holdings']} priced | "
            f"{result['stale_holdings']} stale | "
            f"{result['unpriced_holdings']} missing | "
            f"INR {result['total_market_value']}"
        )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("main",),
        required=True,
        help="Explicitly select the guarded BFAMS-DeFI staging database.",
    )
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        default=date.today(),
        help="Valuation date in YYYY-MM-DD format (default: today).",
    )
    args = parser.parse_args(argv)
    try:
        return create_snapshots(args.as_of_date)
    except (RuntimeError, SQLAlchemyError, OSError) as exc:
        print(
            f"ERROR: Daily portfolio snapshot failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
