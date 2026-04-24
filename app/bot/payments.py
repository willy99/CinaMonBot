"""
Оплата через LiqPay.

Флоу:
1. Юзер натискає "Оплатити 99 грн"
2. Генеруємо LiqPay посилання з підписом
3. Юзер переходить → платить карткою
4. LiqPay робить callback на наш ендпоінт (або ми перевіряємо статус)
5. Активуємо Premium

Оскільки ми на polling (без webhook сервера) — використовуємо
перевірку статусу через LiqPay API після оплати.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone, timedelta

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

LIQPAY_API_URL = "https://www.liqpay.ua/api/3/checkout"
LIQPAY_REQUEST_URL = "https://www.liqpay.ua/api/request"


def _encode(data: dict) -> str:
    """Кодує дані в base64 для LiqPay."""
    return base64.b64encode(json.dumps(data).encode()).decode()


def _sign(data_str: str) -> str:
    """Генерує підпис для LiqPay запиту."""
    private_key = settings.LIQPAY_PRIVATE_KEY.get_secret_value()
    sign_str = private_key + data_str + private_key
    return base64.b64encode(
        hashlib.sha1(sign_str.encode()).digest()
    ).decode()


def create_payment_url(user_id: str, telegram_id: int) -> str:
    """
    Генерує посилання на сторінку оплати LiqPay.
    order_id містить user_id щоб після оплати знати кому активувати Premium.
    """
    order_id = f"premium_{user_id}_{int(datetime.now().timestamp())}"

    data = {
        "version": 3,
        "public_key": settings.LIQPAY_PUBLIC_KEY,
        "action": "pay",
        "amount": settings.PREMIUM_PRICE_UAH,
        "currency": "UAH",
        "description": f"ЦінаБот Premium — 1 місяць",
        "order_id": order_id,
        # Після оплати LiqPay показує цю сторінку юзеру
        "result_url": "https://t.me/cina_mon_bot",
        # LiqPay надішле POST на цей URL (потрібен якщо є вебхук)
        # "server_url": "https://yourserver.com/liqpay/callback",
        "language": "uk",
    }

    data_str = _encode(data)
    signature = _sign(data_str)

    return f"{LIQPAY_API_URL}?data={data_str}&signature={signature}"


async def check_payment_status(order_id: str) -> str | None:
    """
    Перевіряє статус платежу через LiqPay API.
    Повертає статус: 'success', 'failure', 'wait_accept', None
    """
    data = {
        "version": 3,
        "public_key": settings.LIQPAY_PUBLIC_KEY,
        "action": "status",
        "order_id": order_id,
    }

    data_str = _encode(data)
    signature = _sign(data_str)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                LIQPAY_REQUEST_URL,
                data={"data": data_str, "signature": signature},
            )
            result = resp.json()
            return result.get("status")
    except Exception as e:
        logger.error("liqpay_status_check_failed", error=str(e))
        return None


async def activate_premium(user_id: str) -> None:
    """Активує Premium на 30 днів для юзера."""
    from app.database.session import get_session
    from app.database.models import User

    async with get_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return

        user.tier = "premium"
        user.premium_until = datetime.now(timezone.utc) + timedelta(days=30)
        logger.info("premium_activated", user_id=user_id, until=user.premium_until)