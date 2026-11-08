from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.observability import application_logger

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    response_model=dict[str, str],
    responses={503: {"description": "Database unavailable"}},
)
async def readiness(
    db: AsyncSession = Depends(get_db),
) -> dict[str, str] | JSONResponse:
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        application_logger.error(
            "database readiness check failed",
            extra={
                "event_name": "database_readiness_failed",
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready"},
        )
    return {"status": "ready"}
