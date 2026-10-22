from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authorization import assert_authorization_database
from app.auth.deps import get_current_user
from app.auth.schemas import UserResponse
from app.dependencies import get_mf_db
from app.models import FinancialAccount, FinancialConnection, Transaction

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("/")
def list_transactions(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_mf_db),
):
    assert_authorization_database(db)
    transactions = db.scalars(
        select(Transaction)
        .join(FinancialAccount, FinancialAccount.id == Transaction.account_id)
        .join(
            FinancialConnection,
            FinancialConnection.id == FinancialAccount.connection_id,
        )
        .where(FinancialConnection.user_id == current_user.user_id)
        .order_by(Transaction.transaction_at.desc())
        .limit(100)
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