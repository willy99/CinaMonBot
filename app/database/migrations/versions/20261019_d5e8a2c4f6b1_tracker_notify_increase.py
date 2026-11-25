"""price_trackers: notify_increase (сповіщати про подорожчання ≥3%; за замовчуванням вимкнено)

Revision ID: d5e8a2c4f6b1
Revises: c3d9f0a1b2e4
Create Date: 2026-10-19 18:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from app.database.migrations.helpers import add_column_if_missing

revision: str = "d5e8a2c4f6b1"
down_revision: str | None = "c3d9f0a1b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_column_if_missing("price_trackers",
                          sa.Column("notify_increase", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    from alembic import op
    with op.batch_alter_table("price_trackers") as batch_op:
        batch_op.drop_column("notify_increase")
