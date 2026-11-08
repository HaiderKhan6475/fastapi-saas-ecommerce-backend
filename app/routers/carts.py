from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, schemas
from app.database import get_db
from app.services import cart_service

router = APIRouter(prefix="/cart", tags=["cart"])


def _identity(user: models.User) -> tuple[int, str]:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a tenant",
        )
    return user.id, user.tenant_id


def _raise_cart_error(exc: Exception) -> NoReturn:
    if isinstance(exc, cart_service.CartAuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if isinstance(exc, cart_service.CartAuthorizationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        ) from exc
    if isinstance(exc, cart_service.CartProductNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        ) from exc
    if isinstance(exc, cart_service.CartProductUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product is not available for cart operations",
        ) from exc
    if isinstance(exc, cart_service.CartItemAlreadyExistsError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product already has a cart line; update its quantity instead",
        ) from exc
    if isinstance(exc, cart_service.CartItemNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cart item not found",
        ) from exc
    if isinstance(exc, cart_service.CartMixedCurrencyError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cart items must use a single currency",
        ) from exc
    if isinstance(exc, cart_service.CartQuantityError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Quantity must be between 1 and 1000",
        ) from exc
    if isinstance(exc, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cart could not be updated",
        ) from exc
    raise exc


@router.get("/", response_model=schemas.CartResponse)
async def get_cart(
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.CartResponse:
    customer_id, tenant_id = _identity(current_user)
    try:
        return await cart_service.get_cart(db, customer_id, tenant_id)
    except (
        cart_service.CartAuthenticationError,
        cart_service.CartAuthorizationError,
        cart_service.CartProductUnavailableError,
        cart_service.CartMixedCurrencyError,
    ) as exc:
        _raise_cart_error(exc)


@router.post(
    "/items",
    response_model=schemas.CartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_cart_item(
    item_data: schemas.CartItemAdd,
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.CartResponse:
    customer_id, tenant_id = _identity(current_user)
    try:
        return await cart_service.add_item(
            db,
            customer_id,
            tenant_id,
            item_data.product_id,
            item_data.quantity,
        )
    except (
        cart_service.CartAuthenticationError,
        cart_service.CartAuthorizationError,
        cart_service.CartProductNotFoundError,
        cart_service.CartProductUnavailableError,
        cart_service.CartMixedCurrencyError,
        cart_service.CartItemAlreadyExistsError,
        cart_service.CartQuantityError,
        IntegrityError,
    ) as exc:
        _raise_cart_error(exc)


@router.put("/items/{product_id}", response_model=schemas.CartResponse)
async def update_cart_item(
    item_data: schemas.CartItemUpdate,
    product_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.CartResponse:
    customer_id, tenant_id = _identity(current_user)
    try:
        return await cart_service.update_item(
            db,
            customer_id,
            tenant_id,
            product_id,
            item_data.quantity,
        )
    except (
        cart_service.CartAuthenticationError,
        cart_service.CartAuthorizationError,
        cart_service.CartProductNotFoundError,
        cart_service.CartProductUnavailableError,
        cart_service.CartMixedCurrencyError,
        cart_service.CartItemNotFoundError,
        cart_service.CartQuantityError,
        IntegrityError,
    ) as exc:
        _raise_cart_error(exc)


@router.delete(
    "/items/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def remove_cart_item(
    product_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["Customer"])),
    db: AsyncSession = Depends(get_db),
) -> Response:
    customer_id, tenant_id = _identity(current_user)
    try:
        await cart_service.remove_item(db, customer_id, tenant_id, product_id)
    except (
        cart_service.CartAuthenticationError,
        cart_service.CartAuthorizationError,
        cart_service.CartItemNotFoundError,
        IntegrityError,
    ) as exc:
        _raise_cart_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
