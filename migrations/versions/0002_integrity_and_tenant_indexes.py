"""Enforce product numeric invariants and index tenant relationships.

Revision ID: 0002_integrity_indexes
Revises: 0001_initial_schema
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_integrity_indexes"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("products", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "ck_products_price_positive",
            "price > 0",
        )
        batch_op.create_check_constraint(
            "ck_products_stock_nonnegative",
            "stock >= 0",
        )

    op.create_index(
        "ix_users_tenant_id",
        "users",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_products_tenant_id_id",
        "products",
        ["tenant_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_products_tenant_id_id", table_name="products")
    op.drop_index("ix_users_tenant_id", table_name="users")
    with op.batch_alter_table("products", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_products_stock_nonnegative",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_products_price_positive",
            type_="check",
        )
