from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.currencies import SUPPORTED_CURRENCY_CODES
from app.database import Base

USER_ROLES = ("SuperAdmin", "TenantAdmin", "Customer")
UserRole = Literal["SuperAdmin", "TenantAdmin", "Customer"]
MAX_PRODUCT_PRICE_UNITS = 999_999_999_999_999
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
MAX_CART_QUANTITY = 1000
_CURRENCY_VALUES_SQL = ", ".join(
    f"'{code}'" for code in sorted(SUPPORTED_CURRENCY_CODES)
)
_CURRENCY_CHECK_SQL = f"currency IN ({_CURRENCY_VALUES_SQL})"


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "length(default_currency) = 3 "
            "AND default_currency = upper(default_currency)",
            name="ck_tenants_default_currency_format",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_currency: Mapped[str] = mapped_column(
        String(3),
        default="PKR",
        server_default=text("'PKR'"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    users: Mapped[list["User"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    products: Mapped[list["Product"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_tenant_id_id", "tenant_id", "id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        index=True,
        nullable=False,
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )

    tenant: Mapped[Tenant | None] = relationship(back_populates="users")


class AuthRateLimitBucket(Base):
    __tablename__ = "auth_rate_limit_buckets"
    __table_args__ = (
        CheckConstraint("attempts > 0", name="ck_auth_rate_limit_attempts_positive"),
        Index("ix_auth_rate_limit_window_start", "window_start"),
    )

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0", name="ck_products_price_positive"),
        CheckConstraint("stock >= 0", name="ck_products_stock_nonnegative"),
        CheckConstraint("version > 0", name="ck_products_version_positive"),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_products_currency_format",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 255",
            name="ck_products_name_length",
        ),
        CheckConstraint(
            "description IS NULL OR length(description) <= 5000",
            name="ck_products_description_length",
        ),
        Index("ix_products_tenant_id_id", "tenant_id", "id", unique=True),
        Index(
            "ix_products_tenant_currency_id",
            "tenant_id",
            "currency",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        default="PKR",
        server_default=text("'PKR'"),
        nullable=False,
    )
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )

    tenant: Mapped[Tenant] = relationship(back_populates="products")


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "customer_id",
            name="uq_carts_tenant_customer",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_carts_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["users.tenant_id", "users.id"],
            ondelete="CASCADE",
            name="fk_carts_customer_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_carts_tenant",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys=lambda: [CartItem.tenant_id, CartItem.cart_id],
    )


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_CART_QUANTITY}",
            name="ck_cart_items_quantity_range",
        ),
        UniqueConstraint(
            "tenant_id",
            "cart_id",
            "product_id",
            name="uq_cart_items_tenant_cart_product",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cart_id"],
            ["carts.tenant_id", "carts.id"],
            ondelete="CASCADE",
            name="fk_cart_items_cart_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.id"],
            ondelete="CASCADE",
            name="fk_cart_items_product_tenant",
        ),
        Index("ix_cart_items_tenant_product", "tenant_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    cart_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    cart: Mapped[Cart] = relationship(
        back_populates="items",
        foreign_keys=lambda: [CartItem.tenant_id, CartItem.cart_id],
    )
    product: Mapped[Product] = relationship(
        foreign_keys=lambda: [CartItem.tenant_id, CartItem.product_id],
        overlaps="cart,items",
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'processing', 'completed', "
            "'cancelled')",
            name="ck_orders_status",
        ),
        CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_orders_currency_supported"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_orders_idempotency_key_length",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64 "
            "AND request_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="ck_orders_request_fingerprint_sha256",
        ),
        CheckConstraint(
            f"subtotal_units BETWEEN 0 AND {MAX_PRODUCT_PRICE_UNITS} "
            f"AND total_units BETWEEN 0 AND {MAX_PRODUCT_PRICE_UNITS}",
            name="ck_orders_amount_range",
        ),
        CheckConstraint(
            "total_units = subtotal_units",
            name="ck_orders_total_equals_subtotal",
        ),
        CheckConstraint(
            "cancellation_reason IS NULL OR cancellation_reason IN "
            "('customer', 'administrator', 'expiration')",
            name="ck_orders_cancellation_reason",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancellation_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancellation_reason IS NULL)",
            name="ck_orders_cancellation_metadata",
        ),
        UniqueConstraint(
            "tenant_id",
            "customer_id",
            "idempotency_key",
            name="uq_orders_tenant_customer_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "currency",
            name="uq_orders_tenant_id_currency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["users.tenant_id", "users.id"],
            ondelete="RESTRICT",
            name="fk_orders_customer_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_orders_tenant",
        ),
        Index(
            "ix_orders_tenant_customer_created",
            "tenant_id",
            "customer_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_orders_tenant_created",
            "tenant_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_orders_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal_units: Mapped[int] = mapped_column(Integer, nullable=False)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys=lambda: [
            OrderItem.tenant_id,
            OrderItem.order_id,
            OrderItem.currency,
        ],
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint(
            "length(product_name) BETWEEN 1 AND 255",
            name="ck_order_items_product_name_length",
        ),
        CheckConstraint("line_number > 0", name="ck_order_items_line_number_positive"),
        CheckConstraint(
            f"unit_price_units BETWEEN 1 AND {MAX_PRODUCT_PRICE_UNITS}",
            name="ck_order_items_unit_price_range",
        ),
        CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_CART_QUANTITY}",
            name="ck_order_items_quantity_range",
        ),
        CheckConstraint(
            f"line_total_units BETWEEN 1 AND {MAX_SQLITE_INTEGER} "
            "AND line_total_units = unit_price_units * quantity",
            name="ck_order_items_line_total",
        ),
        CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_order_items_currency_supported"),
        CheckConstraint(
            "(source_tenant_id IS NULL AND source_product_id IS NULL) OR "
            "(source_tenant_id IS NOT NULL AND source_product_id IS NOT NULL)",
            name="ck_order_items_source_product_pair",
        ),
        UniqueConstraint(
            "tenant_id",
            "order_id",
            "line_number",
            name="uq_order_items_tenant_order_line",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "order_id", "currency"],
            ["orders.tenant_id", "orders.id", "orders.currency"],
            ondelete="CASCADE",
            name="fk_order_items_order_tenant_currency",
        ),
        ForeignKeyConstraint(
            ["source_tenant_id", "source_product_id"],
            ["products.tenant_id", "products.id"],
            ondelete="SET NULL",
            name="fk_order_items_source_product",
        ),
        Index(
            "ix_order_items_source_product",
            "source_tenant_id",
            "source_product_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_price_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_units: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    order: Mapped[Order] = relationship(
        back_populates="items",
        foreign_keys=lambda: [
            OrderItem.tenant_id,
            OrderItem.order_id,
            OrderItem.currency,
        ],
    )


event.listen(
    OrderItem.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_order_items_immutable_snapshots
        BEFORE UPDATE ON order_items
        FOR EACH ROW
        WHEN NEW.tenant_id IS NOT OLD.tenant_id
          OR NEW.order_id IS NOT OLD.order_id
          OR NEW.line_number IS NOT OLD.line_number
          OR NEW.product_name IS NOT OLD.product_name
          OR NEW.unit_price_units IS NOT OLD.unit_price_units
          OR NEW.currency IS NOT OLD.currency
          OR NEW.quantity IS NOT OLD.quantity
          OR NEW.line_total_units IS NOT OLD.line_total_units
          OR NEW.created_at IS NOT OLD.created_at
          OR (
            (NEW.source_tenant_id IS NOT OLD.source_tenant_id
             OR NEW.source_product_id IS NOT OLD.source_product_id)
            AND NOT (
              NEW.source_tenant_id IS NULL
              AND NEW.source_product_id IS NULL
              AND OLD.source_tenant_id IS NOT NULL
              AND OLD.source_product_id IS NOT NULL
            )
          )
        BEGIN
            SELECT RAISE(ABORT, 'order item snapshots are immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)


class TenantPaymentAccount(Base):
    __tablename__ = "tenant_payment_accounts"
    __table_args__ = (
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 50 "
            "AND provider = lower(provider) "
            "AND provider NOT GLOB '*[^a-z0-9_-]*'",
            name="ck_tenant_payment_accounts_provider_format",
        ),
        CheckConstraint(
            "length(merchant_account_reference) BETWEEN 1 AND 255",
            name="ck_tenant_payment_accounts_reference_length",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_tenant_payment_accounts_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "provider",
            name="uq_tenant_payment_accounts_tenant_id_provider",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "merchant_account_reference",
            name="uq_tenant_payment_accounts_tenant_provider_reference",
        ),
        Index(
            "ix_tenant_payment_accounts_tenant_active",
            "tenant_id",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    merchant_account_reference: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 50 "
            "AND provider = lower(provider) "
            "AND provider NOT GLOB '*[^a-z0-9_-]*'",
            name="ck_payments_provider_format",
        ),
        CheckConstraint(
            "status IN ('initiated', 'pending', 'succeeded', 'failed', 'expired')",
            name="ck_payments_status",
        ),
        CheckConstraint(
            f"amount_units BETWEEN 1 AND {MAX_PRODUCT_PRICE_UNITS}",
            name="ck_payments_amount_range",
        ),
        CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_payments_currency_supported"),
        CheckConstraint(
            "length(internal_reference) BETWEEN 1 AND 255",
            name="ck_payments_internal_reference_length",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_payments_idempotency_key_length",
        ),
        CheckConstraint(
            "provider_transaction_reference IS NULL OR "
            "length(provider_transaction_reference) BETWEEN 1 AND 255",
            name="ck_payments_provider_reference_length",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "order_id", "currency"],
            ["orders.tenant_id", "orders.id", "orders.currency"],
            ondelete="RESTRICT",
            name="fk_payments_order_tenant_currency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "merchant_account_id", "provider"],
            [
                "tenant_payment_accounts.tenant_id",
                "tenant_payment_accounts.id",
                "tenant_payment_accounts.provider",
            ],
            ondelete="RESTRICT",
            name="fk_payments_tenant_merchant_account",
        ),
        UniqueConstraint(
            "internal_reference",
            name="uq_payments_internal_reference",
        ),
        UniqueConstraint(
            "tenant_id",
            "order_id",
            "idempotency_key",
            name="uq_payments_tenant_order_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "merchant_account_id",
            "provider",
            "provider_transaction_reference",
            name="uq_payments_tenant_provider_transaction_reference",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "merchant_account_id",
            "provider",
            name="uq_payments_tenant_id_account_provider",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "merchant_account_id",
            "provider",
            "currency",
            name="uq_payments_tenant_id_account_provider_currency",
        ),
        Index(
            "ix_payments_tenant_order_created",
            "tenant_id",
            "order_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_payments_tenant_status_updated",
            "tenant_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    merchant_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    internal_reference: Mapped[str] = mapped_column(
        String(255),
        default=lambda: str(uuid4()),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default="initiated",
        server_default=text("'initiated'"),
        nullable=False,
    )
    provider_transaction_reference: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
    )
    safe_metadata: Mapped[dict[str, str]] = mapped_column(
        JSON,
        default=dict,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processed', 'rejected', 'reconciliation_required')",
            name="ck_payment_webhook_events_status",
        ),
        CheckConstraint(
            "length(event_id) BETWEEN 1 AND 255",
            name="ck_payment_webhook_events_event_id_length",
        ),
        CheckConstraint(
            "length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_payment_webhook_events_payload_sha256",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('payment_not_found', 'order_mismatch', 'invalid_amount', "
            "'invalid_currency', 'amount_mismatch', 'currency_mismatch', "
            "'order_amount_mismatch')",
            name="ck_payment_webhook_events_safe_error_code",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "merchant_account_id", "provider"],
            [
                "tenant_payment_accounts.tenant_id",
                "tenant_payment_accounts.id",
                "tenant_payment_accounts.provider",
            ],
            ondelete="RESTRICT",
            name="fk_payment_webhook_events_merchant_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "payment_id", "merchant_account_id", "provider"],
            [
                "payments.tenant_id",
                "payments.id",
                "payments.merchant_account_id",
                "payments.provider",
            ],
            ondelete="RESTRICT",
            name="fk_payment_webhook_events_payment",
        ),
        UniqueConstraint(
            "tenant_id",
            "merchant_account_id",
            "provider",
            "event_id",
            name="uq_payment_webhook_events_tenant_provider_event",
        ),
        Index(
            "ix_payment_webhook_events_tenant_status_received",
            "tenant_id",
            "status",
            "received_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    merchant_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint(
            "status IN ('initiated', 'pending', 'succeeded', 'failed')",
            name="ck_refunds_status",
        ),
        CheckConstraint(
            f"amount_units BETWEEN 1 AND {MAX_PRODUCT_PRICE_UNITS}",
            name="ck_refunds_amount_range",
        ),
        CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_refunds_currency_supported"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_refunds_idempotency_key_length",
        ),
        CheckConstraint(
            "provider_reference IS NULL OR "
            "length(provider_reference) BETWEEN 1 AND 255",
            name="ck_refunds_provider_reference_length",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "payment_id",
                "merchant_account_id",
                "provider",
                "currency",
            ],
            [
                "payments.tenant_id",
                "payments.id",
                "payments.merchant_account_id",
                "payments.provider",
                "payments.currency",
            ],
            ondelete="RESTRICT",
            name="fk_refunds_payment_tenant_currency",
        ),
        UniqueConstraint(
            "tenant_id",
            "payment_id",
            "idempotency_key",
            name="uq_refunds_tenant_payment_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "merchant_account_id",
            "provider",
            "provider_reference",
            name="uq_refunds_tenant_provider_reference",
        ),
        Index(
            "ix_refunds_tenant_payment_created",
            "tenant_id",
            "payment_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    merchant_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    amount_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default="initiated",
        server_default=text("'initiated'"),
        nullable=False,
    )
    provider_reference: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
