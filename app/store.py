from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataSyncRun, FinancialAccount, Transaction


def store_sync(
    session: Session,
    connection_id,
    accounts,
    transactions_by_account,
    rejected,
    sync_type="full",
):
    run = DataSyncRun(connection_id=connection_id, sync_type=sync_type, status="running")
    session.add(run)
    session.flush()

    processed = 0
    account_ids = {}

    for acc in accounts:
        existing = session.scalars(
            select(FinancialAccount).where(
                FinancialAccount.connection_id == connection_id,
                FinancialAccount.external_account_id == acc.external_account_id,
            )
        ).first()
        if existing is None:
            existing = FinancialAccount(
                connection_id=connection_id,
                external_account_id=acc.external_account_id,
                account_type=acc.account_type,
                account_name=acc.account_name,
                currency=acc.currency,
                status=acc.status,
                source_updated_at=acc.source_updated_at,
            )
            session.add(existing)
            session.flush()
            processed += 1
        account_ids[acc.external_account_id] = existing.id

    for ext_account_id, txns in transactions_by_account.items():
        account_id = account_ids.get(ext_account_id)
        if account_id is None:
            rejected.append({"index": None, "errors": "transaction for unknown account"})
            continue
        for t in txns:
            exists = session.scalars(
                select(Transaction.id).where(
                    Transaction.account_id == account_id,
                    Transaction.external_transaction_id == t.external_transaction_id,
                )
            ).first()
            if exists is not None:
                continue
            session.add(
                Transaction(
                    account_id=account_id,
                    external_transaction_id=t.external_transaction_id,
                    transaction_type=t.transaction_type,
                    amount=t.amount,
                    currency=t.currency,
                    description=t.description,
                    category=t.category,
                    transaction_status=t.transaction_status,
                    transaction_at=t.transaction_at,
                    source_updated_at=t.source_updated_at,
                )
            )
            processed += 1

    run.records_processed = processed
    run.status = "partial" if rejected else "completed"
    run.completed_at = datetime.now(timezone.utc)
    if rejected:
        run.error_message = f"{len(rejected)} record(s) rejected: " + "; ".join(
            f"index {r['index']}" for r in rejected
        )
    session.commit()
    return run