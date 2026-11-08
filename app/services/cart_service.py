from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import models, schemas
from app.currencies import normalize_currency_code

MAX_PRODUCT_PRICE = Decimal(models.MAX_PRODUCT_PRICE_UNITS).scaleb(-4)


class CartAuthenticationError(Exception):
    pass


class CartAuthorizationError(Exception):
    pass


class CartProductNotFoundError(Exception):
    pass


class CartProductUnavailableError(Exception):
    pass


class CartItemAlreadyExistsError(Exception):
    pass


class CartItemNotFoundError(Exception):
    pass


class CartQuantityError(Exception):
    pass


class CartMixedCurrencyError(Exception):
    pass


def _session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = db.bind
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("Cart operations require an async SQLite engine")
    return async_sessionmaker(
        bind=bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _validate_customer(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
) -> None:
    customer = await db.get(models.User, customer_id)
    if customer is None or not customer.is_active:
        raise CartAuthenticationError
    if customer.role != "Customer" or customer.tenant_id != tenant_id:
        raise CartAuthorizationError


async def _get_cart(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
) -> models.Cart | None:
    result = await db.execute(
        select(models.Cart).where(
            models.Cart.tenant_id == tenant_id,
            models.Cart.customer_id == customer_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_or_create_cart(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
) -> models.Cart:
    cart = await _get_cart(db, customer_id, tenant_id)
    if cart is None:
        cart = models.Cart(tenant_id=tenant_id, customer_id=customer_id)
        db.add(cart)
        await db.flush()
    return cart


async def _get_product(
    db: AsyncSession,
    product_id: int,
    tenant_id: str,
) -> models.Product:
    result = await db.execute(
        select(models.Product).where(
            models.Product.id == product_id,
            models.Product.tenant_id == tenant_id,
        )
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise CartProductNotFoundError
    return product


async def _cart_currencies(
    db: AsyncSession,
    cart: models.Cart,
    *,
    exclude_product_id: int | None = None,
) -> set[str]:
    statement = (
        select(models.Product.currency)
        .join(
            models.CartItem,
            (models.CartItem.product_id == models.Product.id)
            & (models.CartItem.tenant_id == models.Product.tenant_id),
        )
        .where(
            models.CartItem.tenant_id == cart.tenant_id,
            models.CartItem.cart_id == cart.id,
            models.Product.tenant_id == cart.tenant_id,
        )
    )
    if exclude_product_id is not None:
        statement = statement.where(models.CartItem.product_id != exclude_product_id)
    currencies = (await db.scalars(statement)).all()
    try:
        return {normalize_currency_code(currency) for currency in currencies}
    except ValueError as exc:
        raise CartProductUnavailableError from exc


@asynccontextmanager
async def _write_transaction(db: AsyncSession) -> AsyncIterator[None]:
    try:
        await db.execute(text("BEGIN IMMEDIATE"))
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


def _validate_product(product: models.Product, *, require_available: bool) -> str:
    if require_available and not product.is_available:
        raise CartProductUnavailableError

    price = product.price
    if (
        not price.is_finite()
        or price <= 0
        or price > MAX_PRODUCT_PRICE
        or price != price.quantize(Decimal("0.0001"))
    ):
        raise CartProductUnavailableError

    try:
        return normalize_currency_code(product.currency)
    except ValueError as exc:
        raise CartProductUnavailableError from exc


async def _build_cart_response(
    db: AsyncSession,
    cart: models.Cart | None,
) -> schemas.CartResponse:
    if cart is None:
        return schemas.CartResponse(id=None, updated_at=None, items=[])

    result = await db.execute(
        select(models.CartItem, models.Product)
        .join(
            models.Product,
            (models.Product.id == models.CartItem.product_id)
            & (models.Product.tenant_id == models.CartItem.tenant_id),
        )
        .where(
            models.CartItem.tenant_id == cart.tenant_id,
            models.CartItem.cart_id == cart.id,
        )
        .order_by(models.CartItem.id)
    )
    item_rows = result.all()
    currencies = {
        _validate_product(product, require_available=False) for _, product in item_rows
    }
    if len(currencies) > 1:
        raise CartMixedCurrencyError

    items = [
        schemas.CartItemResponse(
            product_id=item.product_id,
            product_name=product.name,
            quantity=item.quantity,
            unit_price=product.price,
            currency=next(iter(currencies)),
            is_available=product.is_available,
            line_total=product.price * item.quantity,
        )
        for item, product in item_rows
    ]
    return schemas.CartResponse(
        id=cart.id,
        updated_at=cart.updated_at,
        items=items,
    )


async def get_cart(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
) -> schemas.CartResponse:
    async with _session_factory(db)() as session:
        await _validate_customer(session, customer_id, tenant_id)
        cart = await _get_cart(session, customer_id, tenant_id)
        return await _build_cart_response(session, cart)


async def add_item(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
    product_id: int,
    quantity: int,
) -> schemas.CartResponse:
    if not 1 <= quantity <= models.MAX_CART_QUANTITY:
        raise CartQuantityError

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            await _validate_customer(session, customer_id, tenant_id)
            cart = await _get_or_create_cart(session, customer_id, tenant_id)
            product = await _get_product(session, product_id, tenant_id)
            currency = _validate_product(product, require_available=True)
            existing_currencies = await _cart_currencies(session, cart)
            if existing_currencies and existing_currencies != {currency}:
                raise CartMixedCurrencyError

            existing_item = await session.scalar(
                select(models.CartItem.id).where(
                    models.CartItem.tenant_id == tenant_id,
                    models.CartItem.cart_id == cart.id,
                    models.CartItem.product_id == product_id,
                )
            )
            if existing_item is not None:
                raise CartItemAlreadyExistsError

            session.add(
                models.CartItem(
                    tenant_id=tenant_id,
                    cart_id=cart.id,
                    product_id=product_id,
                    quantity=quantity,
                )
            )
            cart.updated_at = datetime.now(UTC)
            await session.flush()
            response = await _build_cart_response(session, cart)
        return response


async def update_item(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
    product_id: int,
    quantity: int,
) -> schemas.CartResponse:
    if not 1 <= quantity <= models.MAX_CART_QUANTITY:
        raise CartQuantityError

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            await _validate_customer(session, customer_id, tenant_id)
            cart = await _get_cart(session, customer_id, tenant_id)
            if cart is None:
                raise CartItemNotFoundError

            item = await session.scalar(
                select(models.CartItem).where(
                    models.CartItem.tenant_id == tenant_id,
                    models.CartItem.cart_id == cart.id,
                    models.CartItem.product_id == product_id,
                )
            )
            if item is None:
                raise CartItemNotFoundError

            product = await _get_product(session, product_id, tenant_id)
            currency = _validate_product(product, require_available=True)
            other_currencies = await _cart_currencies(
                session,
                cart,
                exclude_product_id=product_id,
            )
            if other_currencies and other_currencies != {currency}:
                raise CartMixedCurrencyError

            item.quantity = quantity
            cart.updated_at = datetime.now(UTC)
            response = await _build_cart_response(session, cart)
        return response


async def remove_item(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
    product_id: int,
) -> None:
    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            await _validate_customer(session, customer_id, tenant_id)
            cart = await _get_cart(session, customer_id, tenant_id)
            if cart is None:
                raise CartItemNotFoundError

            item = await session.scalar(
                select(models.CartItem).where(
                    models.CartItem.tenant_id == tenant_id,
                    models.CartItem.cart_id == cart.id,
                    models.CartItem.product_id == product_id,
                )
            )
            if item is None:
                raise CartItemNotFoundError

            await session.delete(item)
            cart.updated_at = datetime.now(UTC)
