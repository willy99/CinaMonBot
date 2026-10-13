"""
Автентифікація адмін-дашборду.

- Пароль: scrypt (stdlib hashlib), формат "scrypt$<salt_hex>$<hash_hex>".
- 2FA: 6-значний код надсилається адміну в Telegram через Bot API (той самий BOT_TOKEN).
  Коди живуть у пам'яті процесу: {admin_id: _PendingCode}.
- Антиперебір: лічильник невдалих спроб на (username, ip) у пам'яті, блок на N хвилин; плюс лічильник на логін
  у БД (незалежно від IP, переживає рестарт) з порогом DB_LOCK_FACTOR × ADMIN_LOGIN_MAX_ATTEMPTS.
- Сесія: app.storage.user["admin"] = {"id", "username", "role", "ver", "login_at"}. check_session() звіряє її з БД:
  деактивація, зміна пароля / 2FA / ролі (session_version) одразу роблять чинні сесії недійсними.
- Ролі: owner керує адмінами й поверненнями коштів; admin — решта.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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

OWNER = "owner"
DB_LOCK_FACTOR = 3
_SESSION_CACHE_TTL = 5.0
_session_cache: dict[str, tuple[float, bool, int, str]] = {}


async def _admin_state(admin_id: str) -> tuple[bool, int, str]:
    """(активний, session_version, role) з коротким кешем — middleware питає на кожен запит."""
    now = time.monotonic()
    cached = _session_cache.get(admin_id)
    if cached and now - cached[0] < _SESSION_CACHE_TTL:
        return cached[1:]
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
    state = (bool(admin and admin.is_active), admin.session_version if admin else -1, admin.role if admin else "")
    _session_cache[admin_id] = (now, *state)
    return state


def forget_session_cache(admin_id: str | None = None) -> None:
    if admin_id is None:
        _session_cache.clear()
    else:
        _session_cache.pop(admin_id, None)


async def check_session() -> dict | None:
    """current_admin() + звірка з БД. Недійсну сесію закриває. Для middleware і періодичної перевірки сторінок."""
    data = current_admin()
    if not data:
        return None
    active, version, role = await _admin_state(data["id"])
    if not active or version != data.get("ver") or role != data.get("role"):
        logger.info("admin_session_revoked", admin=data.get("username"))
        logout()
        return None
    return data


def is_owner() -> bool:
    return (current_admin() or {}).get("role") == OWNER


async def verify_current_password(admin_id: str, password: str) -> bool:
    """Повторне підтвердження перед чутливою дією (зміна пароля / 2FA)."""
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
    return bool(admin and admin.is_active and verify_password(password or "", admin.password_hash))


async def db_lock_remaining(username: str) -> int:
    """Скільки секунд ще заблоковано вхід під цим логіном (лічильник у БД)."""
    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.username == username.strip().lower()))).scalar_one_or_none()
    if not admin or not admin.locked_until:
        return 0
    return max(0, int((admin.locked_until - datetime.utcnow()).total_seconds()))


async def db_register_failure(username: str) -> None:
    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.username == username.strip().lower()))).scalar_one_or_none()
        if not admin:
            return
        admin.failed_logins += 1
        if admin.failed_logins >= settings.ADMIN_LOGIN_MAX_ATTEMPTS * DB_LOCK_FACTOR:
            admin.locked_until = datetime.utcnow() + timedelta(minutes=settings.ADMIN_LOGIN_LOCK_MINUTES)
            admin.failed_logins = 0
            logger.warning("admin_login_locked_db", username=admin.username)


async def db_register_success(admin_id: str) -> None:
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin:
            admin.failed_logins, admin.locked_until = 0, None


def safe_next_path(path: str) -> str:
    """Куди повернути після входу: лише локальний шлях («//host» і «/\\host» браузер сприйме як інший сайт)."""
    if not path.startswith("/") or path.startswith(("//", "/\\")):
        return "/"
    return path


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


async def login_session(admin_id: str) -> None:
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
    forget_session_cache(admin_id)
    app.storage.user["admin"] = {"id": admin.id, "username": admin.username, "role": admin.role,
                                 "ver": admin.session_version, "login_at": time.time()}


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
