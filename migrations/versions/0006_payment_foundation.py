"""Add tenant-scoped provider-agnostic payment records.

Revision ID: 0006_payment_foundation
Revises: 0005_commerce_schema
"""

import sqlalchemy as sa
from alembic import op

from app.currencies import SUPPORTED_CURRENCY_CODES

revision = "0006_payment_foundation"
down_revision = "0005_commerce_schema"
branch_labels = None
depends_on = None

_CURRENCY_CHECK_SQL = (
    "currency IN ("
    + ", ".join(f"'{code}'" for code in sorted(SUPPORTED_CURRENCY_CODES))
    + ")"
)
_PAYMENT_TABLES = (
    "refunds",
    "payment_webhook_events",
    "payments",
    "tenant_payment_accounts",
)


def upgrade() -> None:
    op.create_table(
        "tenant_payment_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("merchant_account_reference", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 50 "
            "AND provider = lower(provider) "
            "AND provider NOT GLOB '*[^a-z0-9_-]*'",
            name="ck_tenant_payment_accounts_provider_format",
        ),
        sa.CheckConstraint(
            "length(merchant_account_reference) BETWEEN 1 AND 255",
            name="ck_tenant_payment_accounts_reference_length",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_tenant_payment_accounts_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "provider",
            name="uq_tenant_payment_accounts_tenant_id_provider",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "merchant_account_reference",
            name="uq_tenant_payment_accounts_tenant_provider_reference",
        ),
    )
    op.create_index(
        "ix_tenant_payment_accounts_tenant_active",
        "tenant_payment_accounts",
        ["tenant_id", "is_active"],
        unique=False,
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("merchant_account_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("internal_reference", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("amount_units", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'initiated'"),
            nullable=False,
        ),
        sa.Column(
            "provider_transaction_reference",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 50 "
            "AND provider = lower(provider) "
            "AND provider NOT GLOB '*[^a-z0-9_-]*'",
            name="ck_payments_provider_format",
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'pending', 'succeeded', 'failed', 'expired')",
            name="ck_payments_status",
        ),
        sa.CheckConstraint(
            "amount_units BETWEEN 1 AND 999999999999999",
            name="ck_payments_amount_range",
        ),
        sa.CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_payments_currency_supported"),
        sa.CheckConstraint(
            "length(internal_reference) BETWEEN 1 AND 255",
            name="ck_payments_internal_reference_length",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_payments_idempotency_key_length",
        ),
        sa.CheckConstraint(
            "provider_transaction_reference IS NULL OR "
            "length(provider_transaction_reference) BETWEEN 1 AND 255",
            name="ck_payments_provider_reference_length",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id", "currency"],
            ["orders.tenant_id", "orders.id", "orders.currency"],
            ondelete="RESTRICT",
            name="fk_payments_order_tenant_currency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_account_id", "provider"],
            [
                "tenant_payment_accounts.tenant_id",
                "tenant_payment_accounts.id",
                "tenant_payment_accounts.provider",
            ],
            ondelete="RESTRICT",
            name="fk_payments_tenant_merchant_account",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "internal_reference",
            name="uq_payments_internal_reference",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "order_id",
            "idempotency_key",
            name="uq_payments_tenant_order_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_account_id",
            "provider",
            "provider_transaction_reference",
            name="uq_payments_tenant_provider_transaction_reference",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "merchant_account_id",
            "provider",
            name="uq_payments_tenant_id_account_provider",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "merchant_account_id",
            "provider",
            "currency",
            name="uq_payments_tenant_id_account_provider_currency",
        ),
    )
    op.create_index(
        "ix_payments_tenant_order_created",
        "payments",
        ["tenant_id", "order_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_payments_tenant_status_updated",
        "payments",
        ["tenant_id", "status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("merchant_account_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("order_reference", sa.String(length=255), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("safe_error_code", sa.String(length=40), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processed', 'rejected', 'reconciliation_required')",
            name="ck_payment_webhook_events_status",
        ),
        sa.CheckConstraint(
            "length(event_id) BETWEEN 1 AND 255",
            name="ck_payment_webhook_events_event_id_length",
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_payment_webhook_events_payload_sha256",
        ),
        sa.CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('payment_not_found', 'order_mismatch', 'invalid_amount', "
            "'invalid_currency', 'amount_mismatch', 'currency_mismatch', "
            "'order_amount_mismatch', 'provider_status_invalid')",
            name="ck_payment_webhook_events_safe_error_code",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_account_id", "provider"],
            [
                "tenant_payment_accounts.tenant_id",
                "tenant_payment_accounts.id",
                "tenant_payment_accounts.provider",
            ],
            ondelete="RESTRICT",
            name="fk_payment_webhook_events_merchant_account",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_account_id",
            "provider",
            "event_id",
            name="uq_payment_webhook_events_tenant_provider_event",
        ),
    )
    op.create_index(
        "ix_payment_webhook_events_tenant_status_received",
        "payment_webhook_events",
        ["tenant_id", "status", "received_at"],
        unique=False,
    )

    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("merchant_account_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("amount_units", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'initiated'"),
            nullable=False,
        ),
        sa.Column("provider_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'pending', 'succeeded', 'failed')",
            name="ck_refunds_status",
        ),
        sa.CheckConstraint(
            "amount_units BETWEEN 1 AND 999999999999999",
            name="ck_refunds_amount_range",
        ),
        sa.CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_refunds_currency_supported"),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_refunds_idempotency_key_length",
        ),
        sa.CheckConstraint(
            "provider_reference IS NULL OR "
            "length(provider_reference) BETWEEN 1 AND 255",
            name="ck_refunds_provider_reference_length",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "payment_id",
            "idempotency_key",
            name="uq_refunds_tenant_payment_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_account_id",
            "provider",
            "provider_reference",
            name="uq_refunds_tenant_provider_reference",
        ),
    )
    op.create_index(
        "ix_refunds_tenant_payment_created",
        "refunds",
        ["tenant_id", "payment_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    populated_tables = [
        table
        for table in _PAYMENT_TABLES
        if connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")
        ).scalar_one()
    ]
    if populated_tables:
        raise RuntimeError("Cannot downgrade payment schema while payment data exists")

    op.drop_index("ix_refunds_tenant_payment_created", table_name="refunds")
    op.drop_table("refunds")
    op.drop_index(
        "ix_payment_webhook_events_tenant_status_received",
        table_name="payment_webhook_events",
    )
    op.drop_table("payment_webhook_events")
    op.drop_index("ix_payments_tenant_status_updated", table_name="payments")
    op.drop_index("ix_payments_tenant_order_created", table_name="payments")
    op.drop_table("payments")
    op.drop_index(
        "ix_tenant_payment_accounts_tenant_active",
        table_name="tenant_payment_accounts",
    )
    op.drop_table("tenant_payment_accounts")
