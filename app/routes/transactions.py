from fastapi import APIRouter

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("/")
def transactions_status():
    return {"message": "transactions route working"}