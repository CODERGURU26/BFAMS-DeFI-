from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import FinancialConnection

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("/")
def list_connections(db: Session = Depends(get_db)):
    connections = db.scalars(select(FinancialConnection)).all()
    return [
        {
            "id": str(c.id),
            "provider_id": str(c.provider_id),
            "status": c.status,
            "created_at": c.created_at.isoformat(),
        }
        for c in connections
    ]