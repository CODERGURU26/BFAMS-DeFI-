import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import SecurityDailyPrice
from app.nse_market_data import (
    fetch_nse_bhavcopy_content,
    fetch_nse_index_content,
    parse_equity_bhavcopy,
    parse_index_daily,
    upsert_security_daily_prices,
)


def run_pilot():
    engine = get_test_engine()
    with engine.connect() as conn:
        db_name = conn.execute(text("SELECT current_database()")).scalar()
        if db_name != "BFAMS-DeFI-test":
            print(f"ABORT: Expected 'BFAMS-DeFI-test', got '{db_name}'", file=sys.stderr)
            sys.exit(1)
        print(f"Connected to verified test DB: {db_name}")

    target_date = date(2024, 10, 11)
    raw_dir = Path("data/nse/raw")

    print(f"\n--- Downloading NSE Bhavcopy for {target_date} ---")
    bhav_text = fetch_nse_bhavcopy_content(target_date, raw_dir=raw_dir)
    print(f"Downloaded bhavcopy ({len(bhav_text)} bytes). Preserved in {raw_dir}")

    target_symbols = ["RELIANCE", "TCS", "INFY"]
    equity_bars = parse_equity_bhavcopy(bhav_text, target_symbols=target_symbols)
    print(f"Parsed {len(equity_bars)} equity bars for {target_symbols}:")
    for b in equity_bars:
        print(f"  {b.symbol} | Date: {b.trading_date} | O: {b.open_price} | H: {b.high_price} | L: {b.low_price} | C: {b.close_price} | Vol: {b.volume}")

    print(f"\n--- Downloading NSE Index Data for {target_date} ---")
    index_text = fetch_nse_index_content(target_date, raw_dir=raw_dir)
    print(f"Downloaded index data ({len(index_text)} bytes). Preserved in {raw_dir}")

    target_indices = ["Nifty 50"]
    index_bars = parse_index_daily(index_text, target_indices=target_indices)
    print(f"Parsed {len(index_bars)} index bars for {target_indices}:")
    for b in index_bars:
        print(f"  {b.symbol} | Date: {b.trading_date} | O: {b.open_price} | H: {b.high_price} | L: {b.low_price} | C: {b.close_price} | Vol: {b.volume}")

    all_bars = equity_bars + index_bars

    print(f"\n--- Upserting into BFAMS-DeFI-test (security_daily_prices) ---")
    with Session(engine) as session:
        res = upsert_security_daily_prices(session, all_bars)
        print("Upsert result (Run 1):", res)

        # Test idempotency by re-running upsert
        res_idempotent = upsert_security_daily_prices(session, all_bars)
        print("Upsert result (Run 2 - Idempotent):", res_idempotent)

        # Verify records in database
        records = session.scalars(
            select(SecurityDailyPrice).order_by(SecurityDailyPrice.instrument_type, SecurityDailyPrice.symbol)
        ).all()
        print(f"\nDatabase verification: Total {len(records)} records in security_daily_prices:")
        for r in records:
            print(f"  [{r.instrument_type.upper()}] {r.symbol} ({r.exchange}) | {r.trading_date} | Close: {r.close_price} | Vol: {r.volume} | Source: {r.source}")


if __name__ == "__main__":
    run_pilot()
