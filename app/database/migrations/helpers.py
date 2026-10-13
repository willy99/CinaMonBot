"""
Помічники для ревізій після baseline. Бази, створені до Alembic, migrate.py доводить до моделей старою
адитивною логікою і позначає baseline — тож нова таблиця/колонка там уже може існувати. Ревізії мають бути
ідемпотентними щодо схеми (create_table_if_missing / add_column_if_missing), а міграції даних — виконуватись завжди.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def add_column_if_missing(table: str, column: sa.Column) -> None:
    if not has_column(table, column.name):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(column)


def create_table_if_missing(name: str, *columns, **kw) -> None:
    if not has_table(name):
        op.create_table(name, *columns, **kw)
