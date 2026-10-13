"""Запити адмінки про адмінів і журнал дій (моделі — з контексту проєкту)."""
from __future__ import annotations

from sqlalchemy import func, select

from botkit.context import ctx


def _m():
    return ctx().models


def _fmt(d, fmt: str = "%d.%m.%Y %H:%M") -> str:
    ui = ctx().admin_ui
    return ui.fmt_dt(d, fmt) if ui else (d.strftime(fmt) if d else "—")


async def admins_list() -> list[dict]:
    Admin = _m().Admin  # noqa: N806
    async with ctx().get_session() as s:
        rows = (await s.execute(select(Admin).order_by(Admin.created_at.asc()))).scalars().all()
    return [{
        "id": a.id, "username": a.username, "telegram_id": a.telegram_id or "", "active": a.is_active, "role": a.role,
        "last_login": _fmt(a.last_login_at) if a.last_login_at else "ніколи",
        "created": _fmt(a.created_at, "%d.%m.%Y"),
    } for a in rows]


async def admins_count_active() -> int:
    Admin = _m().Admin  # noqa: N806
    async with ctx().get_session() as s:
        return (await s.execute(select(func.count()).select_from(Admin).where(Admin.is_active == True))).scalar_one()  # noqa: E712


async def owners_count_active() -> int:
    Admin = _m().Admin  # noqa: N806
    async with ctx().get_session() as s:
        return (await s.execute(select(func.count()).select_from(Admin).where(
            Admin.is_active == True, Admin.role == "owner"))).scalar_one()  # noqa: E712


async def create_admin(username: str, password_hash: str, telegram_id: int | None, role: str = "admin"):
    async with ctx().get_session() as s:
        a = _m().Admin(username=username.strip().lower(), password_hash=password_hash, telegram_id=telegram_id,
                       is_active=True, role=role)
        s.add(a)
        await s.flush()
        return a


async def admin_exists(username: str) -> bool:
    Admin = _m().Admin  # noqa: N806
    async with ctx().get_session() as s:
        return (await s.execute(
            select(func.count()).select_from(Admin).where(Admin.username == username.strip().lower())
        )).scalar_one() > 0


async def _update(admin_id: str, **values) -> bool:
    """Будь-яка зміна адміна збільшує session_version — його чинні сесії стають недійсними."""
    async with ctx().get_session() as s:
        a = await s.get(_m().Admin, admin_id)
        if not a:
            return False
        for k, v in values.items():
            setattr(a, k, v)
        a.session_version += 1
    return True


async def set_admin_active(admin_id: str, active: bool) -> bool:
    return await _update(admin_id, is_active=active)


async def set_admin_password(admin_id: str, password_hash: str) -> bool:
    return await _update(admin_id, password_hash=password_hash)


async def set_admin_telegram(admin_id: str, telegram_id: int | None) -> bool:
    return await _update(admin_id, telegram_id=telegram_id)


async def set_admin_role(admin_id: str, role: str) -> bool:
    return await _update(admin_id, role=role)


async def ensure_owner() -> str | None:
    """Якщо серед активних адмінів немає owner — найстаріший активний стає owner. Повертає його логін."""
    Admin = _m().Admin  # noqa: N806
    async with ctx().get_session() as s:
        active = (await s.execute(
            select(Admin).where(Admin.is_active == True).order_by(Admin.created_at.asc())  # noqa: E712
        )).scalars().all()
        if not active or any(a.role == "owner" for a in active):
            return None
        active[0].role = "owner"
        active[0].session_version += 1
        return active[0].username


async def audit_log(limit: int = 50) -> list[dict]:
    AdminAction = _m().AdminAction  # noqa: N806
    async with ctx().get_session() as s:
        rows = (await s.execute(select(AdminAction).order_by(AdminAction.created_at.desc()).limit(limit))).scalars().all()
    return [{
        "when": _fmt(a.created_at), "admin": a.admin_username, "action": a.action,
        "target": a.target or "", "details": a.details or "",
    } for a in rows]
