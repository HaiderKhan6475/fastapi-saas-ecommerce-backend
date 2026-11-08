import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import models, schemas
from app.config import settings
from app.currencies import normalize_currency_code

logger = logging.getLogger(__name__)
PRICE_SCALE = Decimal(10_000)
MAX_AMOUNT_UNITS = models.MAX_PRODUCT_PRICE_UNITS
MAX_SQLITE_INTEGER = models.MAX_SQLITE_INTEGER


class OrderAuthenticationError(Exception):
    pass


class OrderAuthorizationError(Exception):
    pass


class OrderCartEmptyError(Exception):
    pass


class OrderProductUnavailableError(Exception):
    pass


class OrderStockUnavailableError(Exception):
    pass


class OrderCurrencyError(Exception):
    pass


class OrderAmountError(Exception):
    pass


class OrderIdempotencyConflictError(Exception):
    pass


class OrderNotFoundError(Exception):
    pass


class OrderCancellationConflictError(Exception):
    pass


class OrderStockRestorationError(Exception):
    pass


class OrderFulfillmentConflictError(Exception):
    pass


def _session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = db.bind
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("Order operations require an async SQLite engine")
    return async_sessionmaker(
        bind=bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@asynccontextmanager
async def _write_transaction(db: AsyncSession) -> AsyncIterator[None]:
    try:
        await db.execute(text("BEGIN IMMEDIATE"))
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def _validate_customer(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
) -> None:
    customer = await db.get(models.User, customer_id)
    if customer is None or not customer.is_active:
        raise OrderAuthenticationError
    if customer.role != "Customer" or customer.tenant_id != tenant_id:
        raise OrderAuthorizationError


def _request_fingerprint(items: list[models.CartItem]) -> str:
    canonical_items = [
        {"product_id": item.product_id, "quantity": item.quantity}
        for item in sorted(items, key=lambda cart_item: cart_item.product_id)
    ]
    canonical_request = json.dumps(
        canonical_items,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_request).hexdigest()


def _price_to_units(price: Decimal) -> int:
    if not price.is_finite() or price <= 0:
        raise OrderProductUnavailableError
    scaled_price = price * PRICE_SCALE
    integral_price = scaled_price.to_integral_value()
    if scaled_price != integral_price:
        raise OrderProductUnavailableError
    units = int(integral_price)
    if not 1 <= units <= MAX_AMOUNT_UNITS:
        raise OrderProductUnavailableError
    return units


def _amount_from_units(units: int) -> Decimal:
    return Decimal(units).scaleb(-4)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _order_summary(order: models.Order) -> schemas.OrderSummary:
    return schemas.OrderSummary(
        id=order.id,
        status=order.status,
        currency=order.currency,
        subtotal=_amount_from_units(order.subtotal_units),
        total=_amount_from_units(order.total_units),
        created_at=_utc_datetime(order.created_at),
        expires_at=_utc_datetime(order.expires_at),
    )


async def _order_response(
    db: AsyncSession,
    order: models.Order,
) -> schemas.OrderResponse:
    items = await db.scalars(
        select(models.OrderItem)
        .where(
            models.OrderItem.tenant_id == order.tenant_id,
            models.OrderItem.order_id == order.id,
        )
        .order_by(models.OrderItem.line_number)
    )
    return schemas.OrderResponse(
        **_order_summary(order).model_dump(),
        items=[
            schemas.OrderItemResponse(
                line_number=item.line_number,
                product_id=item.source_product_id,
                product_name=item.product_name,
                unit_price=_amount_from_units(item.unit_price_units),
                currency=item.currency,
                quantity=item.quantity,
                line_total=_amount_from_units(item.line_total_units),
            )
            for item in items
        ],
    )


async def _cart_for_customer(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
) -> models.Cart | None:
    return await db.scalar(
        select(models.Cart).where(
            models.Cart.tenant_id == tenant_id,
            models.Cart.customer_id == customer_id,
        )
    )


async def _cart_items(
    db: AsyncSession,
    cart: models.Cart,
) -> list[models.CartItem]:
    result = await db.scalars(
        select(models.CartItem)
        .where(
            models.CartItem.tenant_id == cart.tenant_id,
            models.CartItem.cart_id == cart.id,
        )
        .order_by(models.CartItem.product_id)
    )
    return list(result)


async def _get_existing_order(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
    idempotency_key: str,
) -> models.Order | None:
    return await db.scalar(
        select(models.Order).where(
            models.Order.tenant_id == tenant_id,
            models.Order.customer_id == customer_id,
            models.Order.idempotency_key == idempotency_key,
        )
    )


async def create_order(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
    idempotency_key: str,
) -> tuple[schemas.OrderResponse, bool]:
    normalized_key = idempotency_key.strip()
    if not normalized_key or len(normalized_key) > 255:
        raise OrderIdempotencyConflictError

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            await _validate_customer(session, customer_id, tenant_id)
            existing_order = await _get_existing_order(
                session,
                customer_id,
                tenant_id,
                normalized_key,
            )
            if existing_order is not None:
                cart = await _cart_for_customer(session, customer_id, tenant_id)
                cart_items = await _cart_items(session, cart) if cart else []
                if cart_items and (
                    _request_fingerprint(cart_items)
                    != existing_order.request_fingerprint
                ):
                    raise OrderIdempotencyConflictError
                return await _order_response(session, existing_order), False

            cart = await _cart_for_customer(session, customer_id, tenant_id)
            if cart is None:
                raise OrderCartEmptyError
            cart_items = await _cart_items(session, cart)
            if not cart_items:
                raise OrderCartEmptyError

            cart_products_result = await session.execute(
                select(models.CartItem, models.Product)
                .join(
                    models.Product,
                    (models.Product.id == models.CartItem.product_id)
                    & (models.Product.tenant_id == models.CartItem.tenant_id),
                )
                .where(
                    models.CartItem.tenant_id == tenant_id,
                    models.CartItem.cart_id == cart.id,
                    models.Product.tenant_id == tenant_id,
                )
                .order_by(models.CartItem.product_id)
            )
            cart_products = cart_products_result.all()
            if len(cart_products) != len(cart_items):
                raise OrderProductUnavailableError

            normalized_currencies: set[str] = set()
            prepared_lines: list[tuple[models.CartItem, models.Product, int, int]] = []
            subtotal_units = 0
            for cart_item, product in cart_products:
                if not 1 <= cart_item.quantity <= models.MAX_CART_QUANTITY:
                    raise OrderCartEmptyError
                if not product.is_available:
                    raise OrderProductUnavailableError
                try:
                    currency = normalize_currency_code(product.currency)
                except ValueError as exc:
                    raise OrderCurrencyError from exc
                normalized_currencies.add(currency)
                if product.stock < cart_item.quantity:
                    raise OrderStockUnavailableError

                unit_price_units = _price_to_units(product.price)
                line_total_units = unit_price_units * cart_item.quantity
                if line_total_units > MAX_SQLITE_INTEGER:
                    raise OrderAmountError
                subtotal_units += line_total_units
                if subtotal_units > MAX_AMOUNT_UNITS:
                    raise OrderAmountError
                prepared_lines.append(
                    (
                        cart_item,
                        product,
                        unit_price_units,
                        line_total_units,
                    )
                )

            if len(normalized_currencies) != 1:
                raise OrderCurrencyError
            currency = next(iter(normalized_currencies))
            fingerprint = _request_fingerprint(cart_items)
            now = datetime.now(UTC)
            order = models.Order(
                tenant_id=tenant_id,
                customer_id=customer_id,
                idempotency_key=normalized_key,
                request_fingerprint=fingerprint,
                status="pending",
                currency=currency,
                subtotal_units=subtotal_units,
                total_units=subtotal_units,
                expires_at=now + timedelta(minutes=settings.ORDER_PENDING_TTL_MINUTES),
            )
            session.add(order)
            await session.flush()

            for line_number, (
                cart_item,
                product,
                unit_price_units,
                line_total_units,
            ) in enumerate(prepared_lines, start=1):
                updated_product_id = await session.scalar(
                    update(models.Product)
                    .where(
                        models.Product.id == product.id,
                        models.Product.tenant_id == tenant_id,
                        models.Product.is_available.is_(True),
                        models.Product.stock >= cart_item.quantity,
                    )
                    .values(
                        stock=models.Product.stock - cart_item.quantity,
                        version=models.Product.version + 1,
                    )
                    .returning(models.Product.id)
                    .execution_options(synchronize_session=False)
                )
                if updated_product_id is None:
                    raise OrderStockUnavailableError
                session.add(
                    models.OrderItem(
                        tenant_id=tenant_id,
                        order_id=order.id,
                        line_number=line_number,
                        source_tenant_id=tenant_id,
                        source_product_id=product.id,
                        product_name=product.name,
                        unit_price_units=unit_price_units,
                        currency=currency,
                        quantity=cart_item.quantity,
                        line_total_units=line_total_units,
                    )
                )

            await session.flush()
            await session.execute(
                delete(models.CartItem).where(
                    models.CartItem.tenant_id == tenant_id,
                    models.CartItem.cart_id == cart.id,
                )
            )
            cart.updated_at = now
            await session.flush()
            return await _order_response(session, order), True


async def get_order(
    db: AsyncSession,
    order_id: int,
    customer_id: int,
    tenant_id: str,
) -> schemas.OrderResponse:
    order = await db.scalar(
        select(models.Order).where(
            models.Order.id == order_id,
            models.Order.tenant_id == tenant_id,
            models.Order.customer_id == customer_id,
        )
    )
    if order is None:
        raise OrderNotFoundError
    return await _order_response(db, order)


async def get_order_history(
    db: AsyncSession,
    customer_id: int,
    tenant_id: str,
    *,
    skip: int,
    limit: int,
) -> list[schemas.OrderSummary]:
    orders = await db.scalars(
        select(models.Order)
        .where(
            models.Order.tenant_id == tenant_id,
            models.Order.customer_id == customer_id,
        )
        .order_by(models.Order.created_at.desc(), models.Order.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return [_order_summary(order) for order in orders]


async def get_tenant_order(
    db: AsyncSession,
    order_id: int,
    tenant_id: str,
) -> schemas.OrderResponse:
    order = await db.scalar(
        select(models.Order).where(
            models.Order.id == order_id,
            models.Order.tenant_id == tenant_id,
        )
    )
    if order is None:
        raise OrderNotFoundError
    return await _order_response(db, order)


async def get_tenant_order_history(
    db: AsyncSession,
    tenant_id: str,
    *,
    skip: int,
    limit: int,
) -> list[schemas.OrderSummary]:
    orders = await db.scalars(
        select(models.Order)
        .where(models.Order.tenant_id == tenant_id)
        .order_by(models.Order.created_at.desc(), models.Order.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return [_order_summary(order) for order in orders]


async def transition_order_fulfillment(
    db: AsyncSession,
    order_id: int,
    tenant_id: str,
    fulfillment_status: schemas.OrderFulfillmentStatus,
) -> schemas.OrderResponse:
    transitions = {
        "pending": "confirmed",
        "confirmed": "processing",
        "processing": "completed",
    }
    now = datetime.now(UTC)

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            order = await session.scalar(
                select(models.Order).where(
                    models.Order.id == order_id,
                    models.Order.tenant_id == tenant_id,
                )
            )
            if order is None:
                raise OrderNotFoundError

            if order.status == fulfillment_status:
                return await _order_response(session, order)
            if transitions.get(order.status) != fulfillment_status:
                raise OrderFulfillmentConflictError

            successful_payment_id = await session.scalar(
                select(models.Payment.id)
                .where(
                    models.Payment.tenant_id == tenant_id,
                    models.Payment.order_id == order.id,
                    models.Payment.status == "succeeded",
                    models.Payment.reconciliation_required.is_(False),
                    models.Payment.updated_at < order.expires_at,
                )
                .limit(1)
            )
            if successful_payment_id is None:
                raise OrderFulfillmentConflictError

            updated_order_id = await session.scalar(
                update(models.Order)
                .where(
                    models.Order.id == order.id,
                    models.Order.tenant_id == tenant_id,
                    models.Order.status == order.status,
                )
                .values(status=fulfillment_status, updated_at=now)
                .returning(models.Order.id)
                .execution_options(synchronize_session=False)
            )
            if updated_order_id is None:
                raise OrderFulfillmentConflictError

            await session.refresh(order)
            return await _order_response(session, order)


async def _restore_order_stock(
    db: AsyncSession,
    tenant_id: str,
    order_id: int,
) -> None:
    items = await db.scalars(
        select(models.OrderItem)
        .where(
            models.OrderItem.tenant_id == tenant_id,
            models.OrderItem.order_id == order_id,
        )
        .order_by(models.OrderItem.line_number)
    )
    for item in items:
        if item.source_product_id is None and item.source_tenant_id is None:
            continue
        if (
            item.source_product_id is None
            or item.source_tenant_id != tenant_id
            or item.quantity < 1
        ):
            raise OrderStockRestorationError

        try:
            restored_product_id = await db.scalar(
                update(models.Product)
                .where(
                    models.Product.tenant_id == tenant_id,
                    models.Product.id == item.source_product_id,
                    models.Product.stock <= MAX_SQLITE_INTEGER - item.quantity,
                    models.Product.version < MAX_SQLITE_INTEGER,
                )
                .values(
                    stock=models.Product.stock + item.quantity,
                    version=models.Product.version + 1,
                )
                .returning(models.Product.id)
                .execution_options(synchronize_session=False)
            )
        except IntegrityError as exc:
            raise OrderStockRestorationError from exc

        if restored_product_id is None:
            raise OrderStockRestorationError


async def _cancel_pending_order(
    db: AsyncSession,
    order: models.Order,
    *,
    now: datetime,
    reason: str,
    require_expired: bool,
) -> bool:
    successful_payment_conditions = [
        models.Payment.tenant_id == order.tenant_id,
        models.Payment.order_id == order.id,
        models.Payment.status == "succeeded",
    ]
    if reason == "expiration":
        successful_payment_conditions.append(
            models.Payment.updated_at < order.expires_at
        )
    successful_payment = await db.scalar(
        select(models.Payment.id).where(*successful_payment_conditions).limit(1)
    )
    if successful_payment is not None:
        return False

    conditions = [
        models.Order.id == order.id,
        models.Order.tenant_id == order.tenant_id,
        models.Order.status == "pending",
    ]
    if require_expired:
        conditions.append(models.Order.expires_at <= now)

    cancelled_id = await db.scalar(
        update(models.Order)
        .where(*conditions)
        .values(
            status="cancelled",
            cancelled_at=now,
            cancellation_reason=reason,
            updated_at=now,
        )
        .returning(models.Order.id)
        .execution_options(synchronize_session=False)
    )
    if cancelled_id is None:
        return False

    await _restore_order_stock(db, order.tenant_id, order.id)
    await db.execute(
        update(models.Payment)
        .where(
            models.Payment.tenant_id == order.tenant_id,
            models.Payment.order_id == order.id,
            models.Payment.status.in_(("initiated", "pending")),
        )
        .values(status="expired", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(models.Payment)
        .where(
            models.Payment.tenant_id == order.tenant_id,
            models.Payment.order_id == order.id,
            models.Payment.status == "succeeded",
        )
        .values(reconciliation_required=True, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return True


async def expire_pending_orders(
    db: AsyncSession,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> int:
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")

    cutoff = _utc_datetime(now) if now is not None else datetime.now(UTC)
    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            order_ids = await session.scalars(
                select(models.Order.id)
                .where(
                    models.Order.status == "pending",
                    models.Order.expires_at <= cutoff,
                    ~select(models.Payment.id)
                    .where(
                        models.Payment.tenant_id == models.Order.tenant_id,
                        models.Payment.order_id == models.Order.id,
                        models.Payment.status == "succeeded",
                        models.Payment.updated_at < models.Order.expires_at,
                    )
                    .exists(),
                )
                .order_by(models.Order.expires_at, models.Order.id)
                .limit(batch_size)
            )
            expired_count = 0
            for order_id in order_ids:
                order = await session.scalar(
                    select(models.Order).where(
                        models.Order.id == order_id,
                        models.Order.status == "pending",
                        models.Order.expires_at <= cutoff,
                    )
                )
                if order is not None and await _cancel_pending_order(
                    session,
                    order,
                    now=cutoff,
                    reason="expiration",
                    require_expired=True,
                ):
                    expired_count += 1
            return expired_count


async def cancel_order(
    db: AsyncSession,
    order_id: int,
    customer_id: int,
    tenant_id: str,
) -> tuple[schemas.OrderResponse, bool]:
    now = datetime.now(UTC)
    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            await _validate_customer(session, customer_id, tenant_id)
            order = await session.scalar(
                select(models.Order).where(
                    models.Order.id == order_id,
                    models.Order.tenant_id == tenant_id,
                    models.Order.customer_id == customer_id,
                )
            )
            if order is None:
                raise OrderNotFoundError

            if order.status == "cancelled":
                return await _order_response(session, order), False
            if order.status != "pending":
                raise OrderCancellationConflictError

            expired = _utc_datetime(order.expires_at) <= now
            cancelled = await _cancel_pending_order(
                session,
                order,
                now=now,
                reason="expiration" if expired else "customer",
                require_expired=expired,
            )
            if not cancelled:
                raise OrderCancellationConflictError
            await session.refresh(order)
            return await _order_response(session, order), expired


async def run_expiration_worker(
    session_factory: async_sessionmaker[AsyncSession],
    stop_event: asyncio.Event,
    *,
    poll_interval_seconds: int,
    batch_size: int,
) -> None:
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                await expire_pending_orders(session, batch_size=batch_size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "order expiration batch failed; worker will retry (error_type=%s)",
                type(exc).__name__,
            )

        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=poll_interval_seconds,
            )
        except TimeoutError:
            continue
