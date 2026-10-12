from fastapi import APIRouter

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/")
def accounts_status():
    return {"message": "accounts route working"}