import asyncio
import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import models
from app.services import order_service, payment_service
from app.services.payment_providers import VerifiedPaymentEvent


def _create_product(
    db_session: async_sessionmaker[AsyncSession],
    tenant_id: str,
    name: str,
    *,
    price: Decimal = Decimal("19.99"),
    currency: str = "PKR",
    stock: int = 7,
) -> models.Product:
    async def create() -> models.Product:
        async with db_session() as session:
            product = models.Product(
                name=name,
                description=None,
                price=price,
                stock=stock,
                is_available=True,
                tenant_id=tenant_id,
                currency=currency,
            )
            session.add(product)
            await session.commit()
            return product

    return asyncio.run(create())


def _cart_add(
    client: TestClient,
    headers: dict[str, str],
    product_id: int,
    quantity: int = 1,
) -> None:
    response = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": product_id, "quantity": quantity},
    )
    assert response.status_code == 201


def _checkout(
    client: TestClient,
    headers: dict[str, str],
    key: str = "checkout-request-1",
):
    return client.post(
        "/orders/",
        headers={**headers, "Idempotency-Key": key},
    )


def _create_payment_attempt(
    db_session: async_sessionmaker[AsyncSession],
    *,
    customer_id: int,
    tenant_id: str,
    order_id: int,
) -> tuple[int, str, int, str]:
    async def create() -> tuple[int, str, int, str]:
        async with db_session() as session:
            session.add(
                models.TenantPaymentAccount(
                    tenant_id=tenant_id,
                    provider="test_gateway",
                    merchant_account_reference="order-fulfillment-test-account",
                    is_active=True,
                )
            )
            await session.commit()

        async with db_session() as session:
            payment, created = await payment_service.create_payment_attempt(
                session,
                customer_id=customer_id,
                tenant_id=tenant_id,
                order_id=order_id,
                merchant_account_reference="order-fulfillment-test-account",
                idempotency_key="order-fulfillment-payment",
            )
            assert created
            return (
                payment.id,
                payment.internal_reference,
                payment.amount_units,
                payment.currency,
            )

    return asyncio.run(create())


def _mark_order_paid(
    db_session: async_sessionmaker[AsyncSession],
    *,
    customer_id: int,
    tenant_id: str,
    order_id: int,
) -> int:
    payment_id, payment_reference, amount_units, currency = _create_payment_attempt(
        db_session,
        customer_id=customer_id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    event = VerifiedPaymentEvent(
        provider="test_gateway",
        event_id="order-fulfillment-payment-succeeded",
        payment_reference=payment_reference,
        order_reference=str(order_id),
        amount_units=amount_units,
        currency=currency,
        status="succeeded",
        payload_sha256=hashlib.sha256(
            b"order-fulfillment-payment-succeeded"
        ).hexdigest(),
    )

    async def process() -> None:
        async with db_session() as session:
            result = await payment_service.process_verified_payment_event(
                session,
                tenant_id=tenant_id,
                merchant_account_reference="order-fulfillment-test-account",
                event=event,
            )
            assert result.status == "processed"

    asyncio.run(process())
    return payment_id


def _mark_order_expired(
    db_session: async_sessionmaker[AsyncSession],
    order_id: int,
) -> None:
    async def expire() -> None:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            assert order is not None
            order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire())


def test_checkout_uses_current_prices_creates_snapshots_and_clears_cart(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)

    async def update_product() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            product.name = "Renamed before checkout"
            product.price = Decimal("12.3456")
            await session.commit()

    async_run(update_product())
    response = _checkout(client, headers)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "pending"
    assert order["currency"] == "PKR"
    assert order["subtotal"] == "24.6912"
    assert order["total"] == "24.6912"
    assert order["items"] == [
        {
            "line_number": 1,
            "product_id": sample_product.id,
            "product_name": "Renamed before checkout",
            "unit_price": "12.3456",
            "currency": "PKR",
            "quantity": 2,
            "line_total": "24.6912",
        }
    ]

    cart = client.get("/cart/", headers=headers)
    detail = client.get(f"/orders/{order['id']}", headers=headers)
    history = client.get("/orders/", headers=headers)

    async def load_product_and_order() -> tuple[
        int, models.Order, list[models.OrderItem]
    ]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            stored_order = await session.get(models.Order, order["id"])
            assert stored_order is not None
            items = list(
                await session.scalars(
                    select(models.OrderItem).where(
                        models.OrderItem.order_id == stored_order.id
                    )
                )
            )
            return product.stock, stored_order, items

    stock, stored_order, stored_items = async_run(load_product_and_order())

    assert cart.status_code == 200
    assert cart.json()["items"] == []
    assert detail.status_code == 200
    assert detail.json() == order
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["id"] == order["id"]
    assert stock == 5
    assert stored_order.subtotal_units == 246_912
    assert stored_items[0].product_name == "Renamed before checkout"
    assert stored_items[0].unit_price_units == 123_456
    assert stored_items[0].quantity == 2
    assert stored_items[0].line_total_units == 246_912


def test_idempotent_retry_returns_original_order_after_cart_clear(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)
    first = _checkout(client, headers)
    retry = _checkout(client, headers)

    async def load_state() -> tuple[int, int]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            order_count = await session.scalar(
                select(func.count()).select_from(models.Order)
            )
            return product.stock, order_count or 0

    stock, order_count = async_run(load_state())

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert stock == 5
    assert order_count == 1


def test_idempotency_key_reuse_with_different_cart_payload_conflicts(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    another_product = _create_product(
        db_session,
        tenant.id,
        "Different checkout product",
        stock=4,
    )
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=1)
    first = _checkout(client, headers, "stable-key")
    _cart_add(client, headers, another_product.id, quantity=1)
    reused = _checkout(client, headers, "stable-key")

    async def load_state() -> tuple[int, int, int]:
        async with db_session() as session:
            product = await session.get(models.Product, another_product.id)
            assert product is not None
            order_count = await session.scalar(
                select(func.count()).select_from(models.Order)
            )
            cart_line_count = await session.scalar(
                select(func.count()).select_from(models.CartItem)
            )
            return product.stock, order_count or 0, cart_line_count or 0

    stock, order_count, cart_line_count = async_run(load_state())

    assert first.status_code == 201
    assert reused.status_code == 409
    assert (
        reused.json()["detail"] == "Idempotency key conflicts with a previous checkout"
    )
    assert stock == 4
    assert order_count == 1
    assert cart_line_count == 1


def test_empty_cart_and_insufficient_stock_do_not_create_orders(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    empty = _checkout(client, headers, "empty-cart")
    _cart_add(client, headers, sample_product.id, quantity=8)
    insufficient = _checkout(client, headers, "insufficient-stock")

    async def load_state() -> tuple[int, int, int]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            order_count = await session.scalar(
                select(func.count()).select_from(models.Order)
            )
            cart_quantity = await session.scalar(
                select(models.CartItem.quantity).where(
                    models.CartItem.product_id == sample_product.id
                )
            )
            return product.stock, order_count or 0, cart_quantity or 0

    stock, order_count, cart_quantity = async_run(load_state())

    assert empty.status_code == 409
    assert insufficient.status_code == 409
    assert insufficient.json()["detail"] == (
        "Insufficient stock for one or more cart items"
    )
    assert stock == 7
    assert order_count == 0
    assert cart_quantity == 8


def test_checkout_revalidates_customer_role_inside_write_transaction(
    customer: models.User,
    tenant: models.Tenant,
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    async def change_role_then_checkout() -> None:
        async with db_session() as session:
            user = await session.get(models.User, customer.id)
            assert user is not None
            user.role = "TenantAdmin"
            await session.commit()

        async with db_session() as session:
            with pytest.raises(order_service.OrderAuthorizationError):
                await order_service.create_order(
                    session,
                    customer.id,
                    tenant.id,
                    "revoked-customer-role",
                )

    async_run(change_role_then_checkout())


def test_checkout_rejects_mixed_currency_cart_without_changing_stock(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    other_product = _create_product(
        db_session,
        tenant.id,
        "Second currency product",
        currency="PKR",
    )
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id)
    _cart_add(client, headers, other_product.id)

    async def change_currency() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, other_product.id)
            assert product is not None
            product.currency = "USD"
            await session.commit()

    async_run(change_currency())
    response = _checkout(client, headers)

    async def load_stock() -> tuple[int, int]:
        async with db_session() as session:
            product1 = await session.get(models.Product, sample_product.id)
            product2 = await session.get(models.Product, other_product.id)
            assert product1 is not None and product2 is not None
            return product1.stock + product2.stock, int(
                await session.scalar(select(func.count()).select_from(models.Order))
                or 0
            )

    stock_sum, order_count = async_run(load_stock())

    assert response.status_code == 409
    assert response.json()["detail"] == "Cart products must use one supported currency"
    assert stock_sum == 14
    assert order_count == 0


def test_order_detail_is_not_visible_to_another_tenant_customer(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    other_tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    async def create_other_customer() -> models.User:
        async with db_session() as session:
            user = models.User(
                email="different-tenant-customer@example.com",
                hashed_password="test-hash",
                role="Customer",
                tenant_id=other_tenant.id,
            )
            session.add(user)
            await session.commit()
            return user

    other_customer = async_run(create_other_customer())
    owner_headers = auth_headers(customer)
    _cart_add(client, owner_headers, sample_product.id)
    created = _checkout(client, owner_headers)

    response = client.get(
        f"/orders/{created.json()['id']}",
        headers=auth_headers(other_customer),
    )
    other_history = client.get("/orders/", headers=auth_headers(other_customer))

    assert created.status_code == 201
    assert response.status_code == 404
    assert other_history.status_code == 200
    assert other_history.json() == []


def test_failed_order_item_insert_rolls_back_stock_order_and_cart(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)

    async def create_blocking_trigger() -> None:
        async with db_session() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER block_order_item_insert "
                    "BEFORE INSERT ON order_items BEGIN "
                    "SELECT RAISE(ABORT, 'blocked'); END"
                )
            )
            await session.commit()

    async def drop_blocking_trigger() -> None:
        async with db_session() as session:
            await session.execute(text("DROP TRIGGER block_order_item_insert"))
            await session.commit()

    async_run(create_blocking_trigger())
    response = _checkout(client, headers, "rollback-key")
    async_run(drop_blocking_trigger())
    cart = client.get("/cart/", headers=headers)

    async def load_state() -> tuple[int, int]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            order_count = await session.scalar(
                select(func.count()).select_from(models.Order)
            )
            return product.stock, order_count or 0

    stock, order_count = async_run(load_state())

    assert response.status_code == 409
    assert response.json()["detail"] == "Order could not be created"
    assert stock == 7
    assert order_count == 0
    assert cart.status_code == 200
    assert cart.json()["items"][0]["quantity"] == 2


def test_concurrent_duplicate_checkouts_create_one_order_and_deduct_once(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)

    def checkout() -> tuple[int, dict[str, Any]]:
        response = _checkout(client, headers, "concurrent-same-key")
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: checkout(), range(2)))

    async def load_state() -> tuple[int, int]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            count = await session.scalar(select(func.count()).select_from(models.Order))
            return product.stock, count or 0

    stock, order_count = async_run(load_state())
    order_ids = {result[1]["id"] for result in results}

    assert sorted(result[0] for result in results) == [200, 201]
    assert len(order_ids) == 1
    assert stock == 5
    assert order_count == 1


def test_concurrent_checkouts_cannot_oversell_shared_stock(
    client: TestClient,
    customer: models.User,
    other_customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    first_headers = auth_headers(customer)
    second_headers = auth_headers(other_customer)
    _cart_add(client, first_headers, sample_product.id)
    _cart_add(client, second_headers, sample_product.id)

    async def limit_stock() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            product.stock = 1
            await session.commit()

    async_run(limit_stock())

    def checkout(headers: dict[str, str], key: str) -> int:
        return _checkout(client, headers, key).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(
            executor.map(
                lambda args: checkout(*args),
                (
                    (first_headers, "limited-stock-one"),
                    (second_headers, "limited-stock-two"),
                ),
            )
        )

    async def load_state() -> tuple[int, int, int]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            order_count = await session.scalar(
                select(func.count()).select_from(models.Order)
            )
            remaining_lines = await session.scalar(
                select(func.count()).select_from(models.CartItem)
            )
            return (
                product.stock,
                order_count or 0,
                remaining_lines or 0,
            )

    stock, order_count, remaining_lines = async_run(load_state())

    assert sorted(statuses) == [201, 409]
    assert stock == 0
    assert order_count == 1
    assert remaining_lines == 1


def test_expiration_restores_stock_for_multiple_items_and_only_once(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    second_product = _create_product(
        db_session,
        tenant.id,
        "Second expiration product",
        stock=9,
    )
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)
    _cart_add(client, headers, second_product.id, quantity=3)
    created = _checkout(client, headers, "expire-multiple-items")
    assert created.status_code == 201
    order_id = created.json()["id"]
    _mark_order_expired(db_session, order_id)

    async def expire_twice() -> tuple[int, int]:
        async with db_session() as session:
            first = await order_service.expire_pending_orders(session)
        async with db_session() as session:
            second = await order_service.expire_pending_orders(session)
        return first, second

    expired_counts = async_run(expire_twice())

    async def load_state() -> tuple[models.Order, int, int, int, int]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            product_one = await session.get(models.Product, sample_product.id)
            product_two = await session.get(models.Product, second_product.id)
            assert order is not None
            assert product_one is not None and product_two is not None
            return (
                order,
                product_one.stock,
                product_one.version,
                product_two.stock,
                product_two.version,
            )

    order, first_stock, first_version, second_stock, second_version = async_run(
        load_state()
    )

    assert expired_counts == (1, 0)
    assert order.status == "cancelled"
    assert order.cancellation_reason == "expiration"
    assert order.cancelled_at is not None
    assert first_stock == 7
    assert first_version == 3
    assert second_stock == 9
    assert second_version == 3


def test_concurrent_expiration_runs_restore_stock_once(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)
    created = _checkout(client, headers, "concurrent-expiration")
    assert created.status_code == 201
    order_id = created.json()["id"]
    _mark_order_expired(db_session, order_id)

    async def expire_concurrently() -> tuple[int, int]:
        async def run_one() -> int:
            async with db_session() as session:
                return await order_service.expire_pending_orders(session)

        return await asyncio.gather(run_one(), run_one())

    results = async_run(expire_concurrently())

    async def load_stock_and_order() -> tuple[int, str]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            order = await session.get(models.Order, order_id)
            assert product is not None and order is not None
            return product.stock, order.status

    stock, order_status = async_run(load_stock_and_order())

    assert sorted(results) == [0, 1]
    assert stock == 7
    assert order_status == "cancelled"


def test_expiration_skips_deleted_source_products(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)
    created = _checkout(client, headers, "deleted-source-expiration")
    assert created.status_code == 201
    order_id = created.json()["id"]

    async def delete_product_and_expire_order() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            order = await session.get(models.Order, order_id)
            assert product is not None and order is not None
            await session.delete(product)
            order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    async_run(delete_product_and_expire_order())

    async def expire() -> int:
        async with db_session() as session:
            return await order_service.expire_pending_orders(session)

    assert async_run(expire()) == 1

    async def load_order_item() -> tuple[str, str | None, int | None]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            item = await session.scalar(
                select(models.OrderItem).where(
                    models.OrderItem.tenant_id == sample_product.tenant_id,
                    models.OrderItem.order_id == order_id,
                )
            )
            assert order is not None and item is not None
            return order.status, item.source_tenant_id, item.source_product_id

    assert async_run(load_order_item()) == ("cancelled", None, None)


def test_customer_can_cancel_own_pending_order_and_retry_safely(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id, quantity=2)
    created = _checkout(client, headers, "customer-cancel")
    order_id = created.json()["id"]

    first = client.post(f"/orders/{order_id}/cancel", headers=headers)
    retry = client.post(f"/orders/{order_id}/cancel", headers=headers)

    async def load_state() -> tuple[models.Order, int, int]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert order is not None and product is not None
            return order, product.stock, product.version

    order, stock, version = async_run(load_state())

    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert retry.status_code == 200
    assert retry.json()["id"] == order_id
    assert order.cancellation_reason == "customer"
    assert order.cancelled_at is not None
    assert stock == 7
    assert version == 3


def test_customer_cannot_cancel_another_customers_or_tenants_order(
    client: TestClient,
    customer: models.User,
    other_customer: models.User,
    tenant: models.Tenant,
    other_tenant: models.Tenant,
    sample_product: models.Product,
    other_tenant_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    async def create_other_tenant_customer() -> models.User:
        async with db_session() as session:
            user = models.User(
                email="cancel-other-tenant@example.com",
                hashed_password="test-hash",
                role="Customer",
                tenant_id=other_tenant.id,
            )
            session.add(user)
            await session.commit()
            return user

    other_tenant_customer = async_run(create_other_tenant_customer())
    owner_headers = auth_headers(customer)
    _cart_add(client, owner_headers, sample_product.id)
    order = _checkout(client, owner_headers, "cancel-isolation")
    order_id = order.json()["id"]
    other_tenant_headers = auth_headers(other_tenant_customer)
    _cart_add(client, other_tenant_headers, other_tenant_product.id)
    other_tenant_order = _checkout(
        client,
        other_tenant_headers,
        "cancel-other-tenant-order",
    )
    other_tenant_order_id = other_tenant_order.json()["id"]

    same_tenant_response = client.post(
        f"/orders/{order_id}/cancel",
        headers=auth_headers(other_customer),
    )
    other_tenant_owner_response = client.post(
        f"/orders/{other_tenant_order_id}/cancel",
        headers=owner_headers,
    )

    assert same_tenant_response.status_code == 404
    assert other_tenant_owner_response.status_code == 404
    assert tenant.id != other_tenant.id


@pytest.mark.parametrize("order_status", ["confirmed", "processing", "completed"])
def test_customer_cannot_cancel_non_pending_orders(
    order_status: str,
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id)
    created = _checkout(client, headers, f"cancel-{order_status}")
    order_id = created.json()["id"]

    async def set_order_status() -> None:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            assert order is not None
            order.status = order_status
            await session.commit()

    async_run(set_order_status())
    response = client.post(f"/orders/{order_id}/cancel", headers=headers)

    async def load_stock_and_order() -> tuple[int, str]:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            order = await session.get(models.Order, order_id)
            assert product is not None and order is not None
            return product.stock, order.status

    stock, stored_status = async_run(load_stock_and_order())

    assert response.status_code == 409
    assert response.json()["detail"] == "Order cannot be cancelled in its current state"
    assert stored_status == order_status
    assert stock == 6


def test_cancel_of_expired_pending_order_expires_it_instead(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id)
    created = _checkout(client, headers, "cancel-expired-order")
    order_id = created.json()["id"]
    _mark_order_expired(db_session, order_id)

    response = client.post(f"/orders/{order_id}/cancel", headers=headers)

    async def load_state() -> tuple[str, str | None, int]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert order is not None and product is not None
            return order.status, order.cancellation_reason, product.stock

    state = async_run(load_state())

    assert response.status_code == 409
    assert response.json()["detail"] == "Order has expired and was cancelled"
    assert state == ("cancelled", "expiration", 7)


def test_tenant_admin_order_list_and_detail_are_tenant_scoped(
    client: TestClient,
    customer: models.User,
    tenant_admin: models.User,
    other_tenant_customer: models.User,
    other_tenant_admin: models.User,
    sample_product: models.Product,
    other_tenant_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    customer_headers = auth_headers(customer)
    _cart_add(client, customer_headers, sample_product.id)
    order = _checkout(client, customer_headers, "admin-visible-order")
    order_id = order.json()["id"]

    other_customer_headers = auth_headers(other_tenant_customer)
    _cart_add(client, other_customer_headers, other_tenant_product.id)
    other_order = _checkout(
        client,
        other_customer_headers,
        "other-tenant-admin-visible-order",
    )
    other_order_id = other_order.json()["id"]

    tenant_headers = auth_headers(tenant_admin)
    tenant_list = client.get("/orders/", headers=tenant_headers)
    tenant_detail = client.get(f"/orders/{order_id}", headers=tenant_headers)
    foreign_detail = client.get(f"/orders/{other_order_id}", headers=tenant_headers)
    foreign_admin_detail = client.get(
        f"/orders/{order_id}",
        headers=auth_headers(other_tenant_admin),
    )

    assert tenant_list.status_code == 200
    assert [item["id"] for item in tenant_list.json()] == [order_id]
    assert tenant_detail.status_code == 200
    assert tenant_detail.json() == order.json()
    assert foreign_detail.status_code == 404
    assert foreign_admin_detail.status_code == 404


def test_customer_cannot_change_fulfillment_and_other_tenant_admin_cannot(
    client: TestClient,
    customer: models.User,
    tenant_admin: models.User,
    other_tenant_admin: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    customer_headers = auth_headers(customer)
    _cart_add(client, customer_headers, sample_product.id)
    order = _checkout(client, customer_headers, "fulfillment-authz-order")
    order_id = order.json()["id"]
    body = {"fulfillment_status": "confirmed"}

    customer_response = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=customer_headers,
        json=body,
    )
    foreign_admin_response = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=auth_headers(other_tenant_admin),
        json=body,
    )
    own_admin_response = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=auth_headers(tenant_admin),
        json=body,
    )

    assert customer_response.status_code == 403
    assert foreign_admin_response.status_code == 404
    assert own_admin_response.status_code == 409


def test_paid_order_advances_sequentially_and_duplicate_requests_are_safe(
    client: TestClient,
    customer: models.User,
    tenant_admin: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    customer_headers = auth_headers(customer)
    _cart_add(client, customer_headers, sample_product.id)
    created = _checkout(client, customer_headers, "paid-fulfillment-order")
    order_id = created.json()["id"]
    payment_id = _mark_order_paid(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )
    admin_headers = auth_headers(tenant_admin)

    payment_status_before = client.get(
        f"/orders/{order_id}",
        headers=admin_headers,
    ).json()["status"]
    invalid_jump = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "processing"},
    )
    confirmed = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "confirmed"},
    )
    duplicate = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "confirmed"},
    )
    processing = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "processing"},
    )
    completed = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "completed"},
    )

    async def load_state() -> tuple[str, str, int]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            payment = await session.get(models.Payment, payment_id)
            product = await session.get(models.Product, sample_product.id)
            assert order is not None and payment is not None and product is not None
            return order.status, payment.status, product.stock

    assert payment_status_before == "pending"
    assert invalid_jump.status_code == 409
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "confirmed"
    assert processing.status_code == 200
    assert processing.json()["status"] == "processing"
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert async_run(load_state()) == ("completed", "succeeded", 6)


@pytest.mark.parametrize("payment_status", ["unpaid", "failed", "expired"])
def test_unpaid_failed_and_expired_payments_cannot_enter_fulfillment(
    payment_status: str,
    client: TestClient,
    customer: models.User,
    tenant_admin: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    customer_headers = auth_headers(customer)
    _cart_add(client, customer_headers, sample_product.id)
    created = _checkout(client, customer_headers, f"invalid-payment-{payment_status}")
    order_id = created.json()["id"]
    payment_id: int | None = None
    if payment_status != "unpaid":
        payment_id, _, _, _ = _create_payment_attempt(
            db_session,
            customer_id=customer.id,
            tenant_id=customer.tenant_id or "",
            order_id=order_id,
        )

        async def set_payment_status() -> None:
            async with db_session() as session:
                payment = await session.get(models.Payment, payment_id)
                assert payment is not None
                payment.status = payment_status
                await session.commit()

        async_run(set_payment_status())

    response = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=auth_headers(tenant_admin),
        json={"fulfillment_status": "confirmed"},
    )

    async def load_status() -> tuple[str, str | None]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            payment = (
                await session.get(models.Payment, payment_id)
                if payment_id is not None
                else None
            )
            assert order is not None
            return order.status, payment.status if payment is not None else None

    assert response.status_code == 409
    assert async_run(load_status()) == (
        "pending",
        payment_status if payment_id else None,
    )


def test_reconciliation_required_success_cannot_enter_fulfillment(
    client: TestClient,
    customer: models.User,
    tenant_admin: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    customer_headers = auth_headers(customer)
    _cart_add(client, customer_headers, sample_product.id)
    created = _checkout(client, customer_headers, "reconciliation-fulfillment-order")
    order_id = created.json()["id"]
    payment_id = _mark_order_paid(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )

    async def require_reconciliation() -> None:
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            assert payment is not None
            payment.reconciliation_required = True
            await session.commit()

    async_run(require_reconciliation())
    response = client.patch(
        f"/orders/{order_id}/fulfillment",
        headers=auth_headers(tenant_admin),
        json={"fulfillment_status": "confirmed"},
    )

    async def load_state() -> tuple[str, str, bool]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            payment = await session.get(models.Payment, payment_id)
            assert order is not None and payment is not None
            return order.status, payment.status, payment.reconciliation_required

    assert response.status_code == 409
    assert async_run(load_state()) == ("pending", "succeeded", True)


def test_cancelled_and_expired_orders_cannot_enter_fulfillment(
    client: TestClient,
    customer: models.User,
    tenant_admin: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    admin_headers = auth_headers(tenant_admin)
    _cart_add(client, headers, sample_product.id)
    cancelled = _checkout(client, headers, "cancelled-fulfillment-order")
    cancelled_response = client.post(
        f"/orders/{cancelled.json()['id']}/cancel",
        headers=headers,
    )
    assert cancelled_response.status_code == 200

    _cart_add(client, headers, sample_product.id)
    expired = _checkout(client, headers, "expired-fulfillment-order")
    expired_order_id = expired.json()["id"]
    _mark_order_expired(db_session, expired_order_id)

    async def expire_order() -> int:
        async with db_session() as session:
            return await order_service.expire_pending_orders(session)

    assert async_run(expire_order()) == 1
    cancelled_update = client.patch(
        f"/orders/{cancelled.json()['id']}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "confirmed"},
    )
    expired_update = client.patch(
        f"/orders/{expired_order_id}/fulfillment",
        headers=admin_headers,
        json={"fulfillment_status": "confirmed"},
    )

    assert cancelled_update.status_code == 409
    assert expired_update.status_code == 409
    assert cancelled_update.json()["detail"] == expired_update.json()["detail"]


def test_expiration_rolls_back_order_and_all_stock_on_restoration_failure(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    second_product = _create_product(
        db_session,
        tenant.id,
        "Restoration failure product",
        stock=9,
    )
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id)
    _cart_add(client, headers, second_product.id)
    created = _checkout(client, headers, "restoration-rollback")
    order_id = created.json()["id"]
    _mark_order_expired(db_session, order_id)

    async def add_failure_trigger() -> None:
        async with db_session() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER reject_second_restock "
                    "BEFORE UPDATE OF stock ON products "
                    f"WHEN OLD.id = {second_product.id} BEGIN "
                    "SELECT RAISE(ABORT, 'restock blocked'); END"
                )
            )
            await session.commit()

    async_run(add_failure_trigger())

    async def expire() -> None:
        async with db_session() as session:
            with pytest.raises(order_service.OrderStockRestorationError):
                await order_service.expire_pending_orders(session)

    async_run(expire())

    async def load_state() -> tuple[str, int, int]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            product_one = await session.get(models.Product, sample_product.id)
            product_two = await session.get(models.Product, second_product.id)
            assert order is not None
            assert product_one is not None and product_two is not None
            return order.status, product_one.stock, product_two.stock

    assert async_run(load_state()) == ("pending", 6, 8)


def test_expiration_rolls_back_when_stock_restoration_would_overflow(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    _cart_add(client, headers, sample_product.id)
    created = _checkout(client, headers, "restoration-overflow")
    order_id = created.json()["id"]
    _mark_order_expired(db_session, order_id)

    async def set_maximum_stock() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            product.stock = models.MAX_SQLITE_INTEGER
            await session.commit()

    async_run(set_maximum_stock())

    async def expire() -> None:
        async with db_session() as session:
            with pytest.raises(order_service.OrderStockRestorationError):
                await order_service.expire_pending_orders(session)

    async_run(expire())

    async def load_state() -> tuple[str, int]:
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert order is not None and product is not None
            return order.status, product.stock

    assert async_run(load_state()) == ("pending", models.MAX_SQLITE_INTEGER)


def test_expiration_batch_honors_batch_size(
    client: TestClient,
    customer: models.User,
    other_customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    first_headers = auth_headers(customer)
    second_headers = auth_headers(other_customer)
    _cart_add(client, first_headers, sample_product.id)
    _cart_add(client, second_headers, sample_product.id)
    first = _checkout(client, first_headers, "batch-expiry-one")
    second = _checkout(client, second_headers, "batch-expiry-two")
    _mark_order_expired(db_session, first.json()["id"])
    _mark_order_expired(db_session, second.json()["id"])

    async def expire_one() -> int:
        async with db_session() as session:
            return await order_service.expire_pending_orders(session, batch_size=1)

    expired_count = async_run(expire_one())

    async def statuses() -> list[tuple[int, str]]:
        async with db_session() as session:
            rows = await session.execute(
                select(models.Order.id, models.Order.status).order_by(models.Order.id)
            )
            return [(row[0], row[1]) for row in rows]

    assert expired_count == 1
    assert [status for _, status in async_run(statuses())] == [
        "cancelled",
        "pending",
    ]


def test_expiration_worker_retries_after_exception(
    db_session: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify() -> int:
        stop_event = asyncio.Event()
        calls = 0
        logged_messages: list[str] = []

        def capture_safe_log(message: str, *args: object) -> None:
            logged_messages.append(
                " ".join((message, *(str(argument) for argument in args)))
            )

        async def fail_once_then_stop(
            _session: AsyncSession,
            *,
            batch_size: int,
        ) -> int:
            nonlocal calls
            assert batch_size == 5
            calls += 1
            if calls == 1:
                raise RuntimeError("private database detail")
            stop_event.set()
            return 0

        monkeypatch.setattr(order_service, "expire_pending_orders", fail_once_then_stop)
        monkeypatch.setattr(order_service.logger, "warning", capture_safe_log)
        await order_service.run_expiration_worker(
            db_session,
            stop_event,
            poll_interval_seconds=1,
            batch_size=5,
        )
        assert len(logged_messages) == 1
        assert "RuntimeError" in logged_messages[0]
        assert "private database detail" not in logged_messages[0]
        return calls

    assert asyncio.run(verify()) == 2


def test_application_shutdown_stops_expiration_worker_before_disposing_engine(
    monkeypatch: pytest.MonkeyPatch,
    async_run: Callable[[Any], Any],
) -> None:
    from app import main

    async def verify() -> tuple[bool, bool]:
        worker_stopped = False
        engine_disposed = False
        worker_started = asyncio.Event()

        async def wait_for_shutdown(
            _session_factory: async_sessionmaker[AsyncSession],
            stop_event: asyncio.Event,
            *,
            poll_interval_seconds: int,
            batch_size: int,
        ) -> None:
            nonlocal worker_stopped
            assert poll_interval_seconds > 0
            assert batch_size > 0
            worker_started.set()
            await stop_event.wait()
            worker_stopped = True

        async def dispose_engine() -> None:
            nonlocal engine_disposed
            engine_disposed = True

        monkeypatch.setattr(
            main.order_service,
            "run_expiration_worker",
            wait_for_shutdown,
        )
        monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose_engine))
        async with main.lifespan(main.app):
            await worker_started.wait()
        return worker_stopped, engine_disposed

    assert async_run(verify()) == (True, True)
