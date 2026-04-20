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
    """Створює таблиці якщо не існують. Для dev — достатньо."""
    from app.database.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_engine() -> None:
    await engine.dispose()
