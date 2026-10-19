"""price_trackers: target_direction (above/below), ends_at, remind_before_minutes, ending_notified_at

Revision ID: c3d9f0a1b2e4
Revises: b7c41e2d9a10
Create Date: 2026-10-19 15:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from app.database.migrations.helpers import add_column_if_missing

revision: str = "c3d9f0a1b2e4"
down_revision: str | None = "b7c41e2d9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_column_if_missing("price_trackers", sa.Column("target_direction", sa.String(length=8), nullable=True))
    add_column_if_missing("price_trackers", sa.Column("ends_at", sa.DateTime(), nullable=True))
    add_column_if_missing("price_trackers", sa.Column("remind_before_minutes", sa.Integer(), nullable=True))
    add_column_if_missing("price_trackers", sa.Column("ending_notified_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    from alembic import op
    with op.batch_alter_table("price_trackers") as batch_op:
        for col in ("ending_notified_at", "remind_before_minutes", "ends_at", "target_direction"):
            batch_op.drop_column(col)
