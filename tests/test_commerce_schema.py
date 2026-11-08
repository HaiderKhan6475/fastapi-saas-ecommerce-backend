import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import models


def _order_values(
    tenant_id: str,
    customer_id: int,
    *,
    idempotency_key: str = "checkout-key",
    request_fingerprint: str = "a" * 64,
    status: str = "pending",
    currency: str = "PKR",
    subtotal_units: int = 10_000,
    total_units: int = 10_000,
    cancelled_at: datetime | None = None,
    cancellation_reason: str | None = None,
) -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "status": status,
        "currency": currency,
        "subtotal_units": subtotal_units,
        "total_units": total_units,
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
        "cancelled_at": cancelled_at,
        "cancellation_reason": cancellation_reason,
    }


def _order_item(
    tenant_id: str,
    order_id: int,
    *,
    source_tenant_id: str | None,
    source_product_id: int | None,
    line_number: int = 1,
    product_name: str = "Snapshot product",
    unit_price_units: int = 10_000,
    currency: str = "PKR",
    quantity: int = 1,
    line_total_units: int = 10_000,
) -> models.OrderItem:
    return models.OrderItem(
        tenant_id=tenant_id,
        order_id=order_id,
        line_number=line_number,
        source_tenant_id=source_tenant_id,
        source_product_id=source_product_id,
        product_name=product_name,
        unit_price_units=unit_price_units,
        currency=currency,
        quantity=quantity,
        line_total_units=line_total_units,
    )


def test_cart_unique_lines_and_quantity_bounds(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
    sample_product: models.Product,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            cart = models.Cart(tenant_id=tenant.id, customer_id=customer.id)
            session.add(cart)
            await session.commit()
            cart_id = cart.id

            session.add(models.Cart(tenant_id=tenant.id, customer_id=customer.id))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            for quantity in (0, 1001):
                session.add(
                    models.CartItem(
                        tenant_id=tenant.id,
                        cart_id=cart_id,
                        product_id=sample_product.id,
                        quantity=quantity,
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()

            item = models.CartItem(
                tenant_id=tenant.id,
                cart_id=cart_id,
                product_id=sample_product.id,
                quantity=1,
            )
            session.add(item)
            await session.commit()

            session.add(
                models.CartItem(
                    tenant_id=tenant.id,
                    cart_id=cart_id,
                    product_id=sample_product.id,
                    quantity=2,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

    asyncio.run(verify())


def test_cart_composite_foreign_keys_prevent_cross_tenant_links(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
    other_tenant: models.Tenant,
    other_tenant_admin: models.User,
    sample_product: models.Product,
    other_tenant_product: models.Product,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            own_cart = models.Cart(tenant_id=tenant.id, customer_id=customer.id)
            foreign_cart = models.Cart(
                tenant_id=other_tenant.id,
                customer_id=other_tenant_admin.id,
            )
            session.add_all([own_cart, foreign_cart])
            await session.commit()
            own_cart_id = own_cart.id
            foreign_cart_id = foreign_cart.id

            session.add(
                models.CartItem(
                    tenant_id=tenant.id,
                    cart_id=own_cart_id,
                    product_id=other_tenant_product.id,
                    quantity=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            session.add(
                models.CartItem(
                    tenant_id=tenant.id,
                    cart_id=foreign_cart_id,
                    product_id=sample_product.id,
                    quantity=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

    asyncio.run(verify())


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "paid"},
        {"currency": "US"},
        {"currency": "ZZZ"},
        {"request_fingerprint": "g" * 64},
        {"request_fingerprint": "a" * 63},
        {"subtotal_units": -1},
        {"subtotal_units": 1_000_000_000_000_000},
        {"total_units": 1},
        {"status": "cancelled"},
        {"cancellation_reason": "administrator"},
    ],
)
def test_order_checks_reject_invalid_values(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
    overrides: dict[str, object],
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            values = _order_values(tenant.id, customer.id) | overrides
            session.add(models.Order(**values))
            with pytest.raises((IntegrityError, OverflowError)):
                await session.commit()

    asyncio.run(verify())


def test_cancelled_order_requires_consistent_cancellation_metadata(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            cancelled = models.Order(
                **_order_values(
                    tenant.id,
                    customer.id,
                    status="cancelled",
                    cancelled_at=datetime.now(UTC),
                    cancellation_reason="customer",
                )
            )
            session.add(cancelled)
            await session.commit()

            session.add(
                models.Order(
                    **_order_values(
                        tenant.id,
                        customer.id,
                        idempotency_key="inconsistent-cancel",
                        cancellation_reason="customer",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(verify())


def test_idempotency_key_unique_per_customer_and_tenant(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
    other_tenant: models.Tenant,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            same_tenant_customer = models.User(
                email="another-customer@example.com",
                hashed_password="test-hash",
                role="Customer",
                tenant_id=tenant.id,
            )
            other_tenant_customer = models.User(
                email="other-customer@example.com",
                hashed_password="test-hash",
                role="Customer",
                tenant_id=other_tenant.id,
            )
            session.add_all([same_tenant_customer, other_tenant_customer])
            await session.flush()
            session.add(
                models.Order(
                    **_order_values(
                        tenant.id,
                        customer.id,
                        idempotency_key="shared-key",
                    )
                )
            )
            session.add(
                models.Order(
                    **_order_values(
                        tenant.id,
                        same_tenant_customer.id,
                        idempotency_key="shared-key",
                    )
                )
            )
            session.add(
                models.Order(
                    **_order_values(
                        other_tenant.id,
                        other_tenant_customer.id,
                        idempotency_key="shared-key",
                    )
                )
            )
            await session.commit()

            session.add(
                models.Order(
                    **_order_values(
                        tenant.id,
                        customer.id,
                        idempotency_key="shared-key",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(verify())


@pytest.mark.parametrize(
    "overrides",
    [
        {"line_number": 0},
        {"unit_price_units": 0},
        {"unit_price_units": 1_000_000_000_000_000},
        {"quantity": 0},
        {"quantity": 1001},
        {"line_total_units": 1},
        {"currency": "USD"},
        {"tenant_id": "another-tenant"},
        {"source_tenant_id": "another-tenant"},
    ],
)
def test_order_item_checks_reject_invalid_snapshots(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
    overrides: dict[str, object],
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            order = models.Order(**_order_values(tenant.id, customer.id))
            session.add(order)
            await session.flush()
            item_values: dict[str, object] = {
                "tenant_id": tenant.id,
                "order_id": order.id,
                "line_number": 1,
                "source_tenant_id": None,
                "source_product_id": None,
                "product_name": "Snapshot product",
                "unit_price_units": 10_000,
                "currency": "PKR",
                "quantity": 1,
                "line_total_units": 10_000,
            }
            item_values.update(overrides)
            session.add(models.OrderItem(**item_values))
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(verify())


def test_order_item_line_number_must_be_unique(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            order = models.Order(**_order_values(tenant.id, customer.id))
            session.add(order)
            await session.flush()
            session.add_all(
                [
                    _order_item(
                        tenant.id,
                        order.id,
                        source_tenant_id=None,
                        source_product_id=None,
                    ),
                    _order_item(
                        tenant.id,
                        order.id,
                        source_tenant_id=None,
                        source_product_id=None,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(verify())


def test_order_item_source_is_nulled_without_changing_snapshot(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
    sample_product: models.Product,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            order = models.Order(**_order_values(tenant.id, customer.id))
            session.add(order)
            await session.flush()
            item = _order_item(
                tenant.id,
                order.id,
                source_tenant_id=tenant.id,
                source_product_id=sample_product.id,
                product_name=sample_product.name,
            )
            session.add(item)
            await session.commit()
            item_id = item.id
            snapshot_name = sample_product.name

            with pytest.raises(IntegrityError, match="snapshots are immutable"):
                await session.execute(
                    update(models.OrderItem)
                    .where(models.OrderItem.id == item_id)
                    .values(product_name="Changed")
                )
            await session.rollback()

            await session.execute(
                delete(models.Product).where(models.Product.id == sample_product.id)
            )
            await session.commit()

            stored_item = await session.scalar(
                select(models.OrderItem).where(models.OrderItem.id == item_id)
            )
            assert stored_item is not None
            assert stored_item.source_tenant_id is None
            assert stored_item.source_product_id is None
            assert stored_item.product_name == snapshot_name
            assert stored_item.unit_price_units == 10_000
            assert stored_item.quantity == 1
            assert stored_item.line_total_units == 10_000

            with pytest.raises(IntegrityError, match="snapshots are immutable"):
                await session.execute(
                    update(models.OrderItem)
                    .where(models.OrderItem.id == item_id)
                    .values(source_tenant_id="another-tenant", source_product_id=99)
                )
            await session.rollback()

    asyncio.run(verify())


def test_retained_orders_restrict_customer_and_tenant_deletion(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
    customer: models.User,
) -> None:
    async def verify() -> None:
        async with db_session() as session:
            session.add(models.Order(**_order_values(tenant.id, customer.id)))
            await session.commit()

            with pytest.raises(IntegrityError):
                await session.execute(
                    delete(models.User).where(models.User.id == customer.id)
                )
            await session.rollback()

            with pytest.raises(IntegrityError):
                await session.execute(
                    delete(models.Tenant).where(models.Tenant.id == tenant.id)
                )

    asyncio.run(verify())
