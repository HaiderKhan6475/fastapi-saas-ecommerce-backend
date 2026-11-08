import hmac
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import models, schemas
from app.currencies import normalize_currency_code
from app.services import order_service
from app.services.payment_providers import VerifiedPaymentEvent, VerifiedRefundResult

PRICE_SCALE = Decimal(10_000)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class PaymentError(Exception):
    pass


class PaymentAuthenticationError(PaymentError):
    pass


class PaymentAuthorizationError(PaymentError):
    pass


class PaymentNotFoundError(PaymentError):
    pass


class PaymentAccountNotFoundError(PaymentError):
    pass


class PaymentConflictError(PaymentError):
    pass


class PaymentIdempotencyConflictError(PaymentConflictError):
    pass


class PaymentEventConflictError(PaymentConflictError):
    pass


class PaymentProviderReferenceConflictError(PaymentConflictError):
    pass


class PaymentAmountError(PaymentError):
    pass


class PaymentCurrencyError(PaymentError):
    pass


class PaymentStateConflictError(PaymentConflictError):
    pass


@dataclass(frozen=True)
class VerifiedEventResult:
    status: str
    duplicate: bool
    payment_id: int | None
    reconciliation_required: bool
    safe_error_code: str | None


def _session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    bind = db.bind
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("Payment operations require an async SQLite engine")
    return async_sessionmaker(
        bind=bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@asynccontextmanager
async def _write_transaction(db: AsyncSession) -> AsyncGenerator[None, None]:
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
        raise PaymentAuthenticationError
    if customer.role != "Customer" or customer.tenant_id != tenant_id:
        raise PaymentAuthorizationError


def _safe_metadata(values: dict[str, str] | None) -> dict[str, str]:
    if values is None:
        return {}
    allowed_keys = {"checkout_mode", "payment_method", "provider_environment"}
    if any(
        key not in allowed_keys or not isinstance(value, str) or len(value) > 100
        for key, value in values.items()
    ):
        raise ValueError("Payment metadata contains unsupported values")
    return dict(values)


def _amount_from_units(units: int) -> Decimal:
    return Decimal(units) / PRICE_SCALE


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def create_payment_attempt(
    db: AsyncSession,
    *,
    customer_id: int,
    tenant_id: str,
    order_id: int,
    merchant_account_reference: str,
    idempotency_key: str,
    safe_metadata: dict[str, str] | None = None,
) -> tuple[models.Payment, bool]:
    if not 1 <= len(idempotency_key) <= 255:
        raise PaymentIdempotencyConflictError
    metadata = _safe_metadata(safe_metadata)
    now = datetime.now(UTC)

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            await _validate_customer(session, customer_id, tenant_id)
            account = await session.scalar(
                select(models.TenantPaymentAccount).where(
                    models.TenantPaymentAccount.tenant_id == tenant_id,
                    models.TenantPaymentAccount.merchant_account_reference
                    == merchant_account_reference,
                )
            )
            if account is None:
                raise PaymentAccountNotFoundError
            order = await session.scalar(
                select(models.Order).where(
                    models.Order.id == order_id,
                    models.Order.tenant_id == tenant_id,
                    models.Order.customer_id == customer_id,
                )
            )
            if order is None:
                raise PaymentNotFoundError
            existing = await session.scalar(
                select(models.Payment).where(
                    models.Payment.tenant_id == tenant_id,
                    models.Payment.order_id == order_id,
                    models.Payment.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.merchant_account_id != account.id
                    or existing.provider != account.provider
                    or existing.amount_units != order.total_units
                    or existing.currency != order.currency
                ):
                    raise PaymentIdempotencyConflictError
                return existing, False
            if not account.is_active:
                raise PaymentAccountNotFoundError
            active_or_succeeded_attempt = await session.scalar(
                select(models.Payment.id)
                .where(
                    models.Payment.tenant_id == tenant_id,
                    models.Payment.order_id == order_id,
                    models.Payment.status.in_(("initiated", "pending", "succeeded")),
                )
                .limit(1)
            )
            if active_or_succeeded_attempt is not None:
                raise PaymentStateConflictError
            if order.status != "pending" or _utc_datetime(order.expires_at) <= now:
                raise PaymentStateConflictError
            if not isinstance(order.total_units, int) or order.total_units <= 0:
                raise PaymentAmountError
            try:
                currency = normalize_currency_code(order.currency)
            except ValueError as exc:
                raise PaymentCurrencyError from exc

            payment = models.Payment(
                tenant_id=tenant_id,
                order_id=order.id,
                merchant_account_id=account.id,
                provider=account.provider,
                idempotency_key=idempotency_key,
                amount_units=order.total_units,
                currency=currency,
                status="initiated",
                safe_metadata=metadata,
                created_at=now,
                updated_at=now,
            )
            session.add(payment)
            await session.flush()
            return payment, True


async def process_verified_payment_event(
    db: AsyncSession,
    *,
    tenant_id: str,
    merchant_account_reference: str,
    event: VerifiedPaymentEvent,
    now: datetime | None = None,
) -> VerifiedEventResult:
    timestamp = _utc_datetime(now) if now is not None else datetime.now(UTC)
    if (
        not isinstance(event.event_id, str)
        or not 1 <= len(event.event_id) <= 255
        or not isinstance(event.payment_reference, str)
        or not 1 <= len(event.payment_reference) <= 255
        or not isinstance(event.order_reference, str)
        or not 1 <= len(event.order_reference) <= 255
        or not isinstance(event.provider, str)
        or not 1 <= len(event.provider) <= 50
        or event.provider != event.provider.strip().lower()
        or not isinstance(event.status, str)
        or not isinstance(event.payload_sha256, str)
        or not _SHA256_RE.fullmatch(event.payload_sha256)
        or (
            event.provider_transaction_reference is not None
            and (
                not isinstance(event.provider_transaction_reference, str)
                or not 1 <= len(event.provider_transaction_reference) <= 255
            )
        )
    ):
        raise PaymentEventConflictError

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            account = await session.scalar(
                select(models.TenantPaymentAccount).where(
                    models.TenantPaymentAccount.tenant_id == tenant_id,
                    models.TenantPaymentAccount.merchant_account_reference
                    == merchant_account_reference,
                )
            )
            if account is None:
                raise PaymentAccountNotFoundError
            if event.provider != account.provider:
                raise PaymentEventConflictError
            existing_event = await session.scalar(
                select(models.PaymentWebhookEvent).where(
                    models.PaymentWebhookEvent.tenant_id == tenant_id,
                    models.PaymentWebhookEvent.merchant_account_id == account.id,
                    models.PaymentWebhookEvent.provider == account.provider,
                    models.PaymentWebhookEvent.event_id == event.event_id,
                )
            )
            if existing_event is not None:
                if not hmac.compare_digest(
                    existing_event.payload_sha256,
                    event.payload_sha256,
                ):
                    raise PaymentEventConflictError
                return VerifiedEventResult(
                    status=existing_event.status,
                    duplicate=True,
                    payment_id=existing_event.payment_id,
                    reconciliation_required=(
                        existing_event.status == "reconciliation_required"
                    ),
                    safe_error_code=existing_event.safe_error_code,
                )

            payment = await session.scalar(
                select(models.Payment).where(
                    models.Payment.tenant_id == tenant_id,
                    models.Payment.merchant_account_id == account.id,
                    models.Payment.provider == account.provider,
                    models.Payment.internal_reference == event.payment_reference,
                )
            )
            if payment is None:
                error_code = "payment_not_found"
                payment_id = None
                order = None
            else:
                payment_id = payment.id
                order = await session.scalar(
                    select(models.Order).where(
                        models.Order.tenant_id == tenant_id,
                        models.Order.id == payment.order_id,
                    )
                )
                error_code = ""
                if order is None or event.order_reference != str(payment.order_id):
                    error_code = "order_mismatch"
                elif (
                    isinstance(event.amount_units, bool)
                    or not isinstance(event.amount_units, int)
                    or event.amount_units <= 0
                    or event.amount_units > models.MAX_PRODUCT_PRICE_UNITS
                ):
                    error_code = "invalid_amount"
                else:
                    try:
                        event_currency = normalize_currency_code(event.currency)
                    except (AttributeError, ValueError):
                        error_code = "invalid_currency"
                    else:
                        if event.amount_units != payment.amount_units:
                            error_code = "amount_mismatch"
                        elif event_currency != payment.currency:
                            error_code = "currency_mismatch"
                        elif (
                            order.total_units != payment.amount_units
                            or order.currency != payment.currency
                        ):
                            error_code = "order_amount_mismatch"
                        elif event.status not in {
                            "pending",
                            "succeeded",
                            "failed",
                            "expired",
                        }:
                            error_code = "provider_status_invalid"

            event_row = models.PaymentWebhookEvent(
                tenant_id=tenant_id,
                merchant_account_id=account.id,
                provider=account.provider,
                event_id=event.event_id,
                payment_id=payment_id,
                order_reference=event.order_reference,
                payload_sha256=event.payload_sha256,
                status="rejected" if error_code else "processed",
                safe_error_code=error_code or None,
                received_at=timestamp,
                processed_at=timestamp,
            )
            session.add(event_row)
            if error_code:
                await session.flush()
                return VerifiedEventResult(
                    status="rejected",
                    duplicate=False,
                    payment_id=payment_id,
                    reconciliation_required=False,
                    safe_error_code=error_code,
                )

            assert payment is not None and order is not None
            reconciliation_required = False
            payment.updated_at = timestamp
            if event.status == "succeeded":
                payment.status = "succeeded"
                expired = _utc_datetime(order.expires_at) <= timestamp
                if order.status == "pending" and expired:
                    await order_service._cancel_pending_order(
                        session,
                        order,
                        now=timestamp,
                        reason="expiration",
                        require_expired=True,
                    )
                    reconciliation_required = True
                elif order.status != "pending":
                    reconciliation_required = True
                successful_sibling_ids = list(
                    await session.scalars(
                        select(models.Payment.id).where(
                            models.Payment.tenant_id == tenant_id,
                            models.Payment.order_id == order.id,
                            models.Payment.id != payment.id,
                            models.Payment.status == "succeeded",
                        )
                    )
                )
                if successful_sibling_ids:
                    reconciliation_required = True
                    await session.execute(
                        update(models.Payment)
                        .where(
                            models.Payment.tenant_id == tenant_id,
                            models.Payment.id.in_(successful_sibling_ids),
                        )
                        .values(reconciliation_required=True)
                        .execution_options(synchronize_session=False)
                    )
                if reconciliation_required:
                    payment.reconciliation_required = True
                    event_row.status = "reconciliation_required"
            elif event.status == "pending":
                if payment.status == "initiated":
                    payment.status = "pending"
            elif event.status == "failed":
                if payment.status not in {"succeeded", "expired"}:
                    payment.status = "failed"
            elif event.status == "expired":
                if payment.status in {"initiated", "pending"}:
                    payment.status = "expired"
            if event.provider_transaction_reference:
                payment.provider_transaction_reference = (
                    event.provider_transaction_reference
                )
            await session.flush()
            return VerifiedEventResult(
                status=event_row.status,
                duplicate=False,
                payment_id=payment.id,
                reconciliation_required=reconciliation_required,
                safe_error_code=event_row.safe_error_code,
            )


async def create_refund_request(
    db: AsyncSession,
    *,
    tenant_id: str,
    payment_id: int,
    idempotency_key: str,
    amount_units: int,
    currency: str,
) -> tuple[models.Refund, bool]:
    if not 1 <= len(idempotency_key) <= 255:
        raise PaymentIdempotencyConflictError
    if isinstance(amount_units, bool) or not isinstance(amount_units, int):
        raise PaymentAmountError
    if not 0 < amount_units <= models.MAX_PRODUCT_PRICE_UNITS:
        raise PaymentAmountError
    try:
        normalized_currency = normalize_currency_code(currency)
    except (AttributeError, ValueError) as exc:
        raise PaymentCurrencyError from exc

    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            payment = await session.scalar(
                select(models.Payment).where(
                    models.Payment.id == payment_id,
                    models.Payment.tenant_id == tenant_id,
                )
            )
            if payment is None:
                raise PaymentNotFoundError
            existing = await session.scalar(
                select(models.Refund).where(
                    models.Refund.tenant_id == tenant_id,
                    models.Refund.payment_id == payment_id,
                    models.Refund.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.amount_units != amount_units
                    or existing.currency != normalized_currency
                ):
                    raise PaymentIdempotencyConflictError
                return existing, False
            if payment.status != "succeeded":
                raise PaymentStateConflictError
            if normalized_currency != payment.currency:
                raise PaymentCurrencyError
            reserved_refund_amounts = await session.scalars(
                select(models.Refund.amount_units).where(
                    models.Refund.tenant_id == tenant_id,
                    models.Refund.payment_id == payment_id,
                )
            )
            already_refunded = sum(reserved_refund_amounts)
            if amount_units > payment.amount_units - already_refunded:
                raise PaymentAmountError
            refund = models.Refund(
                tenant_id=tenant_id,
                payment_id=payment.id,
                merchant_account_id=payment.merchant_account_id,
                provider=payment.provider,
                amount_units=amount_units,
                currency=payment.currency,
                idempotency_key=idempotency_key,
            )
            session.add(refund)
            await session.flush()
            return refund, True


async def record_verified_refund_result(
    db: AsyncSession,
    result: VerifiedRefundResult,
    *,
    tenant_id: str,
) -> models.Refund:
    if result.status not in {"pending", "succeeded", "failed"}:
        raise PaymentStateConflictError
    async with _session_factory(db)() as session:
        async with _write_transaction(session):
            account = await session.scalar(
                select(models.TenantPaymentAccount).where(
                    models.TenantPaymentAccount.tenant_id == tenant_id,
                    models.TenantPaymentAccount.provider == result.provider,
                    models.TenantPaymentAccount.merchant_account_reference
                    == result.merchant_account_reference,
                )
            )
            if account is None:
                raise PaymentAccountNotFoundError
            refund = await session.scalar(
                select(models.Refund).where(
                    models.Refund.tenant_id == tenant_id,
                    models.Refund.id == result.refund_id,
                    models.Refund.merchant_account_id == account.id,
                    models.Refund.provider == account.provider,
                )
            )
            if refund is None:
                raise PaymentNotFoundError
            if result.provider_reference is not None and not (
                1 <= len(result.provider_reference) <= 255
            ):
                raise PaymentProviderReferenceConflictError
            if refund.status == "succeeded":
                return refund
            if result.status == "succeeded":
                refund_amounts = await session.scalars(
                    select(models.Refund.amount_units).where(
                        models.Refund.tenant_id == tenant_id,
                        models.Refund.payment_id == refund.payment_id,
                    )
                )
                payment_amount = await session.scalar(
                    select(models.Payment.amount_units).where(
                        models.Payment.tenant_id == tenant_id,
                        models.Payment.id == refund.payment_id,
                    )
                )
                if payment_amount is None or sum(refund_amounts) > payment_amount:
                    raise PaymentAmountError
            elif refund.status == "failed":
                return refund
            refund.status = result.status
            if result.provider_reference is not None:
                refund.provider_reference = result.provider_reference
            refund.updated_at = datetime.now(UTC)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise PaymentProviderReferenceConflictError from exc
            return refund


def _payment_summary(payment: models.Payment) -> schemas.PaymentSummary:
    return schemas.PaymentSummary(
        id=payment.id,
        order_id=payment.order_id,
        status=payment.status,
        amount=_amount_from_units(payment.amount_units),
        currency=payment.currency,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
    )


async def get_payment_history(
    db: AsyncSession,
    *,
    customer_id: int,
    tenant_id: str,
    skip: int,
    limit: int,
) -> list[schemas.PaymentSummary]:
    payments = await db.scalars(
        select(models.Payment)
        .join(
            models.Order,
            (models.Order.id == models.Payment.order_id)
            & (models.Order.tenant_id == models.Payment.tenant_id),
        )
        .where(
            models.Payment.tenant_id == tenant_id,
            models.Order.customer_id == customer_id,
        )
        .order_by(models.Payment.created_at.desc(), models.Payment.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return [_payment_summary(payment) for payment in payments]


async def get_payment(
    db: AsyncSession,
    *,
    payment_id: int,
    customer_id: int,
    tenant_id: str,
) -> schemas.PaymentSummary:
    payment = await db.scalar(
        select(models.Payment)
        .join(
            models.Order,
            (models.Order.id == models.Payment.order_id)
            & (models.Order.tenant_id == models.Payment.tenant_id),
        )
        .where(
            models.Payment.id == payment_id,
            models.Payment.tenant_id == tenant_id,
            models.Order.customer_id == customer_id,
        )
    )
    if payment is None:
        raise PaymentNotFoundError
    return _payment_summary(payment)
