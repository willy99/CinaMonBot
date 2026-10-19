"""users.share_token (посилання «мій список бажань») і users.source (звідки прийшов юзер)

Revision ID: b7c41e2d9a10
Revises: 66a2deca7ebe
Create Date: 2026-10-19 12:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.database.migrations.helpers import add_column_if_missing

revision: str = "b7c41e2d9a10"
down_revision: str | None = "66a2deca7ebe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_index(table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in sa.inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    add_column_if_missing("users", sa.Column("share_token", sa.String(length=24), nullable=True))
    add_column_if_missing("users", sa.Column("source", sa.String(length=32), nullable=True))
    if not _has_index("users", "ix_users_share_token"):
        op.create_index("ix_users_share_token", "users", ["share_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_share_token", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("source")
        batch_op.drop_column("share_token")
