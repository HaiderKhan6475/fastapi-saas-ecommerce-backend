import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import models
from app.services import order_service, payment_service
from app.services.payment_providers import VerifiedPaymentEvent, VerifiedRefundResult


def _create_payment_account(
    db_session: async_sessionmaker[AsyncSession],
    tenant_id: str,
    reference: str = "tenant-test-account",
) -> int:
    async def create() -> int:
        async with db_session() as session:
            account = models.TenantPaymentAccount(
                tenant_id=tenant_id,
                provider="test_gateway",
                merchant_account_reference=reference,
                is_active=True,
            )
            session.add(account)
            await session.commit()
            return account.id

    return asyncio.run(create())


def _create_order(
    client: TestClient,
    headers: dict[str, str],
    product_id: int,
) -> int:
    cart_response = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": product_id, "quantity": 1},
    )
    assert cart_response.status_code == 201
    order_response = client.post(
        "/orders/",
        headers={**headers, "Idempotency-Key": "payment-test-order"},
    )
    assert order_response.status_code == 201
    return order_response.json()["id"]


def _create_payment(
    db_session: async_sessionmaker[AsyncSession],
    *,
    customer_id: int,
    tenant_id: str,
    order_id: int,
    account_reference: str = "tenant-test-account",
    key: str = "payment-attempt-1",
) -> tuple[int, str]:
    async def create() -> tuple[int, str]:
        async with db_session() as session:
            payment, created = await payment_service.create_payment_attempt(
                session,
                customer_id=customer_id,
                tenant_id=tenant_id,
                order_id=order_id,
                merchant_account_reference=account_reference,
                idempotency_key=key,
            )
            assert created
            return payment.id, payment.internal_reference

    return asyncio.run(create())


def _verified_event(
    *,
    event_id: str,
    payment_reference: str,
    order_id: int,
    amount_units: int = 199_900,
    currency: str = "PKR",
    status: str = "succeeded",
    provider_transaction_reference: str | None = None,
) -> VerifiedPaymentEvent:
    return VerifiedPaymentEvent(
        provider="test_gateway",
        event_id=event_id,
        payment_reference=payment_reference,
        order_reference=str(order_id),
        amount_units=amount_units,
        currency=currency,
        status=status,  # type: ignore[arg-type]
        payload_sha256=hashlib.sha256(event_id.encode()).hexdigest(),
        provider_transaction_reference=provider_transaction_reference,
    )


def _process_event(
    db_session: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    event: VerifiedPaymentEvent,
    now: datetime | None = None,
):
    async def process():
        async with db_session() as session:
            return await payment_service.process_verified_payment_event(
                session,
                tenant_id=tenant_id,
                merchant_account_reference="tenant-test-account",
                event=event,
                now=now,
            )

    return asyncio.run(process())


def test_customer_payment_history_is_tenant_and_owner_scoped(
    client: TestClient,
    customer: models.User,
    other_customer: models.User,
    other_tenant_customer: models.User,
    tenant_admin: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, customer.tenant_id or "")
    payment_id, _ = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )

    own_detail = client.get(
        f"/payments/{payment_id}",
        headers=auth_headers(customer),
    )
    own_history = client.get("/payments/", headers=auth_headers(customer))
    other_customer_detail = client.get(
        f"/payments/{payment_id}",
        headers=auth_headers(other_customer),
    )
    other_tenant_detail = client.get(
        f"/payments/{payment_id}",
        headers=auth_headers(other_tenant_customer),
    )
    admin_detail = client.get(
        f"/payments/{payment_id}",
        headers=auth_headers(tenant_admin),
    )

    assert own_detail.status_code == 200
    assert own_detail.json() == own_history.json()[0]
    assert own_detail.json()["amount"] == "19.99"
    assert (
        not {
            "internal_reference",
            "merchant_account_id",
            "provider_transaction_reference",
            "safe_metadata",
        }
        & own_detail.json().keys()
    )
    assert other_customer_detail.status_code == 404
    assert other_tenant_detail.status_code == 404
    assert admin_detail.status_code == 403


def test_payment_attempt_is_idempotent_and_requires_matching_tenant_account(
    client: TestClient,
    customer: models.User,
    other_tenant: models.Tenant,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, customer.tenant_id or "")

    async def create_twice():
        async with db_session() as session:
            first, created = await payment_service.create_payment_attempt(
                session,
                customer_id=customer.id,
                tenant_id=customer.tenant_id or "",
                order_id=order_id,
                merchant_account_reference="tenant-test-account",
                idempotency_key="same-key",
            )
            retry, retry_created = await payment_service.create_payment_attempt(
                session,
                customer_id=customer.id,
                tenant_id=customer.tenant_id or "",
                order_id=order_id,
                merchant_account_reference="tenant-test-account",
                idempotency_key="same-key",
            )
            return first.id, retry.id, created, retry_created

    assert asyncio.run(create_twice()) == (1, 1, True, False)
    _create_payment_account(
        db_session,
        customer.tenant_id or "",
        reference="tenant-alt-account",
    )

    async def wrong_account():
        async with db_session() as session:
            await payment_service.create_payment_attempt(
                session,
                customer_id=customer.id,
                tenant_id=other_tenant.id,
                order_id=order_id,
                merchant_account_reference="tenant-test-account",
                idempotency_key="wrong-tenant",
            )

    with pytest.raises(payment_service.PaymentAuthorizationError):
        asyncio.run(wrong_account())

    async def reuse_key_with_other_account():
        async with db_session() as session:
            await payment_service.create_payment_attempt(
                session,
                customer_id=customer.id,
                tenant_id=customer.tenant_id or "",
                order_id=order_id,
                merchant_account_reference="tenant-alt-account",
                idempotency_key="same-key",
            )

    with pytest.raises(payment_service.PaymentIdempotencyConflictError):
        asyncio.run(reuse_key_with_other_account())


def test_new_payment_attempt_is_blocked_while_existing_attempt_is_active(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, customer.tenant_id or "")
    _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )

    async def create_another_attempt():
        async with db_session() as session:
            await payment_service.create_payment_attempt(
                session,
                customer_id=customer.id,
                tenant_id=customer.tenant_id or "",
                order_id=order_id,
                merchant_account_reference="tenant-test-account",
                idempotency_key="second-attempt",
            )

    with pytest.raises(payment_service.PaymentStateConflictError):
        asyncio.run(create_another_attempt())


def test_database_rejects_duplicate_internal_payment_references(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, customer.tenant_id or "")
    payment_id, _ = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )

    async def duplicate_reference() -> None:
        async with db_session() as session:
            original = await session.get(models.Payment, payment_id)
            assert original is not None
            session.add(
                models.Payment(
                    tenant_id=original.tenant_id,
                    order_id=original.order_id,
                    merchant_account_id=original.merchant_account_id,
                    provider=original.provider,
                    internal_reference=original.internal_reference,
                    idempotency_key="different-key",
                    amount_units=original.amount_units,
                    currency=original.currency,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

    asyncio.run(duplicate_reference())


def test_verified_event_is_idempotent_and_does_not_change_order_fulfillment(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    event = _verified_event(
        event_id="provider-event-1",
        payment_reference=reference,
        order_id=order_id,
        provider_transaction_reference="provider-txn-1",
    )

    first = _process_event(db_session, tenant_id=tenant_id, event=event)
    retry = _process_event(db_session, tenant_id=tenant_id, event=event)

    async def state():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            order = await session.get(models.Order, order_id)
            event_count = await session.scalar(
                select(func.count()).select_from(models.PaymentWebhookEvent)
            )
            assert payment is not None and order is not None
            return payment.status, order.status, event_count

    assert first.status == "processed"
    assert not first.duplicate
    assert retry.duplicate
    assert asyncio.run(state()) == ("succeeded", "pending", 1)


@pytest.mark.parametrize(
    ("order_reference", "amount_units", "currency", "error"),
    [
        ("not-the-order", 199_900, "PKR", "order_mismatch"),
        ("1", 1_000, "PKR", "amount_mismatch"),
        ("1", 199_900, "not-a-currency", "invalid_currency"),
        ("1", 199_900, "USD", "currency_mismatch"),
        ("1", 0, "PKR", "invalid_amount"),
    ],
)
def test_verified_event_rejects_order_amount_and_currency_mismatches(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    order_reference: str,
    amount_units: int,
    currency: str,
    error: str,
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    event = _verified_event(
        event_id=f"invalid-{error}",
        payment_reference=reference,
        order_id=order_id,
        amount_units=amount_units,
        currency=currency,
    )
    event = VerifiedPaymentEvent(
        **{
            **event.__dict__,
            "order_reference": order_reference,
        }
    )

    result = _process_event(db_session, tenant_id=tenant_id, event=event)

    async def status():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            assert payment is not None
            return payment.status

    assert result.status == "rejected"
    assert result.safe_error_code == error
    assert asyncio.run(status()) == "initiated"


def test_late_payment_success_after_customer_cancellation_requires_reconciliation(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
    async_run: Callable[[Any], Any],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    cancellation = client.post(
        f"/orders/{order_id}/cancel",
        headers=auth_headers(customer),
    )
    event = _verified_event(
        event_id="late-success",
        payment_reference=reference,
        order_id=order_id,
    )

    result = _process_event(db_session, tenant_id=tenant_id, event=event)

    async def state():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert payment is not None and order is not None and product is not None
            return (
                payment.status,
                payment.reconciliation_required,
                order.status,
                product.stock,
            )

    assert cancellation.status_code == 200
    assert result.status == "reconciliation_required"
    assert result.reconciliation_required
    assert async_run(state()) == ("succeeded", True, "cancelled", 7)


def test_late_payment_success_after_expiration_does_not_restore_stock_twice(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )

    async def expire_order():
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            assert order is not None
            order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        async with db_session() as session:
            return await order_service.expire_pending_orders(session)

    assert asyncio.run(expire_order()) == 1
    result = _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="success-after-expiration",
            payment_reference=reference,
            order_id=order_id,
        ),
    )

    async def state():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert payment is not None and order is not None and product is not None
            return (
                payment.status,
                payment.reconciliation_required,
                order.status,
                product.stock,
            )

    assert result.status == "reconciliation_required"
    assert asyncio.run(state()) == ("succeeded", True, "cancelled", 7)


def test_successful_payment_before_expiry_prevents_expiry_and_customer_cancellation(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    paid_at = datetime.now(UTC)

    async def shorten_expiry():
        async with db_session() as session:
            order = await session.get(models.Order, order_id)
            assert order is not None
            order.expires_at = paid_at + timedelta(seconds=1)
            await session.commit()

    asyncio.run(shorten_expiry())
    _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="paid-before-expiry",
            payment_reference=reference,
            order_id=order_id,
        ),
        now=paid_at,
    )

    async def expire_order():
        async with db_session() as session:
            return await order_service.expire_pending_orders(
                session,
                now=paid_at + timedelta(seconds=2),
            )

    assert asyncio.run(expire_order()) == 0
    cancellation = client.post(
        f"/orders/{order_id}/cancel",
        headers=auth_headers(customer),
    )

    async def state():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert payment is not None and order is not None and product is not None
            return payment.status, order.status, product.stock

    assert cancellation.status_code == 409
    assert asyncio.run(state()) == ("succeeded", "pending", 6)


def test_customer_cannot_cancel_order_with_succeeded_payment(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="payment-before-cancel",
            payment_reference=reference,
            order_id=order_id,
        ),
    )

    response = client.post(
        f"/orders/{order_id}/cancel",
        headers=auth_headers(customer),
    )

    async def state():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            order = await session.get(models.Order, order_id)
            product = await session.get(models.Product, sample_product.id)
            assert payment is not None and order is not None and product is not None
            return (
                payment.status,
                payment.reconciliation_required,
                order.status,
                product.stock,
            )

    assert response.status_code == 409
    assert asyncio.run(state()) == ("succeeded", False, "pending", 6)


def test_second_successful_attempt_is_flagged_for_reconciliation(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    account_id = _create_payment_account(db_session, tenant_id)
    first_payment_id, first_reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )

    async def insert_second_attempt() -> tuple[int, str]:
        async with db_session() as session:
            second = models.Payment(
                tenant_id=tenant_id,
                order_id=order_id,
                merchant_account_id=account_id,
                provider="test_gateway",
                idempotency_key="second-attempt-from-legacy-state",
                amount_units=199_900,
                currency="PKR",
                status="initiated",
            )
            session.add(second)
            await session.commit()
            return second.id, second.internal_reference

    second_payment_id, second_reference = asyncio.run(insert_second_attempt())
    first_result = _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="first-attempt-success",
            payment_reference=first_reference,
            order_id=order_id,
        ),
    )
    second_result = _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="second-attempt-success",
            payment_reference=second_reference,
            order_id=order_id,
        ),
    )

    async def state():
        async with db_session() as session:
            first = await session.get(models.Payment, first_payment_id)
            second = await session.get(models.Payment, second_payment_id)
            assert first is not None and second is not None
            return (
                first.status,
                first.reconciliation_required,
                second.status,
                second.reconciliation_required,
            )

    assert first_result.status == "processed"
    assert second_result.status == "reconciliation_required"
    assert asyncio.run(state()) == ("succeeded", True, "succeeded", True)


def test_duplicate_event_id_with_different_payload_is_rejected(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    _, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    first_event = _verified_event(
        event_id="same-event-id",
        payment_reference=reference,
        order_id=order_id,
        status="pending",
    )
    conflicting_event = VerifiedPaymentEvent(
        **{
            **first_event.__dict__,
            "payload_sha256": hashlib.sha256(b"different-body").hexdigest(),
        }
    )

    first = _process_event(db_session, tenant_id=tenant_id, event=first_event)
    with pytest.raises(payment_service.PaymentEventConflictError):
        _process_event(
            db_session,
            tenant_id=tenant_id,
            event=conflicting_event,
        )

    assert first.status == "processed"
    assert not first.duplicate


def test_out_of_order_failure_event_does_not_reverse_payment_success(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    success = _verified_event(
        event_id="out-of-order-success",
        payment_reference=reference,
        order_id=order_id,
        status="succeeded",
    )
    failure = _verified_event(
        event_id="out-of-order-failure",
        payment_reference=reference,
        order_id=order_id,
        status="failed",
    )

    _process_event(db_session, tenant_id=tenant_id, event=success)
    _process_event(db_session, tenant_id=tenant_id, event=failure)

    async def current_status():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            assert payment is not None
            return payment.status

    assert asyncio.run(current_status()) == "succeeded"


def test_refund_requests_are_idempotent_and_require_verified_success(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="refund-success-payment",
            payment_reference=reference,
            order_id=order_id,
        ),
    )

    async def create_refunds():
        async with db_session() as session:
            first, created = await payment_service.create_refund_request(
                session,
                tenant_id=tenant_id,
                payment_id=payment_id,
                idempotency_key="refund-key",
                amount_units=100_000,
                currency="pkr",
            )
            retry, retry_created = await payment_service.create_refund_request(
                session,
                tenant_id=tenant_id,
                payment_id=payment_id,
                idempotency_key="refund-key",
                amount_units=100_000,
                currency="PKR",
            )
            return first.id, retry.id, created, retry_created

    first_id, retry_id, created, retry_created = asyncio.run(create_refunds())
    assert (first_id, retry_id, created, retry_created) == (1, 1, True, False)

    async def conflicting_refund():
        async with db_session() as session:
            await payment_service.create_refund_request(
                session,
                tenant_id=tenant_id,
                payment_id=payment_id,
                idempotency_key="refund-key",
                amount_units=200_000,
                currency="PKR",
            )

    with pytest.raises(payment_service.PaymentIdempotencyConflictError):
        asyncio.run(conflicting_refund())

    async def record_verified_result():
        async with db_session() as session:
            return await payment_service.record_verified_refund_result(
                session,
                VerifiedRefundResult(
                    refund_id=first_id,
                    provider="test_gateway",
                    merchant_account_reference="tenant-test-account",
                    status="succeeded",
                    provider_reference="provider-refund-1",
                ),
                tenant_id=tenant_id,
            )

    refund = asyncio.run(record_verified_result())
    assert refund.status == "succeeded"


def test_failed_refund_reserves_amount_against_late_success(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    tenant_id = customer.tenant_id or ""
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, tenant_id)
    payment_id, payment_reference = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=tenant_id,
        order_id=order_id,
    )
    _process_event(
        db_session,
        tenant_id=tenant_id,
        event=_verified_event(
            event_id="payment-for-refund-race",
            payment_reference=payment_reference,
            order_id=order_id,
        ),
    )

    async def create_full_refund():
        async with db_session() as session:
            return await payment_service.create_refund_request(
                session,
                tenant_id=tenant_id,
                payment_id=payment_id,
                idempotency_key="first-full-refund",
                amount_units=199_900,
                currency="PKR",
            )

    refund, created = asyncio.run(create_full_refund())
    assert created

    async def record_failed_refund():
        async with db_session() as session:
            return await payment_service.record_verified_refund_result(
                session,
                VerifiedRefundResult(
                    refund_id=refund.id,
                    provider="test_gateway",
                    merchant_account_reference="tenant-test-account",
                    status="failed",
                    provider_reference="failed-refund-reference",
                ),
                tenant_id=tenant_id,
            )

    assert asyncio.run(record_failed_refund()).status == "failed"

    async def create_second_full_refund():
        async with db_session() as session:
            await payment_service.create_refund_request(
                session,
                tenant_id=tenant_id,
                payment_id=payment_id,
                idempotency_key="second-full-refund",
                amount_units=199_900,
                currency="PKR",
            )

    with pytest.raises(payment_service.PaymentAmountError):
        asyncio.run(create_second_full_refund())

    async def record_late_success():
        async with db_session() as session:
            return await payment_service.record_verified_refund_result(
                session,
                VerifiedRefundResult(
                    refund_id=refund.id,
                    provider="test_gateway",
                    merchant_account_reference="tenant-test-account",
                    status="succeeded",
                    provider_reference="succeeded-refund-reference",
                ),
                tenant_id=tenant_id,
            )

    assert asyncio.run(record_late_success()).status == "succeeded"


def test_payment_history_supports_stable_pagination(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, customer.tenant_id or "")
    _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )
    response = client.get(
        "/payments/?skip=0&limit=1",
        headers=auth_headers(customer),
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_order_currency_and_price_are_preserved_without_float_storage(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    db_session: async_sessionmaker[AsyncSession],
    auth_headers: Callable[[models.User], dict[str, str]],
) -> None:
    order_id = _create_order(client, auth_headers(customer), sample_product.id)
    _create_payment_account(db_session, customer.tenant_id or "")
    payment_id, _ = _create_payment(
        db_session,
        customer_id=customer.id,
        tenant_id=customer.tenant_id or "",
        order_id=order_id,
    )

    async def amount():
        async with db_session() as session:
            payment = await session.get(models.Payment, payment_id)
            assert payment is not None
            return payment.amount_units, payment.currency

    assert asyncio.run(amount()) == (199_900, "PKR")
    assert Decimal("19.99") * Decimal(10_000) == Decimal(199_900)
