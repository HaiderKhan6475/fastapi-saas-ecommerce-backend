from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, schemas
from app.database import get_db
from app.services import payment_service

router = APIRouter(prefix="/payments", tags=["payments"])


def _identity(user: models.User) -> tuple[int, str]:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a tenant",
        )
    return user.id, user.tenant_id


def _raise_payment_error(exc: Exception) -> NoReturn:
    if isinstance(exc, payment_service.PaymentAuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if isinstance(exc, payment_service.PaymentAuthorizationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        ) from exc
    if isinstance(exc, payment_service.PaymentNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        ) from exc
    raise exc


@router.get("/", response_model=list[schemas.PaymentSummary])
async def get_payment_history(
    skip: int = Query(default=0, ge=0, le=1_000_000),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> list[schemas.PaymentSummary]:
    customer_id, tenant_id = _identity(current_user)
    return await payment_service.get_payment_history(
        db,
        customer_id=customer_id,
        tenant_id=tenant_id,
        skip=skip,
        limit=limit,
    )


@router.get("/{payment_id}", response_model=schemas.PaymentSummary)
async def get_payment(
    payment_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.PaymentSummary:
    customer_id, tenant_id = _identity(current_user)
    try:
        return await payment_service.get_payment(
            db,
            payment_id=payment_id,
            customer_id=customer_id,
            tenant_id=tenant_id,
        )
    except payment_service.PaymentNotFoundError as exc:
        _raise_payment_error(exc)
