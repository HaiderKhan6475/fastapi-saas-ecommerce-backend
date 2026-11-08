from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import SecretStr

PaymentProviderStatus = Literal["pending", "succeeded", "failed", "expired"]
RefundProviderStatus = Literal["pending", "succeeded", "failed"]


@dataclass(frozen=True)
class CheckoutRequest:
    payment_reference: str
    order_reference: str
    amount_units: int
    currency: str
    expires_at: datetime


@dataclass(frozen=True)
class CheckoutSession:
    checkout_url: str
    provider_transaction_reference: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class VerifiedPaymentEvent:
    provider: str
    event_id: str
    payment_reference: str
    order_reference: str
    amount_units: int
    currency: str
    status: PaymentProviderStatus
    payload_sha256: str
    provider_transaction_reference: str | None = None


@dataclass(frozen=True)
class RefundRequest:
    refund_id: int
    payment_reference: str
    amount_units: int
    currency: str


@dataclass(frozen=True)
class VerifiedRefundResult:
    refund_id: int
    provider: str
    merchant_account_reference: str
    status: RefundProviderStatus
    provider_reference: str | None


class CredentialResolver(Protocol):
    async def resolve(
        self,
        *,
        tenant_id: str,
        provider: str,
        merchant_account_reference: str,
    ) -> Mapping[str, SecretStr]: ...


class PaymentProvider(Protocol):
    async def create_checkout(
        self,
        request: CheckoutRequest,
        credentials: Mapping[str, SecretStr],
    ) -> CheckoutSession: ...

    async def verify_payment(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        credentials: Mapping[str, SecretStr],
    ) -> VerifiedPaymentEvent: ...

    async def create_refund(
        self,
        request: RefundRequest,
        credentials: Mapping[str, SecretStr],
    ) -> VerifiedRefundResult: ...
