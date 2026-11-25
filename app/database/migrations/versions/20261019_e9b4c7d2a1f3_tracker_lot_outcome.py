"""price_trackers: lot_outcome (аукціон: sold / unsold зі сторінки лота; продано → трекер на паузі)

Revision ID: e9b4c7d2a1f3
Revises: d5e8a2c4f6b1
Create Date: 2026-10-19 22:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from app.database.migrations.helpers import add_column_if_missing

revision: str = "e9b4c7d2a1f3"
down_revision: str | None = "d5e8a2c4f6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_column_if_missing("price_trackers", sa.Column("lot_outcome", sa.String(length=8), nullable=True))


def downgrade() -> None:
    from alembic import op
    with op.batch_alter_table("price_trackers") as batch_op:
        batch_op.drop_column("lot_outcome")
