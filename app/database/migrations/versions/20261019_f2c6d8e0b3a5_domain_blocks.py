"""domain_blocks: магазини, що стабільно блокують перевірки (позиції юзерів — paused_domain)

Revision ID: f2c6d8e0b3a5
Revises: e9b4c7d2a1f3
Create Date: 2026-10-19 23:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from app.database.migrations.helpers import create_table_if_missing

revision: str = "f2c6d8e0b3a5"
down_revision: str | None = "e9b4c7d2a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_table_if_missing(
        "domain_blocks",
        sa.Column("domain", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("blocked_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("domain"),
    )


def downgrade() -> None:
    from alembic import op
    op.drop_table("domain_blocks")
