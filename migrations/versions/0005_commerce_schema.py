"""Add tenant-scoped commerce tables.

Revision ID: 0005_commerce_schema
Revises: 0004_product_currency_decimal
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_commerce_schema"
down_revision: Union[str, None] = "0004_product_currency_decimal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SUPPORTED_CURRENCY_CODES = """
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND
BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU
CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP
GMD GNF GTQ GYD HKD
HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD
KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR
MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG
QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC
SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW
UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD
XPF XPT XSU XTS XUA YER ZAR ZMW ZWG
""".split()
_CURRENCY_CHECK_SQL = (
    "currency IN ("
    + ", ".join(f"'{code}'" for code in sorted(_SUPPORTED_CURRENCY_CODES))
    + ")"
)
_ORDER_ITEM_IMMUTABILITY_TRIGGER = """
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
_COMMERCE_TABLES = ("order_items", "orders", "cart_items", "carts")


def upgrade() -> None:
    op.drop_index("ix_products_tenant_id_id", table_name="products")
    op.create_index(
        "ix_products_tenant_id_id",
        "products",
        ["tenant_id", "id"],
        unique=True,
    )
    op.create_index(
        "uq_users_tenant_id_id",
        "users",
        ["tenant_id", "id"],
        unique=True,
    )

    op.create_table(
        "carts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_carts_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["users.tenant_id", "users.id"],
            name="fk_carts_customer_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "customer_id",
            name="uq_carts_tenant_customer",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_carts_tenant_id"),
    )
    op.create_table(
        "cart_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("cart_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
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
            "quantity BETWEEN 1 AND 1000",
            name="ck_cart_items_quantity_range",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cart_id"],
            ["carts.tenant_id", "carts.id"],
            name="fk_cart_items_cart_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.id"],
            name="fk_cart_items_product_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "cart_id",
            "product_id",
            name="uq_cart_items_tenant_cart_product",
        ),
    )
    op.create_index(
        "ix_cart_items_tenant_product",
        "cart_items",
        ["tenant_id", "product_id"],
        unique=False,
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal_units", sa.Integer(), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=32), nullable=True),
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
            "status IN ('pending', 'confirmed', 'processing', 'completed', "
            "'cancelled')",
            name="ck_orders_status",
        ),
        sa.CheckConstraint(_CURRENCY_CHECK_SQL, name="ck_orders_currency_supported"),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_orders_idempotency_key_length",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64 "
            "AND request_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="ck_orders_request_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "subtotal_units BETWEEN 0 AND 999999999999999 "
            "AND total_units BETWEEN 0 AND 999999999999999",
            name="ck_orders_amount_range",
        ),
        sa.CheckConstraint(
            "total_units = subtotal_units",
            name="ck_orders_total_equals_subtotal",
        ),
        sa.CheckConstraint(
            "cancellation_reason IS NULL OR cancellation_reason IN "
            "('customer', 'administrator', 'expiration')",
            name="ck_orders_cancellation_reason",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancellation_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancellation_reason IS NULL)",
            name="ck_orders_cancellation_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_orders_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["users.tenant_id", "users.id"],
            name="fk_orders_customer_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "customer_id",
            "idempotency_key",
            name="uq_orders_tenant_customer_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "currency",
            name="uq_orders_tenant_id_currency",
        ),
    )
    op.create_index(
        "ix_orders_tenant_customer_created",
        "orders",
        ["tenant_id", "customer_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_orders_tenant_created",
        "orders",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_orders_tenant_status_created",
        "orders",
        ["tenant_id", "status", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("source_tenant_id", sa.String(length=36), nullable=True),
        sa.Column("source_product_id", sa.Integer(), nullable=True),
        sa.Column("product_name", sa.String(length=255), nullable=False),
        sa.Column("unit_price_units", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total_units", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(product_name) BETWEEN 1 AND 255",
            name="ck_order_items_product_name_length",
        ),
        sa.CheckConstraint(
            "line_number > 0",
            name="ck_order_items_line_number_positive",
        ),
        sa.CheckConstraint(
            "unit_price_units BETWEEN 1 AND 999999999999999",
            name="ck_order_items_unit_price_range",
        ),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 1000",
            name="ck_order_items_quantity_range",
        ),
        sa.CheckConstraint(
            "line_total_units BETWEEN 1 AND 9223372036854775807 "
            "AND line_total_units = unit_price_units * quantity",
            name="ck_order_items_line_total",
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK_SQL,
            name="ck_order_items_currency_supported",
        ),
        sa.CheckConstraint(
            "(source_tenant_id IS NULL AND source_product_id IS NULL) OR "
            "(source_tenant_id IS NOT NULL AND source_product_id IS NOT NULL)",
            name="ck_order_items_source_product_pair",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id", "currency"],
            ["orders.tenant_id", "orders.id", "orders.currency"],
            name="fk_order_items_order_tenant_currency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_tenant_id", "source_product_id"],
            ["products.tenant_id", "products.id"],
            name="fk_order_items_source_product",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "order_id",
            "line_number",
            name="uq_order_items_tenant_order_line",
        ),
    )
    op.create_index(
        "ix_order_items_source_product",
        "order_items",
        ["source_tenant_id", "source_product_id"],
        unique=False,
    )
    op.execute(_ORDER_ITEM_IMMUTABILITY_TRIGGER)


def downgrade() -> None:
    connection = op.get_bind()
    populated_tables = [
        table
        for table in _COMMERCE_TABLES
        if connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")
        ).scalar_one()
    ]
    if populated_tables:
        raise RuntimeError(
            "Cannot downgrade commerce schema while commerce data exists"
        )

    op.execute("DROP TRIGGER trg_order_items_immutable_snapshots")
    op.drop_index("ix_order_items_source_product", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_tenant_status_created", table_name="orders")
    op.drop_index("ix_orders_tenant_created", table_name="orders")
    op.drop_index("ix_orders_tenant_customer_created", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_cart_items_tenant_product", table_name="cart_items")
    op.drop_table("cart_items")
    op.drop_table("carts")

    op.drop_index("uq_users_tenant_id_id", table_name="users")
    op.drop_index("ix_products_tenant_id_id", table_name="products")
    op.create_index(
        "ix_products_tenant_id_id",
        "products",
        ["tenant_id", "id"],
        unique=False,
    )
