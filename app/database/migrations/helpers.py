"""Помічники ревізій — у спільному пакеті (botkit/db/migration_helpers.py); тут для наявних ревізій."""
from botkit.db.migration_helpers import (  # noqa: F401
    add_column_if_missing,
    create_table_if_missing,
    has_column,
    has_table,
)
