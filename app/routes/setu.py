"""Sandbox-only Setu integration readiness endpoints."""

from fastapi import APIRouter, HTTPException

from app.integrations.setu.config import SetuSettings

router = APIRouter(prefix="/api/setu", tags=["Setu sandbox"])


@router.get("/health")
def setu_health() -> dict[str, str | bool]:
    settings = SetuSettings()
    if settings.environment != "sandbox":
        raise HTTPException(
            status_code=403,
            detail="Setu endpoints are available only in the sandbox environment.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=503,
            detail="Setu sandbox configuration is incomplete.",
        )

    return {
        "status": "ready",
        "environment": "sandbox",
        "configured": True,
        "connectivity": "not_tested",
    }
