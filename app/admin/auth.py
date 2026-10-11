"""
Автентифікація адмін-дашборду.

- Пароль: scrypt (stdlib hashlib), формат "scrypt$<salt_hex>$<hash_hex>".
- 2FA: 6-значний код надсилається адміну в Telegram через Bot API (той самий BOT_TOKEN).
  Коди живуть у пам'яті процесу: {admin_id: _PendingCode}.
- Антиперебір: лічильник невдалих спроб на (username, ip), блок на N хвилин.
- Сесія: app.storage.user["admin"] = {"id", "username", "login_at"}.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog
from nicegui import app
from sqlalchemy import select

from app.config import settings
from app.database.models import Admin, AdminAction
from app.database.session import get_session

logger = structlog.get_logger(__name__)

# ─── Паролі ──────────────────────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def validate_password_strength(password: str) -> str | None:
    """Повертає текст помилки або None."""
    if len(password) < 10:
        return "Пароль має бути не коротший за 10 символів"
    if password.isdigit() or password.isalpha():
        return "Пароль має містити і літери, і цифри"
    return None


# ─── Антиперебір ─────────────────────────────────────────────────

@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


_attempts: dict[str, _Attempts] = {}


def _attempt_key(username: str, ip: str) -> str:
    return f"{username.lower()}|{ip}"


def is_locked(username: str, ip: str) -> int:
    """Скільки секунд ще триває блокування (0 — не заблоковано)."""
    a = _attempts.get(_attempt_key(username, ip))
    if not a:
        return 0
    remaining = int(a.locked_until - time.monotonic())
    if remaining <= 0:
        if a.locked_until:
            _attempts.pop(_attempt_key(username, ip), None)
        return 0
    return remaining


def register_failure(username: str, ip: str) -> None:
    a = _attempts.setdefault(_attempt_key(username, ip), _Attempts())
    a.failures += 1
    if a.failures >= settings.ADMIN_LOGIN_MAX_ATTEMPTS:
        a.locked_until = time.monotonic() + settings.ADMIN_LOGIN_LOCK_MINUTES * 60
        a.failures = 0
        logger.warning("admin_login_locked", username=username, ip=ip)


def register_success(username: str, ip: str) -> None:
    _attempts.pop(_attempt_key(username, ip), None)


# ─── 2FA ─────────────────────────────────────────────────────────

@dataclass
class _PendingCode:
    code: str
    expires_at: float
    attempts: int = 0
    admin: dict = field(default_factory=dict)


_pending: dict[str, _PendingCode] = {}
_MAX_CODE_ATTEMPTS = 5


async def send_telegram_message(chat_id: int, text: str) -> bool:
    token = settings.BOT_TOKEN.get_secret_value()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            if r.status_code != 200:
                logger.warning("telegram_send_failed", status=r.status_code, body=r.text[:200])
                return False
        return True
    except Exception as e:
        logger.error("telegram_send_error", error=str(e))
        return False


async def start_2fa(admin: Admin, ip: str) -> tuple[bool, str]:
    """Генерує код і шле в Telegram. Повертає (ok, повідомлення для юзера)."""
    if not admin.telegram_id:
        return False, "У цього адміна не вказано Telegram ID для 2FA"

    code = f"{secrets.randbelow(1_000_000):06d}"
    _pending[admin.id] = _PendingCode(
        code=code,
        expires_at=time.monotonic() + settings.ADMIN_2FA_CODE_TTL_SECONDS,
        admin={"id": admin.id, "username": admin.username},
    )
    minutes = settings.ADMIN_2FA_CODE_TTL_SECONDS // 60
    ok = await send_telegram_message(
        admin.telegram_id,
        f"🔐 <b>Код входу в адмінку ЦінаБот</b>\n\n"
        f"<code>{code}</code>\n\n"
        f"Дійсний {minutes} хв. IP: <code>{ip or '—'}</code>\n"
        f"Якщо це не ви — змініть пароль.",
    )
    if not ok:
        _pending.pop(admin.id, None)
        return False, "Не вдалося надіслати код у Telegram. Переконайтесь, що ви писали боту."
    return True, "Код надіслано у ваш Telegram"


def verify_2fa(admin_id: str, code: str) -> tuple[bool, str]:
    pending = _pending.get(admin_id)
    if not pending:
        return False, "Код не запитувався або вже використаний"
    if time.monotonic() > pending.expires_at:
        _pending.pop(admin_id, None)
        return False, "Код прострочений, увійдіть заново"
    pending.attempts += 1
    if pending.attempts > _MAX_CODE_ATTEMPTS:
        _pending.pop(admin_id, None)
        return False, "Забагато спроб, увійдіть заново"
    if not hmac.compare_digest(pending.code, code.strip()):
        return False, "Невірний код"
    _pending.pop(admin_id, None)
    return True, "OK"


# ─── Сесія ───────────────────────────────────────────────────────

def current_admin() -> dict | None:
    """{"id", "username", "login_at"} або None, якщо не залогінений / сесія прострочена."""
    try:
        data = app.storage.user.get("admin")
    except Exception:
        return None
    if not data:
        return None
    login_at = data.get("login_at", 0)
    if time.time() - login_at > settings.ADMIN_SESSION_HOURS * 3600:
        logout()
        return None
    return data


def login_session(admin_id: str, username: str) -> None:
    app.storage.user["admin"] = {"id": admin_id, "username": username, "login_at": time.time()}


def logout() -> None:
    app.storage.user.pop("admin", None)
    app.storage.user.pop("pending_2fa", None)


async def authenticate(username: str, password: str) -> Admin | None:
    """Перевіряє логін/пароль. Повертає Admin або None. Не враховує lock — це робить сторінка."""
    async with get_session() as session:
        admin = (await session.execute(
            select(Admin).where(Admin.username == username.strip().lower())
        )).scalar_one_or_none()
    if not admin or not admin.is_active:
        # Витрачаємо час як на справжню перевірку, щоб не було timing-витоку існування логіна
        verify_password(password, hash_password("dummy-password-1"))
        return None
    if not verify_password(password, admin.password_hash):
        return None
    return admin


async def mark_logged_in(admin_id: str) -> None:
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin:
            admin.last_login_at = datetime.now(timezone.utc)


async def audit(action: str, target: str | None = None, details: str | None = None) -> None:
    """Пише дію поточного адміна в журнал."""
    who = current_admin() or {"id": None, "username": "system"}
    async with get_session() as session:
        session.add(AdminAction(
            admin_id=who.get("id"),
            admin_username=who.get("username", "system"),
            action=action,
            target=target,
            details=details,
        ))
    logger.info("admin_action", admin=who.get("username"), action=action, target=target)
