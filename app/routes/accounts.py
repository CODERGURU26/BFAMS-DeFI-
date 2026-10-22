from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authorization import assert_authorization_database
from app.auth.deps import get_current_user
from app.auth.schemas import UserResponse
from app.dependencies import get_mf_db
from app.models import FinancialAccount, FinancialConnection

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/")
def list_accounts(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_mf_db),
):
    assert_authorization_database(db)
    accounts = db.scalars(
        select(FinancialAccount)
        .join(
            FinancialConnection,
            FinancialConnection.id == FinancialAccount.connection_id,
        )
        .where(FinancialConnection.user_id == current_user.user_id)
    ).all()
    return [
        {
            "id": str(a.id),
            "account_type": a.account_type,
            "account_name": a.account_name,
            "currency": a.currency,
            "status": a.status,
        }
        for a in accounts
    ]