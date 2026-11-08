import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import models
from app.services import cart_service


def _create_product(
    db_session: async_sessionmaker[AsyncSession],
    tenant_id: str,
    name: str,
    currency: str,
) -> models.Product:
    async def create() -> models.Product:
        async with db_session() as session:
            product = models.Product(
                name=name,
                description=None,
                price=Decimal("3.25"),
                stock=0,
                is_available=True,
                tenant_id=tenant_id,
                currency=currency,
            )
            session.add(product)
            await session.commit()
            return product

    return asyncio.run(create())


def test_customer_can_view_add_update_and_remove_cart_items(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    headers = auth_headers(customer)
    empty = client.get("/cart/", headers=headers)
    added = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 2},
    )

    assert empty.status_code == 200
    assert empty.json()["id"] is None
    assert empty.json()["items"] == []
    assert added.status_code == 201
    assert added.json()["items"] == [
        {
            "product_id": sample_product.id,
            "product_name": "Sample Product",
            "quantity": 2,
            "unit_price": "19.99",
            "currency": "PKR",
            "is_available": True,
            "line_total": "39.98",
        }
    ]

    updated = client.put(
        f"/cart/items/{sample_product.id}",
        headers=headers,
        json={"quantity": 3},
    )
    viewed = client.get("/cart/", headers=headers)
    removed = client.delete(
        f"/cart/items/{sample_product.id}",
        headers=headers,
    )
    after_remove = client.get("/cart/", headers=headers)

    assert updated.status_code == 200
    assert updated.json()["items"][0]["quantity"] == 3
    assert updated.json()["items"][0]["line_total"] == "59.97"
    assert viewed.json()["items"][0]["unit_price"] == "19.99"
    assert removed.status_code == 204
    assert after_remove.json()["items"] == []


def test_cart_prices_always_come_from_current_product_data(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    added = client.post(
        "/cart/items",
        headers=headers,
        json={
            "product_id": sample_product.id,
            "quantity": 2,
            "price": "0.01",
            "currency": "USD",
            "customer_id": 999999,
            "tenant_id": "attacker-tenant",
            "total": "0.02",
        },
    )
    assert added.status_code == 422

    added = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 2},
    )
    assert added.status_code == 201

    async def change_product_price() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            product.price = Decimal("12.3456")
            await session.commit()

    async_run(change_product_price())
    viewed = client.get("/cart/", headers=headers)
    assert viewed.status_code == 200
    assert viewed.json()["items"][0]["unit_price"] == "12.3456"
    assert viewed.json()["items"][0]["line_total"] == "24.6912"


def test_cart_is_private_to_the_authenticated_customer(
    client: TestClient,
    customer: models.User,
    other_customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    added = client.post(
        "/cart/items",
        headers=auth_headers(customer),
        json={"product_id": sample_product.id, "quantity": 1},
    )
    other_cart = client.get("/cart/", headers=auth_headers(other_customer))

    assert added.status_code == 201
    assert other_cart.status_code == 200
    assert other_cart.json()["items"] == []


def test_cart_hides_products_owned_by_another_tenant(
    client: TestClient,
    customer: models.User,
    other_tenant_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    response = client.post(
        "/cart/items",
        headers=auth_headers(customer),
        json={"product_id": other_tenant_product.id, "quantity": 1},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"


def test_cart_requires_active_customer_role(
    client: TestClient,
    inactive_customer: models.User,
    tenant_admin: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    inactive = client.post(
        "/cart/items",
        headers=auth_headers(inactive_customer),
        json={"product_id": sample_product.id, "quantity": 1},
    )
    wrong_role = client.get("/cart/", headers=auth_headers(tenant_admin))

    assert inactive.status_code == 401
    assert wrong_role.status_code == 403


def test_cart_write_revalidates_user_status_in_its_transaction(
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    tenant_id = customer.tenant_id
    assert tenant_id is not None

    async def deactivate_and_attempt_write() -> None:
        async with db_session() as session:
            user = await session.get(models.User, customer.id)
            assert user is not None
            user.is_active = False
            await session.commit()

        async with db_session() as session:
            with pytest.raises(cart_service.CartAuthenticationError):
                await cart_service.add_item(
                    session,
                    customer.id,
                    tenant_id,
                    sample_product.id,
                    1,
                )

    async_run(deactivate_and_attempt_write())


def test_cart_write_revalidates_that_user_still_exists(
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    tenant_id = customer.tenant_id
    assert tenant_id is not None

    async def delete_and_attempt_write() -> None:
        async with db_session() as session:
            user = await session.get(models.User, customer.id)
            assert user is not None
            await session.delete(user)
            await session.commit()

        async with db_session() as session:
            with pytest.raises(cart_service.CartAuthenticationError):
                await cart_service.add_item(
                    session,
                    customer.id,
                    tenant_id,
                    sample_product.id,
                    1,
                )

    async_run(delete_and_attempt_write())


def test_cart_write_revalidates_role_and_tenant_in_its_transaction(
    customer: models.User,
    tenant: models.Tenant,
    other_tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    async def change_role_then_attempt_write() -> None:
        async with db_session() as session:
            user = await session.get(models.User, customer.id)
            assert user is not None
            user.role = "TenantAdmin"
            await session.commit()

        async with db_session() as session:
            with pytest.raises(cart_service.CartAuthorizationError):
                await cart_service.add_item(
                    session,
                    customer.id,
                    tenant.id,
                    sample_product.id,
                    1,
                )

    async def change_tenant_then_attempt_write() -> None:
        async with db_session() as session:
            user = await session.get(models.User, customer.id)
            assert user is not None
            user.role = "Customer"
            user.tenant_id = other_tenant.id
            await session.commit()

        async with db_session() as session:
            with pytest.raises(cart_service.CartAuthorizationError):
                await cart_service.add_item(
                    session,
                    customer.id,
                    tenant.id,
                    sample_product.id,
                    1,
                )

    async_run(change_role_then_attempt_write())
    async_run(change_tenant_then_attempt_write())


def test_unavailable_or_invalid_products_cannot_be_added_to_cart(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    async def update_product(**values: Any) -> None:
        async with db_session() as session:
            product = await session.get(models.Product, sample_product.id)
            assert product is not None
            for name, value in values.items():
                setattr(product, name, value)
            await session.commit()

    headers = auth_headers(customer)
    async_run(update_product(is_available=False))
    unavailable = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    assert unavailable.status_code == 409

    async_run(update_product(is_available=True, currency="ZZZ"))
    invalid_currency = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    assert invalid_currency.status_code == 409

    async_run(
        update_product(
            currency="PKR",
            price=Decimal("100000000000.0000"),
        )
    )
    invalid_price = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    assert invalid_price.status_code == 409


def test_cart_quantity_boundaries_and_duplicate_prevention(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    headers = auth_headers(customer)
    minimum = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    duplicate = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    maximum = client.put(
        f"/cart/items/{sample_product.id}",
        headers=headers,
        json={"quantity": 1000},
    )

    assert minimum.status_code == 201
    assert duplicate.status_code == 409
    assert maximum.status_code == 200
    assert maximum.json()["items"][0]["quantity"] == 1000

    for quantity in (0, 1001):
        assert (
            client.post(
                "/cart/items",
                headers=headers,
                json={"product_id": sample_product.id + 1, "quantity": quantity},
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/cart/items/{sample_product.id}",
                headers=headers,
                json={"quantity": quantity},
            ).status_code
            == 422
        )


def test_cart_rejects_decimal_and_boolean_quantities(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    for quantity in (1.5, True):
        response = client.post(
            "/cart/items",
            headers=auth_headers(customer),
            json={"product_id": sample_product.id, "quantity": quantity},
        )
        assert response.status_code == 422


def test_concurrent_cart_updates_remain_serialized(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    headers = auth_headers(customer)
    initial = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    assert initial.status_code == 201

    def update(quantity: int) -> int:
        return client.put(
            f"/cart/items/{sample_product.id}",
            headers=headers,
            json={"quantity": quantity},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(update, (17, 29)))

    cart = client.get("/cart/", headers=headers)
    assert statuses == [200, 200]
    assert len(cart.json()["items"]) == 1
    assert cart.json()["items"][0]["quantity"] in (17, 29)


def test_adding_same_currency_product_succeeds(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    second_product = _create_product(
        db_session,
        tenant.id,
        "Another PKR product",
        "PKR",
    )
    headers = auth_headers(customer)

    first = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    second = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": second_product.id, "quantity": 2},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert len(second.json()["items"]) == 2
    assert {item["currency"] for item in second.json()["items"]} == {"PKR"}


def test_adding_different_currency_is_rejected_without_cart_changes(
    client: TestClient,
    customer: models.User,
    tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    usd_product = _create_product(db_session, tenant.id, "USD product", "USD")
    headers = auth_headers(customer)
    added = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 2},
    )

    rejected = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": usd_product.id, "quantity": 1},
    )
    cart = client.get("/cart/", headers=headers)

    assert added.status_code == 201
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "Cart items must use a single currency"
    assert len(cart.json()["items"]) == 1
    assert cart.json()["items"][0]["product_id"] == sample_product.id
    assert cart.json()["items"][0]["quantity"] == 2


def test_updating_item_rejects_currency_inconsistent_with_other_lines(
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
        "Another PKR product",
        "PKR",
    )
    headers = auth_headers(customer)
    for product_id in (sample_product.id, second_product.id):
        response = client.post(
            "/cart/items",
            headers=headers,
            json={"product_id": product_id, "quantity": 1},
        )
        assert response.status_code == 201

    async def change_currency() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, second_product.id)
            assert product is not None
            product.currency = "USD"
            await session.commit()

    async_run(change_currency())
    rejected = client.put(
        f"/cart/items/{sample_product.id}",
        headers=headers,
        json={"quantity": 9},
    )
    assert rejected.status_code == 409

    response = client.get("/cart/", headers=headers)
    assert response.status_code == 409


def test_mixed_cart_is_rejected_but_removal_restores_a_valid_cart(
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
        "Another PKR product",
        "PKR",
    )
    headers = auth_headers(customer)
    for product_id in (sample_product.id, second_product.id):
        response = client.post(
            "/cart/items",
            headers=headers,
            json={"product_id": product_id, "quantity": 1},
        )
        assert response.status_code == 201

    async def change_currency() -> None:
        async with db_session() as session:
            product = await session.get(models.Product, second_product.id)
            assert product is not None
            product.currency = "USD"
            await session.commit()

    async_run(change_currency())
    mixed = client.get("/cart/", headers=headers)
    removed = client.delete(
        f"/cart/items/{second_product.id}",
        headers=headers,
    )
    valid = client.get("/cart/", headers=headers)

    assert mixed.status_code == 409
    assert mixed.json()["detail"] == "Cart items must use a single currency"
    assert removed.status_code == 204
    assert valid.status_code == 200
    assert len(valid.json()["items"]) == 1
    assert valid.json()["items"][0]["product_id"] == sample_product.id


def test_failed_update_rolls_back_all_cart_changes(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    created = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    assert created.status_code == 201

    async def create_blocking_trigger() -> None:
        async with db_session() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER block_cart_item_update "
                    "BEFORE UPDATE ON cart_items BEGIN "
                    "SELECT RAISE(ABORT, 'blocked'); END"
                )
            )
            await session.commit()

    async def drop_blocking_trigger() -> None:
        async with db_session() as session:
            await session.execute(text("DROP TRIGGER block_cart_item_update"))
            await session.commit()

    async_run(create_blocking_trigger())
    failed = client.put(
        f"/cart/items/{sample_product.id}",
        headers=headers,
        json={"quantity": 8},
    )
    async_run(drop_blocking_trigger())
    cart = client.get("/cart/", headers=headers)

    assert failed.status_code == 409
    assert failed.json()["detail"] == "Cart could not be updated"
    assert cart.status_code == 200
    assert cart.json()["items"][0]["quantity"] == 1


def test_failed_remove_rolls_back_and_keeps_cart_item(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    headers = auth_headers(customer)
    created = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": sample_product.id, "quantity": 1},
    )
    assert created.status_code == 201

    async def create_blocking_trigger() -> None:
        async with db_session() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER block_cart_item_delete "
                    "BEFORE DELETE ON cart_items BEGIN "
                    "SELECT RAISE(ABORT, 'blocked'); END"
                )
            )
            await session.commit()

    async def drop_blocking_trigger() -> None:
        async with db_session() as session:
            await session.execute(text("DROP TRIGGER block_cart_item_delete"))
            await session.commit()

    async_run(create_blocking_trigger())
    failed = client.delete(
        f"/cart/items/{sample_product.id}",
        headers=headers,
    )
    async_run(drop_blocking_trigger())
    cart = client.get("/cart/", headers=headers)

    assert failed.status_code == 409
    assert failed.json()["detail"] == "Cart could not be updated"
    assert cart.status_code == 200
    assert cart.json()["items"][0]["quantity"] == 1
