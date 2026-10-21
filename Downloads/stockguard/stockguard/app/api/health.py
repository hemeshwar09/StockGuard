from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import Session
from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    """Process is up (used by container health checks; does not touch the DB)."""
    return {"status": "ok"}


@router.get("/health")
async def ready(session: Session) -> JSONResponse:
    """Readiness: verifies the database is reachable."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "database_unavailable"})
    return JSONResponse(
        content={"status": "ok", "inventory_strategy": settings.inventory_strategy}
    )
