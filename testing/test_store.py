from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.ingestion import validate_accounts, validate_transactions
from app.models import DataSyncRun, FinancialConnection, FinancialAccount, Transaction
from app.store import store_sync

engine = get_test_engine()

raw_accounts = [
    {"external_account_id": "A1", "account_type": "savings", "status": "active"},
    {"external_account_id": "", "account_type": "savings"},
]
base = {"transaction_type": "credit", "amount": "100.00",
        "transaction_at": "2026-10-01T10:00:00+05:30"}
raw_txns = [
    {**base, "external_transaction_id": "T1"},
    {**base, "external_transaction_id": "T4", "amount": "12.50", "transaction_type": "debit"},
    {**base, "external_transaction_id": "T5", "amount": "0"},
]

accounts, rej_a = validate_accounts(raw_accounts)
txns, rej_t = validate_transactions(raw_txns)

with Session(engine) as session:
    conn = session.scalars(
        select(FinancialConnection).where(
            FinancialConnection.external_connection_id == "TEST-CONNECTION-1"
        )
    ).one()
    for attempt in (1, 2):
        run = store_sync(session, conn.id, accounts, {"A1": txns}, rej_a + rej_t)
        print("run", attempt, run.status, "processed:", run.records_processed)
    print("accounts:", session.scalar(select(func.count()).select_from(FinancialAccount)))
    print("transactions:", session.scalar(select(func.count()).select_from(Transaction)))
    print("sync runs:", session.scalar(select(func.count()).select_from(DataSyncRun)))