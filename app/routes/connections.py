from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authorization import assert_authorization_database
from app.auth.deps import get_current_user
from app.auth.schemas import UserResponse
from app.dependencies import get_mf_db
from app.models import FinancialConnection

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("/")
def list_connections(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_mf_db),
):
    assert_authorization_database(db)
    connections = db.scalars(
        select(FinancialConnection).where(
            FinancialConnection.user_id == current_user.user_id
        )
    ).all()
    return [
        {
            "id": str(c.id),
            "provider_id": str(c.provider_id),
            "status": c.status,
            "created_at": c.created_at.isoformat(),
        }
        for c in connections
    ]