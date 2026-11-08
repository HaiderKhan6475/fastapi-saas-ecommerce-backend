"""Add persistent authentication rate-limit buckets.

Revision ID: 0003_auth_hardening
Revises: 0002_integrity_indexes
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_auth_hardening"
down_revision: Union[str, None] = "0002_integrity_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limit_buckets",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "attempts > 0",
            name="ck_auth_rate_limit_attempts_positive",
        ),
        sa.PrimaryKeyConstraint("key", "window_start"),
    )
    op.create_index(
        "ix_auth_rate_limit_window_start",
        "auth_rate_limit_buckets",
        ["window_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_rate_limit_window_start",
        table_name="auth_rate_limit_buckets",
    )
    op.drop_table("auth_rate_limit_buckets")
