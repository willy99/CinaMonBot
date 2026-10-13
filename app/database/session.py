from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def _create_engine() -> AsyncEngine:
    """
    Налаштування engine залежать від типу БД.
    SQLite: не підтримує connection pool → NullPool
    PostgreSQL: використовуємо pool для продуктивності
    """
    if settings.is_sqlite:
        # SQLite не підтримує concurrent writes добре,
        # але для старту з кількома сотнями юзерів — ок.
        # check_same_thread=False потрібно для async
        return create_async_engine(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False},
            echo=not settings.is_production,
        )
    else:
        # PostgreSQL — повноцінний connection pool
        return create_async_engine(
            settings.DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            echo=not settings.is_production,
        )


engine: AsyncEngine = _create_engine()

if settings.is_sqlite:
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        # WAL: бот і адмін-дашборд читають/пишуть одну базу з різних процесів.
        # busy_timeout: замість "database is locked" чекаємо до 5 с.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        # SQLite за замовчуванням ігнорує ON DELETE CASCADE / SET NULL — вмикаємо
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

AsyncSessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Застаріле: адитивна міграція на спільному engine. Схему тепер веде Alembic — див. app/database/migrate.py."""
    await legacy_upgrade(engine)


async def legacy_upgrade(engine) -> None:
    """
    Шлях з баз, створених до Alembic: створює таблиці, яких немає, і додає колонки, яких бракує
    (ALTER TABLE ... ADD COLUMN). Після цього migrate.py позначає базу ревізією.
    """
    from app.database.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns, Base)


def _add_missing_columns(sync_conn, base) -> list[str]:
    """Порівнює моделі з базою і додає відсутні колонки. Повертає список 'table.column'."""
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    for table in base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            ddl = CreateColumn(column).compile(dialect=sync_conn.dialect)
            spec = str(ddl)
            # NOT NULL без server_default не можна додати в непорожню таблицю — ставимо дефолт із моделі
            if not column.nullable and column.server_default is None:
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                if default is not None:
                    if isinstance(default, bool):
                        literal = "1" if default else "0"
                        if sync_conn.dialect.name == "postgresql":
                            literal = "true" if default else "false"
                    elif isinstance(default, (int, float)):
                        literal = str(default)
                    else:
                        literal = f"'{default}'"
                    spec += f" DEFAULT {literal}"
                else:
                    spec = spec.replace(" NOT NULL", "")
            sync_conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {spec}"))
            added.append(f"{table.name}.{column.name}")
    if added:
        import structlog
        structlog.get_logger(__name__).info("db_columns_added", columns=added)
    return added


async def close_engine() -> None:
    await engine.dispose()
