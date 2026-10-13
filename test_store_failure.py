from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.ingestion import validate_accounts
from app.models import DataSyncRun, FinancialConnection, Transaction
from app.schemas import NormalizedTransaction
from app.store import store_sync

engine = get_test_engine()

accounts, _ = validate_accounts(
    [{"external_account_id": "A1", "account_type": "savings", "status": "active"}]
)

# Built without validation, to simulate a bug that lets a bad record through.
bad_txn = NormalizedTransaction.model_construct(
    external_transaction_id="T99",
    transaction_type="refund",
    amount="5.00",
    currency="INR",
    description=None,
    category=None,
    transaction_status="unknown",
    transaction_at="2026-10-01T10:00:00+05:30",
    source_updated_at=None,
)

with Session(engine) as session:
    conn = session.scalars(
        select(FinancialConnection).where(
            FinancialConnection.external_connection_id == "TEST-CONNECTION-1"
        )
    ).one()
    try:
        store_sync(session, conn.id, accounts, {"A1": [bad_txn]}, [])
        print("ERROR: no exception was raised (this is a bug)")
    except Exception as exc:
        print("caught:", type(exc).__name__)

    failed = session.scalars(
        select(DataSyncRun).where(DataSyncRun.status == "failed")
    ).all()
    for f in failed:
        print("failed run:", f.records_processed, f.error_message)
    print("transactions:", session.scalar(select(func.count()).select_from(Transaction)))
    print("sync runs:", session.scalar(select(func.count()).select_from(DataSyncRun)))