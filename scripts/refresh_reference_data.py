"""Refresh AMFI NAV and NSE reference data in BFAMS-DeFI-test."""

import argparse
from datetime import date, datetime, timedelta
import io
import os
from pathlib import Path
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.demat_ingestion import _is_valid_isin, value_equity_holding
from app.models import InvestmentHolding, MfNav, SecurityDailyPrice
from app.mf_valuation import value_investment_holding_record
from app.nav_loader import load_nav
from app.nav_parser import parse_nav_lines
from app.nse_market_data import DEFAULT_BROAD_INDICES, NSEHistoricalImporter


AMFI_NAV_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
AMFI_NAV_FILE = PROJECT_ROOT / "data" / "amfi" / "NAVAll.txt"
NSE_SERIES = ("EQ", "BE", "RR")
NSE_LOOKBACK_DAYS = 7
MAX_NSE_AGE_DAYS = 3
MAX_NAV_AGE_DAYS = 4
AMFI_USER_AGENT = "BFAMS-DeFi reference-data refresh"


def parse_amfi_response(content: bytes, as_of_date: date):
    text_content = content.decode("utf-8-sig")
    if text_content.lstrip().startswith("<"):
        raise ValueError("AMFI returned HTML instead of NAV data.")

    schemes, navs, rejected, stats = parse_nav_lines(io.StringIO(text_content))
    if not schemes or not navs:
        raise ValueError("AMFI response contained no valid schemes or NAVs.")

    latest_nav_date = max(row["nav_date"] for row in navs)
    if latest_nav_date > as_of_date:
        raise ValueError("AMFI response contains a future NAV date.")

    return schemes, navs, rejected, stats, latest_nav_date


def fetch_amfi_nav(url: str = AMFI_NAV_URL, retries: int = 3) -> bytes:
    request = Request(url, headers={"User-Agent": AMFI_USER_AGENT})
    last_error = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read()
            if not content:
                raise ValueError("AMFI returned an empty response.")
            return content
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)

    raise RuntimeError("Unable to download AMFI NAV data.") from last_error


def nse_date_window(as_of_date: date, lookback_days: int = NSE_LOOKBACK_DAYS):
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least one.")
    return as_of_date - timedelta(days=lookback_days - 1), as_of_date


def freshness_warning(
    label: str,
    latest_date: date | None,
    as_of_date: date,
    max_age_days: int,
) -> str | None:
    if latest_date is None:
        return f"{label} has no data."
    age_days = (as_of_date - latest_date).days
    if age_days > max_age_days:
        return (
            f"{label} is stale: latest date {latest_date} "
            f"({age_days} calendar days old)."
        )
    return None


def save_amfi_snapshot(content: bytes) -> None:
    AMFI_NAV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = AMFI_NAV_FILE.with_suffix(".txt.tmp")
    temporary_path.write_bytes(content)
    temporary_path.replace(AMFI_NAV_FILE)


def current_holdings_coverage(session: Session, as_of_date: date):
    latest_snapshots = (
        select(
            InvestmentHolding.account_id.label("account_id"),
            func.max(InvestmentHolding.observed_at).label("observed_at"),
        )
        .group_by(InvestmentHolding.account_id)
        .subquery()
    )
    holdings = session.scalars(
        select(InvestmentHolding).join(
            latest_snapshots,
            and_(
                InvestmentHolding.account_id == latest_snapshots.c.account_id,
                InvestmentHolding.observed_at == latest_snapshots.c.observed_at,
            ),
        )
    ).all()

    priced_count = 0
    for holding in holdings:
        if holding.asset_type == "MUTUAL_FUND":
            result = value_investment_holding_record(
                session,
                holding,
                as_of_date=as_of_date,
            )
            market_value = result.market_value
        elif holding.asset_identifier and _is_valid_isin(holding.asset_identifier):
            result = value_equity_holding(
                session=session,
                isin=holding.asset_identifier,
                asset_name=holding.asset_name,
                asset_type=holding.asset_type,
                quantity=holding.quantity or 0,
                as_of_date=as_of_date,
                dhan_closing_price=holding.unit_price,
            )
            market_value = result.market_value
        else:
            market_value = holding.market_value

        if market_value is not None:
            priced_count += 1

    return priced_count, len(holdings)


def refresh_reference_data(as_of_date: date, lookback_days: int) -> int:
    os.chdir(PROJECT_ROOT)
    from database import get_test_engine

    engine = get_test_engine()
    errors = []
    warnings = []
    try:
        with engine.connect() as connection:
            database_name = connection.execute(
                text("SELECT current_database()")
            ).scalar_one()
        if database_name != "BFAMS-DeFI-test":
            raise RuntimeError(
                f"Safety abort: connected to '{database_name}', expected 'BFAMS-DeFI-test'."
            )
        print(f"Verified database: {database_name}")

        try:
            content = fetch_amfi_nav()
            schemes, navs, rejected, stats, latest_nav_date = parse_amfi_response(
                content,
                as_of_date,
            )
            save_amfi_snapshot(content)
            with Session(engine) as session:
                load_result = load_nav(
                    session,
                    schemes,
                    navs,
                    rejected,
                    source="AMFI_NAVAll",
                )
            print(
                "AMFI: "
                f"{len(schemes)} schemes, {len(navs)} NAV rows parsed, "
                f"{load_result['navs_inserted']} new NAV rows, "
                f"{load_result['rejected']} rejected, latest NAV {latest_nav_date}."
            )
            if load_result["rejected"]:
                warnings.append(
                    f"AMFI rejected or skipped {load_result['rejected']} NAV/scheme rows."
                )
            warning = freshness_warning(
                "AMFI NAV data",
                latest_nav_date,
                as_of_date,
                MAX_NAV_AGE_DAYS,
            )
            if warning:
                warnings.append(warning)
        except (RuntimeError, ValueError, URLError, TimeoutError, OSError, SQLAlchemyError) as exc:
            errors.append(f"AMFI refresh failed: {type(exc).__name__}: {exc}")

        start_date, end_date = nse_date_window(as_of_date, lookback_days)
        try:
            with NSEHistoricalImporter(
                base_dir=PROJECT_ROOT / "data" / "nse",
                eligible_series=NSE_SERIES,
                indices_to_track=DEFAULT_BROAD_INDICES,
            ) as importer, Session(engine) as session:
                nse_result = importer.import_bounded_batch(
                    session=session,
                    start_date=start_date,
                    end_date=end_date,
                    pause_seconds=0.1,
                    force=True,
                )
            print(
                "NSE: "
                f"{nse_result['trading_days_imported']} trading dates imported/reprocessed, "
                f"{nse_result['non_trading_days']} non-trading dates, "
                f"{nse_result['failed_count']} failed dates "
                f"({start_date} through {end_date}; series {','.join(NSE_SERIES)})."
            )
            if nse_result["failed_count"]:
                errors.append(
                    f"NSE import failed for {nse_result['failed_count']} date(s): "
                    f"{nse_result['failed_dates']}"
                )
        except (RuntimeError, SQLAlchemyError, OSError) as exc:
            errors.append(f"NSE refresh failed: {type(exc).__name__}: {exc}")

        with Session(engine) as session:
            latest_prices = session.execute(
                select(
                    SecurityDailyPrice.series,
                    func.max(SecurityDailyPrice.trading_date),
                )
                .where(SecurityDailyPrice.instrument_type == "equity")
                .group_by(SecurityDailyPrice.series)
            ).all()
            latest_nav_date = session.scalar(select(func.max(MfNav.nav_date)))
            priced_count, holding_count = current_holdings_coverage(
                session,
                as_of_date,
            )

        for series, latest_date in latest_prices:
            warning = freshness_warning(
                f"NSE {series or 'unclassified'} price data",
                latest_date,
                as_of_date,
                MAX_NSE_AGE_DAYS,
            )
            if warning:
                warnings.append(warning)
            print(f"NSE {series or 'unclassified'} latest date: {latest_date}")

        warning = freshness_warning(
            "Stored AMFI NAV data",
            latest_nav_date,
            as_of_date,
            MAX_NAV_AGE_DAYS,
        )
        if warning:
            warnings.append(warning)
        print(f"Stored AMFI NAV latest date: {latest_nav_date}")

        if holding_count:
            print(
                f"Latest portfolio coverage: {priced_count}/{holding_count} holdings priced."
            )
            if priced_count < holding_count:
                warnings.append(
                    f"Portfolio price coverage is partial: {priced_count}/{holding_count} "
                    "latest holdings have a price or NAV."
                )
        else:
            print("Latest portfolio coverage: no holdings found.")

    finally:
        engine.dispose()

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=NSE_LOOKBACK_DAYS,
        help="Calendar days to reprocess for late NSE files (default: 7).",
    )
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        default=date.today(),
        help="Valuation/import end date in YYYY-MM-DD format (default: today).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.lookback_days < 1:
        print("ERROR: --lookback-days must be at least one.")
        return 2
    try:
        return refresh_reference_data(args.as_of_date, args.lookback_days)
    except (RuntimeError, SQLAlchemyError, OSError) as exc:
        print(f"ERROR: Reference-data refresh aborted: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
