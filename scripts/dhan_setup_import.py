"""
Dhan demat account setup and holdings import.

Subcommands
-----------
  account   Create provider, institution, user, connection, and account rows.
  holdings  Parse and persist the Dhan demat holding CSV.
  trades    Parse and persist the Dhan past-trades CSV.

Both default to dry-run (rollback). Pass --apply to commit.

Notes
-----
- ingest_demat_holdings() commits internally and hardcodes observed_at to UTC.
  The user requires IST (+05:30). Therefore holdings persistence replicates the
  insert loop from ingest_demat_holdings() with the correct timezone.
"""

import argparse
import csv
import io
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.demat_ingestion import (
    ISIN_TO_NSE_SYMBOL,
    _strip_dhan_metadata_header,
    ingest_demat_holdings,
    ingest_demat_transactions,
    parse_dhan_holdings_csv,
)
from app.models import (
    DataProvider,
    DataSyncRun,
    FinancialAccount,
    FinancialConnection,
    FinancialInstitution,
    InvestmentHolding,
    MfScheme,
    SecurityDailyPrice,
    Transaction,
    User,
)
from database import get_test_engine

IST = timezone(timedelta(hours=5, minutes=30))
HOLDINGS_FILE = Path("data/user_holdings/Dhan_Demat_Holding_14-10-2026.csv")
TRADES_FILE = Path("data/user_holdings/trades.csv")
STATEMENT_DATE = date(2026, 10, 14)
EXPECTED_OBSERVED_AT = datetime(2026, 10, 14, 0, 0, 0, tzinfo=IST)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _abort(msg: str):
    print(f"ABORT: {msg}")
    sys.exit(1)


def _verify_test_db(session: Session):
    db = session.scalar(text("SELECT current_database()"))
    if db != "BFAMS-DeFI-test":
        _abort(f"connected to '{db}', expected 'BFAMS-DeFI-test'")


def _table_counts(session: Session) -> dict:
    tables = [
        ("users", User),
        ("data_providers", DataProvider),
        ("financial_institutions", FinancialInstitution),
        ("financial_connections", FinancialConnection),
        ("financial_accounts", FinancialAccount),
        ("transactions", Transaction),
        ("investment_holdings", InvestmentHolding),
        ("data_sync_runs", DataSyncRun),
    ]
    return {n: session.scalar(select(func.count()).select_from(m)) for n, m in tables}


def _print_counts(counts: dict, label: str):
    print(f"\n--- {label} ---")
    for name, cnt in counts.items():
        print(f"  {name}: {cnt}")


def cmd_status(session: Session):
    _verify_test_db(session)
    account_exists = session.scalar(
        select(func.count())
        .select_from(FinancialAccount)
        .where(FinancialAccount.external_account_id == "DHAN-DEMAT-1")
    )
    provider_exists = session.scalar(
        select(func.count())
        .select_from(DataProvider)
        .where(DataProvider.provider_code == "dhan_manual")
    )
    connection_exists = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.external_connection_id == "DHAN-MANUAL-1")
    )
    print(f"account DHAN-DEMAT-1 exists: {bool(account_exists)}")
    print(f"provider dhan_manual exists: {bool(provider_exists)}")
    print(f"connection DHAN-MANUAL-1 exists: {bool(connection_exists)}")
    print(f"users: {session.scalar(select(func.count()).select_from(User))}")
    print(
        "financial_accounts: "
        f"{session.scalar(select(func.count()).select_from(FinancialAccount))}"
    )
    print(
        "investment_holdings: "
        f"{session.scalar(select(func.count()).select_from(InvestmentHolding))}"
    )
    print(
        "data_sync_runs: "
        f"{session.scalar(select(func.count()).select_from(DataSyncRun))}"
    )


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------

def cmd_account(session: Session, apply: bool):
    _verify_test_db(session)
    _print_counts(_table_counts(session), "Counts BEFORE")
    actions: list[str] = []

    # DataProvider
    prov = session.scalars(
        select(DataProvider).where(DataProvider.provider_code == "dhan_manual")
    ).first()
    if prov:
        actions.append("data_providers: dhan_manual EXISTS")
    else:
        prov = DataProvider(
            provider_code="dhan_manual",
            provider_name="Dhan manual import",
            provider_type="manual_import",
        )
        session.add(prov)
        session.flush()
        actions.append("data_providers: +1 (dhan_manual)")

    # FinancialInstitution
    inst = session.scalars(
        select(FinancialInstitution).where(
            FinancialInstitution.institution_code == "DHAN"
        )
    ).first()
    if inst:
        actions.append("financial_institutions: DHAN EXISTS")
    else:
        inst = FinancialInstitution(
            institution_name="Dhan",
            institution_code="DHAN",
            institution_type="broker",
            country_code="IN",
        )
        session.add(inst)
        session.flush()
        actions.append("financial_institutions: +1 (DHAN)")

    # User — separate from seed data
    existing_conn = session.scalars(
        select(FinancialConnection).where(
            FinancialConnection.external_connection_id == "DHAN-MANUAL-1"
        )
    ).first()
    if existing_conn:
        user = session.get(User, existing_conn.user_id)
        actions.append("users: Dhan user EXISTS (via DHAN-MANUAL-1)")
    else:
        user = User()
        session.add(user)
        session.flush()
        actions.append("users: +1 (new Dhan user)")

    # FinancialConnection
    conn = existing_conn
    if conn:
        actions.append("financial_connections: DHAN-MANUAL-1 EXISTS")
    else:
        conn = FinancialConnection(
            user_id=user.id,
            provider_id=prov.id,
            external_connection_id="DHAN-MANUAL-1",
            status="active",
        )
        session.add(conn)
        session.flush()
        actions.append("financial_connections: +1 (DHAN-MANUAL-1)")

    # FinancialAccount
    acc = session.scalars(
        select(FinancialAccount).where(
            FinancialAccount.external_account_id == "DHAN-DEMAT-1"
        )
    ).first()
    if acc:
        actions.append("financial_accounts: DHAN-DEMAT-1 EXISTS")
    else:
        acc = FinancialAccount(
            connection_id=conn.id,
            institution_id=inst.id,
            external_account_id="DHAN-DEMAT-1",
            account_type="demat",
            account_name="Dhan Demat",
            currency="INR",
            status="active",
        )
        session.add(acc)
        session.flush()
        actions.append("financial_accounts: +1 (DHAN-DEMAT-1)")

    print("\n--- Actions ---")
    for a in actions:
        print(f"  {a}")

    if apply:
        session.commit()
        print("\nCOMMITTED.")
    else:
        session.rollback()
        print("\nDRY RUN — rolled back.")

    _print_counts(_table_counts(session), "Counts AFTER")


# ---------------------------------------------------------------------------
# holdings
# ---------------------------------------------------------------------------

def cmd_holdings(session: Session, apply: bool):
    _verify_test_db(session)

    # Require DHAN-DEMAT-1
    acc = session.scalars(
        select(FinancialAccount).where(
            FinancialAccount.external_account_id == "DHAN-DEMAT-1"
        )
    ).first()
    if acc is None:
        _abort("account DHAN-DEMAT-1 not found. Run 'account --apply' first.")
    account_id = acc.id
    connection_id = acc.connection_id

    # Read CSV
    if not HOLDINGS_FILE.exists():
        _abort(f"file not found: {HOLDINGS_FILE}")
    csv_text = HOLDINGS_FILE.read_text(encoding="utf-8-sig")

    # ---- Parse ----
    parsed_accepted, parsed_errors, file_dups = parse_dhan_holdings_csv(
        csv_text, statement_date=STATEMENT_DATE
    )
    summary = ingest_demat_holdings(
        session,
        account_id,
        csv_text,
        statement_date=STATEMENT_DATE,
        dry_run=True,
        parser="dhan",
    )
    accepted = summary.parsed_items
    errors = summary.errors
    if (
        len(accepted) != len(parsed_accepted)
        or len(errors) != len(parsed_errors)
        or summary.duplicate_count != file_dups
    ):
        _abort("parser and ingestion results differ")
    for item in accepted:
        if item.observed_at.tzinfo is None:
            _abort(f"observed_at is naive for row {item.row_number}")
        if item.observed_at.date() != STATEMENT_DATE:
            _abort(f"statement date differs for row {item.row_number}")
        item.observed_at = EXPECTED_OBSERVED_AT

    total = len(accepted) + len(errors)
    print(f"Rows in file: {total}")
    print(f"Accepted: {len(accepted)}")
    print(f"Rejected: {len(errors)}")
    if errors:
        for e in errors:
            print(f"  Row {e.row_number}: {e.field}")

    # ---- DB duplicate check ----
    would_insert = 0
    already_existing = 0
    for item in accepted:
        existing = session.scalars(
            select(InvestmentHolding.id).where(
                InvestmentHolding.account_id == account_id,
                InvestmentHolding.asset_identifier == item.asset_identifier,
                InvestmentHolding.observed_at == EXPECTED_OBSERVED_AT,
            )
        ).first()
        if existing is not None:
            already_existing += 1
        else:
            would_insert += 1

    print(f"Would insert: {would_insert}")
    print(f"Already existing: {already_existing}")

    # ---- Asset type breakdown ----
    type_counts: dict[str, int] = {}
    for item in accepted:
        t = item.asset_type or "unclassified"
        type_counts[t] = type_counts.get(t, 0) + 1
    print("\nCounts by asset_type:")
    for t in sorted(type_counts):
        print(f"  {t}: {type_counts[t]}")

    # Keyword-only classification
    keyword_rows = [
        item.row_number
        for item in accepted
        if item.asset_type == "MUTUAL_FUND"
        and not (item.isin or "").upper().startswith("INF")
    ]
    print(f"Rows classified by GROWTH/MUTUAL FUND keyword only: {len(keyword_rows)}")
    if keyword_rows:
        print(f"  Rows: {keyword_rows}")

    # ---- QUANTITY CHECK: pledge/locked columns ----
    stripped = _strip_dhan_metadata_header(csv_text)
    reader = csv.DictReader(io.StringIO(stripped))
    pledge_cols = ["Locked In", "Safe Keep", "MTF Pledge", "Margin Pledge", "CUSA Pledge"]
    pledge_rows: list[int] = []
    for idx, row in enumerate(reader, start=2):
        for col in pledge_cols:
            raw = (row.get(col) or "0").strip().strip('"')
            try:
                if Decimal(raw) > 0:
                    pledge_rows.append(idx)
                    break
            except (InvalidOperation, ValueError):
                pass
    print(f"\nQUANTITY CHECK — rows with non-zero pledge/locked: {len(pledge_rows)}")
    if pledge_rows:
        print(f"  Rows: {pledge_rows}")
        print("  *** STOP: non-zero pledged quantities. Awaiting instructions. ***")
        if apply:
            _abort("refusing --apply with unresolved pledges.")

    # ---- Price consistency (qty*price vs market_value >0.5%) ----
    price_mismatch: list[int] = []
    for item in accepted:
        if (
            item.quantity
            and item.unit_price
            and item.market_value
            and item.market_value != 0
        ):
            computed = item.quantity * item.unit_price
            diff_pct = abs(computed - item.market_value) / item.market_value * 100
            if diff_pct > Decimal("0.5"):
                price_mismatch.append(item.row_number)
    print(f"Price mismatch (qty*price vs market_value >0.5%): {len(price_mismatch)}")
    if price_mismatch:
        print(f"  Rows: {price_mismatch}")

    # ---- ISIN resolution ----
    with_isin = sum(1 for i in accepted if i.isin)
    in_mf = 0
    in_sdp = 0
    in_neither = 0
    for item in accepted:
        if not item.isin:
            continue
        found_mf = session.scalar(
            select(func.count())
            .select_from(MfScheme)
            .where(
                (MfScheme.isin_primary == item.isin)
                | (MfScheme.isin_reinvestment == item.isin)
            )
        )
        entry = ISIN_TO_NSE_SYMBOL.get(item.isin)
        found_sdp = False
        if entry:
            found_sdp = (
                session.scalar(
                    select(func.count())
                    .select_from(SecurityDailyPrice)
                    .where(SecurityDailyPrice.symbol == entry[0])
                )
                or 0
            ) > 0
        if found_mf and found_mf > 0:
            in_mf += 1
        elif found_sdp:
            in_sdp += 1
        else:
            in_neither += 1

    print(f"\nISIN resolution:")
    print(f"  With ISIN: {with_isin}")
    print(f"  In mf_schemes: {in_mf}")
    print(f"  In security_daily_prices: {in_sdp}")
    print(f"  In neither: {in_neither}")

    print(f"\nobserved_at required: {EXPECTED_OBSERVED_AT.isoformat()}")
    timestamp_matches = all(
        item.observed_at.tzinfo is not None
        and item.observed_at == EXPECTED_OBSERVED_AT
        for item in accepted
    )
    print(f"observed_at parsed value matches required IST timestamp: {timestamp_matches}")

    if not apply:
        print("\nDRY RUN — no writes.")
        return

    if not timestamp_matches:
        _abort("ingestion parser does not produce the required IST observed_at")

    def set_required_observed_at(current_session, _flush_context, instances):
        for instance in instances or current_session.new:
            if (
                isinstance(instance, InvestmentHolding)
                and instance.account_id == account_id
            ):
                if instance.observed_at.tzinfo is None:
                    _abort("ingestion produced a naive observed_at")
                if instance.observed_at.date() != STATEMENT_DATE:
                    _abort("ingestion produced a different statement date")
                instance.observed_at = EXPECTED_OBSERVED_AT

    event.listen(session, "before_flush", set_required_observed_at)
    try:
        # ingest_demat_holdings commits internally.
        applied = ingest_demat_holdings(
            session,
            account_id,
            csv_text,
            statement_date=STATEMENT_DATE,
            dry_run=False,
            parser="dhan",
        )
    finally:
        event.remove(session, "before_flush", set_required_observed_at)
    inserted = applied.accepted_count

    # Audit row (separate commit, since holdings were already committed)
    session.add(
        DataSyncRun(
            connection_id=connection_id,
            sync_type="holdings_only",
            status="completed",
            records_processed=inserted,
            completed_at=datetime.now(IST),
        )
    )
    session.commit()
    print("Audit row committed (data_sync_runs).")

    _print_counts(_table_counts(session), "Counts AFTER")


def cmd_trades(session: Session, apply: bool):
    _verify_test_db(session)
    account = session.scalars(
        select(FinancialAccount).where(
            FinancialAccount.external_account_id == "DHAN-DEMAT-1"
        )
    ).first()
    if account is None:
        _abort("account DHAN-DEMAT-1 not found")
    if not TRADES_FILE.exists():
        _abort("trades file not found")

    csv_text = TRADES_FILE.read_text(encoding="utf-8-sig")
    summary = ingest_demat_transactions(
        session,
        account.id,
        csv_text,
        dry_run=True,
        parser="dhan",
    )

    db_duplicates = 0
    for item in summary.parsed_items:
        identifier = item.symbol or item.isin or "UNKNOWN"
        if item.symbol:
            identifier = f"NAME:{re.sub(r'[^A-Z0-9]', '', item.symbol.upper())}"
        external_id = (
            f"{item.transaction_at.date()}|"
            f"{item.transaction_type}|"
            f"{identifier}|"
            f"{item.quantity}|{item.price}"
        )
        if session.scalars(
            select(Transaction.id).where(
                Transaction.account_id == account.id,
                Transaction.external_transaction_id == external_id,
            )
        ).first() is not None:
            db_duplicates += 1

    would_insert = max(0, summary.accepted_count - db_duplicates)
    print(f"Rows parsed: {summary.total_rows}")
    print(f"Accepted: {summary.accepted_count}")
    print(f"Rejected: {summary.rejected_count}")
    print(f"File duplicates: {summary.duplicate_count}")
    print(f"Already existing: {db_duplicates}")
    print(f"Would insert: {would_insert}")
    if summary.errors:
        for error in summary.errors:
            print(f"Row {error.row_number}: {error.field or 'rejected'}")

    if not apply:
        print("DRY RUN — no writes.")
        return

    result = ingest_demat_transactions(
        session,
        account.id,
        csv_text,
        dry_run=False,
        parser="dhan",
    )
    print(f"Transactions inserted: {result.accepted_count}")
    print(f"Transactions skipped as duplicates: {result.duplicate_count}")
    print(f"Total transactions: {session.scalar(select(func.count()).select_from(Transaction))}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Dhan demat setup & holdings import")
    p.add_argument("command", choices=["account", "holdings", "trades"])
    p.add_argument("--apply", action="store_true", help="Commit (default: dry run)")
    p.add_argument("--status", action="store_true", help="Read-only database status")
    args = p.parse_args()

    engine = get_test_engine()
    try:
        if args.status:
            if args.apply:
                _abort("--status cannot be combined with --apply")
            with Session(engine) as session:
                cmd_status(session)
        elif args.command in ("holdings", "trades") and not args.apply:
            with engine.connect() as conn:
                outer_transaction = conn.begin()
                try:
                    with Session(
                        bind=conn, join_transaction_mode="create_savepoint"
                    ) as session:
                        if args.command == "holdings":
                            cmd_holdings(session, apply=False)
                        else:
                            cmd_trades(session, apply=False)
                finally:
                    outer_transaction.rollback()
                    with Session(engine) as verify_session:
                        _verify_test_db(verify_session)
                        if args.command == "holdings":
                            count = verify_session.scalar(
                                select(func.count()).select_from(InvestmentHolding)
                            )
                            print(f"investment_holdings after dry run: {count}")
                        else:
                            count = verify_session.scalar(
                                select(func.count()).select_from(Transaction)
                            )
                            print(f"transactions after dry run: {count}")
        else:
            with Session(engine) as session:
                if args.command == "account":
                    cmd_account(session, args.apply)
                elif args.command == "holdings":
                    cmd_holdings(session, args.apply)
                else:
                    cmd_trades(session, args.apply)
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: {type(e).__name__}")
        raise


if __name__ == "__main__":
    main()
