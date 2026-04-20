"""
Database models — сумісні з SQLite і PostgreSQL.

Головна відмінність від попередньої версії:
- Не використовуємо PostgreSQL-специфічний тип UUID
- Зберігаємо UUID як String(36) — працює скрізь
"""
from __future__ import annotations

import uuid
from datetime import datetime
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
    PAUSED = "paused"
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
    source: Mapped[str] = mapped_column(String(32), nullable=False)

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
