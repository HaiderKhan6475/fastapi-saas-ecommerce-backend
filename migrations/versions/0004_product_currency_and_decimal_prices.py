"""Add tenant currencies, decimal product prices, and optimistic versions.

Revision ID: 0004_product_currency_decimal
Revises: 0003_auth_hardening
"""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_product_currency_decimal"
down_revision: Union[str, None] = "0003_auth_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _validate_existing_product_values() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT name, description, price FROM products"))
    scale = Decimal("0.0001")
    for name, description, raw_price in rows:
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise RuntimeError(
                "Product migration stopped: an existing product name is outside "
                "the supported 1-255 character range"
            )
        if description is not None and (
            not isinstance(description, str) or len(description) > 5000
        ):
            raise RuntimeError(
                "Product migration stopped: an existing description exceeds "
                "the supported 5000 character limit"
            )
        try:
            price = Decimal(str(raw_price))
            with localcontext() as context:
                context.prec = 60
                quantized = price.quantize(scale)
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError(
                "Product migration stopped: an existing price cannot be "
                "represented as NUMERIC(15, 4)"
            ) from exc
        if (
            not price.is_finite()
            or price <= 0
            or quantized != price
            or price.adjusted() >= 11
        ):
            raise RuntimeError(
                "Product migration stopped: an existing price cannot be "
                "represented as NUMERIC(15, 4) without changing its value"
            )


def upgrade() -> None:
    _validate_existing_product_values()

    op.add_column(
        "tenants",
        sa.Column(
            "default_currency",
            sa.String(length=3),
            sa.CheckConstraint(
                "length(default_currency) = 3 "
                "AND default_currency = upper(default_currency)",
                name="ck_tenants_default_currency_format",
            ),
            nullable=False,
            server_default=sa.text("'PKR'"),
        ),
    )

    with op.batch_alter_table("products", recreate="always") as batch_op:
        batch_op.alter_column(
            "price",
            existing_type=sa.Float(),
            type_=sa.Numeric(precision=15, scale=4),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("currency", sa.String(length=3), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=True))

    op.execute(
        "UPDATE products SET currency = ("
        "SELECT default_currency FROM tenants WHERE tenants.id = products.tenant_id"
        ") WHERE currency IS NULL OR trim(currency) = ''"
    )
    op.execute("UPDATE products SET version = 1 WHERE version IS NULL OR version < 1")

    with op.batch_alter_table("products", recreate="always") as batch_op:
        batch_op.alter_column(
            "currency",
            existing_type=sa.String(length=3),
            nullable=False,
            server_default="PKR",
        )
        batch_op.alter_column(
            "version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
        batch_op.create_check_constraint(
            "ck_products_currency_format",
            "length(currency) = 3 AND currency = upper(currency)",
        )
        batch_op.create_check_constraint(
            "ck_products_version_positive",
            "version > 0",
        )
        batch_op.create_check_constraint(
            "ck_products_name_length",
            "length(name) BETWEEN 1 AND 255",
        )
        batch_op.create_check_constraint(
            "ck_products_description_length",
            "description IS NULL OR length(description) <= 5000",
        )

    op.create_index(
        "ix_products_tenant_currency_id",
        "products",
        ["tenant_id", "currency", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_products_tenant_currency_id", table_name="products")
    with op.batch_alter_table("products", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_products_description_length", type_="check")
        batch_op.drop_constraint("ck_products_name_length", type_="check")
        batch_op.drop_constraint("ck_products_version_positive", type_="check")
        batch_op.drop_constraint("ck_products_currency_format", type_="check")
        batch_op.drop_column("version")
        batch_op.drop_column("currency")
        batch_op.alter_column(
            "price",
            existing_type=sa.Numeric(precision=15, scale=4),
            type_=sa.Float(),
            existing_nullable=False,
        )

    op.drop_column("tenants", "default_currency")
