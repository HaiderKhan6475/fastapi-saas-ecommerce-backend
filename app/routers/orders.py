from typing import NoReturn

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, schemas
from app.database import get_db
from app.services import order_service

router = APIRouter(prefix="/orders", tags=["orders"])


def _tenant_id(user: models.User) -> str:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a tenant",
        )
    return user.tenant_id


def _identity(user: models.User) -> tuple[int, str]:
    return user.id, _tenant_id(user)


def _raise_order_error(exc: Exception) -> NoReturn:
    if isinstance(exc, order_service.OrderAuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if isinstance(exc, order_service.OrderAuthorizationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        ) from exc
    if isinstance(exc, order_service.OrderNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        ) from exc
    if isinstance(exc, order_service.OrderFulfillmentConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Order fulfillment requires a successful payment and the next "
                "valid state"
            ),
        ) from exc
    if isinstance(
        exc,
        (
            order_service.OrderCancellationConflictError,
            order_service.OrderStockRestorationError,
        ),
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order cannot be cancelled in its current state",
        ) from exc
    if isinstance(exc, order_service.OrderIdempotencyConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key conflicts with a previous checkout",
        ) from exc
    if isinstance(exc, order_service.OrderCartEmptyError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cart is empty or contains an invalid quantity",
        ) from exc
    if isinstance(exc, order_service.OrderProductUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cart product is unavailable",
        ) from exc
    if isinstance(exc, order_service.OrderStockUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Insufficient stock for one or more cart items",
        ) from exc
    if isinstance(exc, order_service.OrderCurrencyError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cart products must use one supported currency",
        ) from exc
    if isinstance(exc, order_service.OrderAmountError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order total exceeds the supported amount",
        ) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order could not be created",
        ) from exc
    raise exc


@router.post(
    "/",
    response_model=schemas.OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order(
    response: Response,
    idempotency_key: str = Header(
        min_length=1,
        max_length=255,
        alias="Idempotency-Key",
    ),
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.OrderResponse:
    customer_id, tenant_id = _identity(current_user)
    try:
        order, created = await order_service.create_order(
            db,
            customer_id,
            tenant_id,
            idempotency_key,
        )
    except (
        order_service.OrderAuthenticationError,
        order_service.OrderAuthorizationError,
        order_service.OrderCartEmptyError,
        order_service.OrderProductUnavailableError,
        order_service.OrderStockUnavailableError,
        order_service.OrderCurrencyError,
        order_service.OrderAmountError,
        order_service.OrderIdempotencyConflictError,
        IntegrityError,
    ) as exc:
        _raise_order_error(exc)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return order


@router.get("/", response_model=list[schemas.OrderSummary])
async def get_order_history(
    skip: int = Query(default=0, ge=0, le=1_000_000),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: models.User = Depends(deps.RequireRole(["Customer", "TenantAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> list[schemas.OrderSummary]:
    if current_user.role == "TenantAdmin":
        return await order_service.get_tenant_order_history(
            db,
            _tenant_id(current_user),
            skip=skip,
            limit=limit,
        )
    customer_id, tenant_id = _identity(current_user)
    return await order_service.get_order_history(
        db,
        customer_id,
        tenant_id,
        skip=skip,
        limit=limit,
    )


@router.post("/{order_id}/cancel", response_model=schemas.OrderResponse)
async def cancel_order(
    order_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.OrderResponse:
    customer_id, tenant_id = _identity(current_user)
    try:
        order, expired = await order_service.cancel_order(
            db,
            order_id,
            customer_id,
            tenant_id,
        )
    except (
        order_service.OrderAuthenticationError,
        order_service.OrderAuthorizationError,
        order_service.OrderNotFoundError,
        order_service.OrderCancellationConflictError,
        order_service.OrderStockRestorationError,
        IntegrityError,
    ) as exc:
        _raise_order_error(exc)
    if expired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order has expired and was cancelled",
        )
    return order


@router.get("/{order_id}", response_model=schemas.OrderResponse)
async def get_order(
    order_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["Customer", "TenantAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.OrderResponse:
    if current_user.role == "TenantAdmin":
        try:
            return await order_service.get_tenant_order(
                db,
                order_id,
                _tenant_id(current_user),
            )
        except order_service.OrderNotFoundError as exc:
            _raise_order_error(exc)

    customer_id, tenant_id = _identity(current_user)
    try:
        return await order_service.get_order(
            db,
            order_id,
            customer_id,
            tenant_id,
        )
    except order_service.OrderNotFoundError as exc:
        _raise_order_error(exc)


@router.patch(
    "/{order_id}/fulfillment",
    response_model=schemas.OrderResponse,
)
async def update_order_fulfillment(
    fulfillment_update: schemas.OrderFulfillmentUpdate,
    order_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["TenantAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.OrderResponse:
    try:
        return await order_service.transition_order_fulfillment(
            db,
            order_id,
            _tenant_id(current_user),
            fulfillment_update.fulfillment_status,
        )
    except (
        order_service.OrderNotFoundError,
        order_service.OrderFulfillmentConflictError,
    ) as exc:
        _raise_order_error(exc)
