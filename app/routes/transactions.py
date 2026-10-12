from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import Transaction

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("/")
def list_transactions(db: Session = Depends(get_db)):
    transactions = db.scalars(
        select(Transaction).order_by(Transaction.transaction_at.desc()).limit(100)
    ).all()
    return [
        {
            "id": str(t.id),
            "account_id": str(t.account_id),
            "transaction_type": t.transaction_type,
            "amount": str(t.amount),
            "currency": t.currency,
            "description": t.description,
            "category": t.category,
            "transaction_status": t.transaction_status,
            "transaction_at": t.transaction_at.isoformat(),
        }
        for t in transactions
    ]