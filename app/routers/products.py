from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, schemas
from app.currencies import normalize_currency_code
from app.database import get_db
from app.services import product_service

router = APIRouter(prefix="/products", tags=["products"])


def _require_tenant_id(user: models.User) -> str:
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a tenant",
        )
    return user.tenant_id


@router.post(
    "/",
    response_model=schemas.ProductResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_product(
    product_data: schemas.ProductCreate,
    current_user: models.User = Depends(deps.RequireRole(["TenantAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.ProductResponse:
    try:
        product = await product_service.create_product(
            db,
            product_data,
            _require_tenant_id(current_user),
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        ) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product could not be created",
        ) from exc
    return schemas.ProductResponse.model_validate(product)


@router.get("/", response_model=list[schemas.ProductResponse])
async def get_products(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    currency: str | None = Query(default=None, min_length=3, max_length=3),
    min_price: Decimal | None = Query(
        default=None, ge=0, max_digits=15, decimal_places=4
    ),
    max_price: Decimal | None = Query(
        default=None, ge=0, max_digits=15, decimal_places=4
    ),
    current_user: models.User = Depends(
        deps.RequireRole(["TenantAdmin", "Customer", "SuperAdmin"])
    ),
    db: AsyncSession = Depends(get_db),
) -> list[schemas.ProductResponse]:
    if skip > 1_000_000:
        raise HTTPException(
            status_code=422,
            detail="skip must be less than or equal to 1000000",
        )
    if search is not None:
        search = search.strip()
        if not search:
            raise HTTPException(
                status_code=422,
                detail="search must not be blank",
            )
    try:
        normalized_currency = (
            normalize_currency_code(currency) if currency is not None else None
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="currency must be a supported ISO 4217 code",
        ) from exc
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=422,
            detail="min_price must not exceed max_price",
        )

    products = await product_service.get_tenant_products(
        db,
        _require_tenant_id(current_user),
        skip=skip,
        limit=limit,
        search=search,
        currency=normalized_currency,
        min_price=min_price,
        max_price=max_price,
    )
    return [schemas.ProductResponse.model_validate(product) for product in products]


@router.get("/{product_id}", response_model=schemas.ProductResponse)
async def get_product(
    product_id: int = Path(gt=0),
    current_user: models.User = Depends(
        deps.RequireRole(["TenantAdmin", "Customer", "SuperAdmin"])
    ),
    db: AsyncSession = Depends(get_db),
) -> schemas.ProductResponse:
    product = await product_service.get_tenant_product(
        db,
        product_id,
        _require_tenant_id(current_user),
    )
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )
    return schemas.ProductResponse.model_validate(product)


@router.patch("/{product_id}", response_model=schemas.ProductResponse)
async def update_product(
    product_data: schemas.ProductUpdate,
    product_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["TenantAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.ProductResponse:
    try:
        product = await product_service.update_product(
            db,
            product_id,
            _require_tenant_id(current_user),
            product_data,
        )
    except product_service.ProductConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product was changed by another request; reload and retry",
        ) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product update conflicts with stored data",
        ) from exc
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )
    return schemas.ProductResponse.model_validate(product)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_product(
    product_id: int = Path(gt=0),
    current_user: models.User = Depends(deps.RequireRole(["TenantAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> Response:
    was_deleted = await product_service.delete_product(
        db,
        product_id,
        _require_tenant_id(current_user),
    )
    if not was_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
