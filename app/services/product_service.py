from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, schemas


class ProductConflictError(Exception):
    pass


async def create_product(
    db: AsyncSession,
    product_data: schemas.ProductCreate,
    tenant_id: str,
) -> models.Product:
    tenant_result = await db.execute(
        select(models.Tenant.default_currency).where(models.Tenant.id == tenant_id)
    )
    default_currency = tenant_result.scalar_one_or_none()
    if default_currency is None:
        raise LookupError("Tenant is no longer available")

    product_values = product_data.model_dump(exclude={"currency"})
    product = models.Product(
        **product_values,
        currency=product_data.currency or default_currency,
        tenant_id=tenant_id,
    )
    db.add(product)
    await db.commit()
    return product


async def get_tenant_products(
    db: AsyncSession,
    tenant_id: str,
    skip: int = 0,
    limit: int = 10,
    search: str | None = None,
    currency: str | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
) -> list[models.Product]:
    query = select(models.Product).where(models.Product.tenant_id == tenant_id)
    if search is not None:
        query = query.where(
            func.lower(models.Product.name).contains(search.lower(), autoescape=True)
            | func.lower(models.Product.description).contains(
                search.lower(),
                autoescape=True,
            )
        )
    if currency is not None:
        query = query.where(models.Product.currency == currency)
    if min_price is not None:
        query = query.where(models.Product.price >= min_price)
    if max_price is not None:
        query = query.where(models.Product.price <= max_price)

    result = await db.execute(
        query.order_by(models.Product.id).offset(skip).limit(limit)
    )
    return list(result.scalars().all())


async def get_tenant_product(
    db: AsyncSession,
    product_id: int,
    tenant_id: str,
) -> models.Product | None:
    result = await db.execute(
        select(models.Product).where(
            models.Product.id == product_id,
            models.Product.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def update_product(
    db: AsyncSession,
    product_id: int,
    tenant_id: str,
    product_data: schemas.ProductUpdate,
) -> models.Product | None:
    values = product_data.model_dump(
        exclude={"expected_version"},
        exclude_unset=True,
    )
    statement = (
        update(models.Product)
        .where(
            models.Product.id == product_id,
            models.Product.tenant_id == tenant_id,
            models.Product.version == product_data.expected_version,
        )
        .values(
            **values,
            version=models.Product.version + 1,
        )
        .returning(models.Product.id)
    )
    result = await db.execute(statement)
    if result.scalar_one_or_none() is None:
        await db.rollback()
        current = await get_tenant_product(db, product_id, tenant_id)
        if current is None:
            return None
        raise ProductConflictError

    await db.commit()
    return await get_tenant_product(db, product_id, tenant_id)


async def delete_product(
    db: AsyncSession,
    product_id: int,
    tenant_id: str,
) -> bool:
    result = await db.execute(
        select(models.Product).where(
            models.Product.id == product_id,
            models.Product.tenant_id == tenant_id,
        )
    )
    product = result.scalar_one_or_none()
    if product is None:
        return False

    await db.delete(product)
    await db.commit()
    return True
