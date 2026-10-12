from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import FinancialAccount

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/")
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.scalars(select(FinancialAccount)).all()
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