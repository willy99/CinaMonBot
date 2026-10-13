"""
Міграції схеми (Alembic). Один запуск перед стартом процесів — не з кожного процесу одночасно.

    python -m app.database.migrate          # ./run.sh migrate; ./run.sh start і dev роблять це самі
    python -m app.database.migrate --check  # 0 — схема актуальна, 1 — треба мігрувати

- База без alembic_version, але з таблицями (створена до Alembic): стара адитивна логіка (legacy_upgrade)
  доводить її до моделей, ми позначаємо baseline і проганяємо ревізії після нього (вони ідемпотентні щодо
  схеми — migrations/helpers.py — тож виконуються лише їхні міграції даних).
- Порожня база: alembic upgrade head.
- Файловий lock: два одночасні запуски на одному хості не перетнуться.

Процеси (бот, адмінка, API) при старті лише перевіряють ensure_schema() і зупиняються з підказкою.
Нова колонка / таблиця = нова ревізія: alembic revision --autogenerate -m "що змінилось", а потім
op.add_column / op.create_table замінити на add_column_if_missing / create_table_if_missing (helpers.py).
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import sys
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE = ROOT / ".migrate.lock"


class SchemaOutdated(RuntimeError):
    pass


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = settings.DATABASE_URL
    cfg.attributes["configure_logger"] = False     # не чіпаємо логування процесу (fileConfig скинув би його)
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def current_revision() -> tuple[str | None, bool]:
    """(ревізія в базі або None, чи є в базі таблиці застосунку)."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rev = await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
            has_tables = await conn.run_sync(lambda c: "users" in inspect(c).get_table_names())
    finally:
        await engine.dispose()
    return rev, has_tables


async def _legacy_upgrade() -> None:
    from app.database.session import legacy_upgrade
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        await legacy_upgrade(engine)
    finally:
        await engine.dispose()


@contextmanager
def _lock():
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def migrate() -> str:
    """Доводить базу до head. Синхронна (Alembic сам крутить asyncio) — не викликати з event loop."""
    cfg = alembic_config()
    with _lock():
        rev, has_tables = asyncio.run(current_revision())
        if rev is None and has_tables:
            asyncio.run(_legacy_upgrade())
            # legacy_upgrade довела схему до моделей; позначаємо baseline, а ревізії після нього (ідемпотентні
            # щодо схеми, див. migrations/helpers.py) проженемо — інакше пропустили б їхні міграції даних
            command.stamp(cfg, ScriptDirectory.from_config(cfg).get_base())
        command.upgrade(cfg, "head")
    return head_revision()


async def ensure_schema() -> None:
    """Для процесів: схема має бути на head, інакше — зрозуміла помилка замість дивних збоїв у рантаймі."""
    rev, _ = await current_revision()
    head = head_revision()
    if rev != head:
        raise SchemaOutdated(f"Схема бази {rev or 'без версії'} ≠ {head}. Запусти ./run.sh migrate")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.database.migrate")
    parser.add_argument("--check", action="store_true", help="лише перевірити, чи схема актуальна")
    args = parser.parse_args()
    if args.check:
        try:
            asyncio.run(ensure_schema())
        except SchemaOutdated as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        print("Схема актуальна:", head_revision())
        return
    print("Схема на ревізії:", migrate())


if __name__ == "__main__":
    main()
