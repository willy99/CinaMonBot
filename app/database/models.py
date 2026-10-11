"""
Database models — сумісні з SQLite і PostgreSQL.

Головна відмінність від попередньої версії:
- Не використовуємо PostgreSQL-специфічний тип UUID
- Зберігаємо UUID як String(36) — працює скрізь
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index,
    Integer, Numeric, String, Text, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─── Enums ───────────────────────────────────────────────────────

class SubscriptionTier(StrEnum):
    FREE = "free"
    PREMIUM = "premium"


class TrackerStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"              # зупинено вручну (адмін)
    PAUSED_LIMIT = "paused_limit"  # понад ліміт безкоштовного плану — не перевіряється
    ERROR = "error"
    OUT_OF_STOCK = "out_of_stock"


class PriceEventType(StrEnum):
    DECREASE = "decrease"
    INCREASE = "increase"
    RESTOCK = "restock"
    TARGET_REACHED = "target_reached"


class NotificationStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"


# ─── Models ──────────────────────────────────────────────────────

class User(Base, TimestampMixin):
    __tablename__ = "users"

    # String замість UUID — сумісно з SQLite і PostgreSQL
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str] = mapped_column(String(8), default="uk")

    tier: Mapped[str] = mapped_column(
        String(16), default=SubscriptionTier.FREE, nullable=False
    )
    premium_until: Mapped[datetime | None] = mapped_column(DateTime)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Щотижневий дайджест "що подешевшало" (можна вимкнути кнопкою в самому дайджесті)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime)

    trackers: Mapped[list[PriceTracker]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_telegram_id", "telegram_id"),
    )

    @property
    def is_premium(self) -> bool:
        if self.tier == SubscriptionTier.FREE:
            return False
        if self.premium_until is None:
            return False
        return datetime.utcnow() < self.premium_until

    @property
    def lang(self) -> str:
        """Мова інтерфейсу: 'uk' або 'en' (з language_code)."""
        from app.i18n import resolve_lang
        return resolve_lang(self.language_code)

    @property
    def active_tracker_limit(self) -> int:
        from app.config import settings
        return 999 if self.is_premium else settings.FREE_TIER_MAX_ITEMS


class PriceTracker(Base, TimestampMixin):
    __tablename__ = "price_trackers"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)  # домен без www

    title: Mapped[str | None] = mapped_column(String(512))
    image_url: Mapped[str | None] = mapped_column(Text)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="UAH")
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    status: Mapped[str] = mapped_column(
        String(16), default=TrackerStatus.ACTIVE, nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)

    # ── Налаштування сповіщень по товару ──
    notify_drop: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)      # зниження ціни / ціль
    notify_restock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)   # знову в наявності
    last_restock_notified_at: Mapped[datetime | None] = mapped_column(DateTime)           # антиспам для restock

    # ── Ціль у відсотках: "скажи, коли впаде на 15%" від ціни на момент додавання ──
    target_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    baseline_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    user: Mapped[User] = relationship(back_populates="trackers")
    price_history: Mapped[list[PriceHistory]] = relationship(
        back_populates="tracker",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("uq_tracker_user_url", "user_id", "canonical_url", unique=True),
        Index("ix_tracker_status_checked", "status", "last_checked_at"),
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tracker_id: Mapped[str] = mapped_column(
        ForeignKey("price_trackers.id", ondelete="CASCADE"), nullable=False
    )
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    tracker: Mapped[PriceTracker] = relationship(back_populates="price_history")

    __table_args__ = (
        Index("ix_price_history_tracker_date", "tracker_id", "recorded_at"),
    )


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tracker_id: Mapped[str] = mapped_column(
        ForeignKey("price_trackers.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_notifications_user", "user_id", "created_at"),
    )


# ─── Admin dashboard ─────────────────────────────────────────────

class Admin(Base, TimestampMixin):
    """Адміністратор веб-дашборду. Пароль — scrypt-хеш, 2FA — код у Telegram."""
    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # Telegram ID, куди надсилати код 2FA. Адмін має хоча б раз написати боту.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class AdminAction(Base):
    """Журнал дій адмінів (активація Premium, блокування, зміни адмінів)."""
    __tablename__ = "admin_actions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    admin_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("admins.id", ondelete="SET NULL"))
    admin_username: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_admin_actions_created_at", "created_at"),
    )


# ─── Журнал парсингу (для статистики по платформах у дашборді) ───

class ScrapeLog(Base):
    """
    Один рядок на кожну спробу спарсити сторінку (успішну чи ні).
    Пишеться парсером через app.services.scrapers.scrape_log.record_scrape().
    Старіші за 30 днів рядки прибирає планувальник.
    """
    __tablename__ = "scrape_log"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    domain: Mapped[str] = mapped_column(String(128), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # ok=False: "parse_failed" | "not_found" | "blocked" | "network" | "max_retries_exceeded" | ...
    reason: Mapped[str | None] = mapped_column(String(64))
    # ok=True: "json-ld" | "microdata" | "opengraph" | "selectors" | "heuristic"
    strategy: Mapped[str | None] = mapped_column(String(32))
    # True, якщо результат отримано після автолікування (Claude підібрав нові селектори)
    healed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_scrape_log_domain_created", "domain", "created_at"),
        Index("ix_scrape_log_created", "created_at"),
    )


# ─── Фідбек користувачів (/feedback у боті) ──────────────────────

class Feedback(Base):
    """
    Одне повідомлення від користувача через /feedback: текст або медіа
    (фото, файл, відео, голос...). Копія йде адміну в Telegram, а рядок
    лишається тут для інбоксу в дашборді (прочитано / відповіли).
    """
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))

    # Текст повідомлення або підпис до медіа
    text: Mapped[str | None] = mapped_column(Text)
    # "text" | "photo" | "document" | "video" | "voice" | "audio" | "sticker" | "other"
    content_type: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    # Telegram file_id найбільшого фото / документа (віддається через /media/<file_id> у дашборді)
    file_id: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(256))
    mime_type: Mapped[str | None] = mapped_column(String(128))

    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # message_id копії в чаті адміна (None, якщо доставити не вдалося)
    forwarded_message_id: Mapped[int | None] = mapped_column(BigInteger)

    # "new" | "read" | "replied"
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    reply_text: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    # "telegram:<admin id>" (через /reply у боті) або логін адміна дашборду
    replied_by: Mapped[str | None] = mapped_column(String(64))

    # Python-default із мікросекундами (у SQLite now() — секунди, а порядок у інбоксі має бути стабільним)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_feedback_status_created", "status", "created_at"),
        Index("ix_feedback_telegram_id", "telegram_id"),
    )
