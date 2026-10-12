from fastapi import APIRouter

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("/")
def connections_status():
    return {"message": "connections route working"}