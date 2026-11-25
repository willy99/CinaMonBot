"""
Інлайн-клавіатури, спільні для планувальника і хендлерів бота.

Живуть окремо, бо планувальник (app.scheduler.price_checker) шле сповіщення з кнопкою
"🔕 Не сповіщати…", а хендлер mute:/unmute: перемальовує ту саму клавіатуру в
протилежному стані. Жодна кнопка тут не змінює адресата повідомлення — лише дані
callback_data з id трекера, власність якого хендлер перевіряє окремо.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import PriceEventType
from app.i18n import t

# Тип події → яку групу сповіщень вимикає кнопка під повідомленням
MUTE_KIND_BY_EVENT: dict[str, str] = {
    PriceEventType.RESTOCK: "restock",
    PriceEventType.DECREASE: "drop",
    PriceEventType.TARGET_REACHED: "drop",
    PriceEventType.INCREASE: "increase",
}

# Групи сповіщень по товару: kind у mute:/unmute:/ttoggle: → поле PriceTracker
NOTIFY_FIELDS: dict[str, str] = {"drop": "notify_drop", "restock": "notify_restock", "increase": "notify_increase"}


def notification_keyboard(lang: str, kind: str, tracker_id: str, *, muted: bool = False,
                          retarget: str | None = None) -> InlineKeyboardMarkup:
    """
    Клавіатура під сповіщенням: одна контекстна кнопка mute/unmute + "⚙️ Налаштування".
    kind: "restock" | "drop" | "increase". muted=True → кнопка "🔔 Увімкнути знову" (unmute:...).
    retarget: "above" | "below" — під «ціль досягнута»: кнопка «🎯 Нова межа ставки / Нова ціль» (newtarget:...).
    """
    if muted:
        toggle = InlineKeyboardButton(text=t(lang, "btn_unmute"), callback_data=f"unmute:{kind}:{tracker_id}")
    else:
        toggle = InlineKeyboardButton(text=t(lang, f"btn_mute_{kind}"), callback_data=f"mute:{kind}:{tracker_id}")
    rows = [[toggle]]
    if retarget:
        rows.append([InlineKeyboardButton(text=t(lang, f"btn_new_target_{retarget}"),
                                          callback_data=f"newtarget:{tracker_id}")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_tracker_settings"), callback_data=f"tsettings:{tracker_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def digest_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Одна кнопка під щотижневим дайджестом: "🔕 Вимкнути дайджест" (digest:off → User.digest_enabled=False)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_digest_off"), callback_data="digest:off")],
    ])


def digest_toggle_button(lang: str, enabled: bool) -> InlineKeyboardButton:
    """Рядок у /settings: "📬 Щотижневий дайджест ✅/☐" (digest:toggle)."""
    return InlineKeyboardButton(
        text=f"{t(lang, 'settings_digest')} {'✅' if enabled else '☐'}",
        callback_data="digest:toggle",
    )


REMIND_OPTIONS = (60, 30, 15, 5)      # за скільки хвилин до кінця торгів нагадати


def reminder_label(lang: str, minutes: int | None) -> str:
    return t(lang, f"reminder_{minutes}") if minutes in REMIND_OPTIONS else t(lang, "reminder_off")


def reminder_row(lang: str, tracker_id: str, current: int | None, prefix: str) -> list[InlineKeyboardButton]:
    """Ряд «1 год · 30 хв · 15 хв · 5 хв», вибраний — з ✅. prefix: rem (після додавання) / trem (налаштування)."""
    return [
        InlineKeyboardButton(text=("✅ " if m == current else "") + t(lang, f"rem_btn_{m}"),
                             callback_data=f"{prefix}:{m}:{tracker_id}")
        for m in REMIND_OPTIONS
    ]


def reminder_keyboard(lang: str, tracker_id: str, current: int | None) -> InlineKeyboardMarkup:
    """Під повідомленням «Додано!» для аукціонного лота."""
    return InlineKeyboardMarkup(inline_keyboard=[reminder_row(lang, tracker_id, current, "rem")])


def tracker_settings_keyboard(lang: str, tracker_id: str, *, notify_drop: bool, notify_restock: bool,
                              notify_increase: bool = False, remind_minutes: int | None = None,
                              auction: bool = False) -> InlineKeyboardMarkup:
    """Картка налаштувань одного товару: перемикачі (✅/☐), для аукціону — коли нагадати, назад, видалити."""
    def mark(flag: bool) -> str:
        return "✅" if flag else "☐"
    rows = [
        [InlineKeyboardButton(text=f"{mark(notify_drop)} {t(lang, 'toggle_drop')}",
                              callback_data=f"ttoggle:drop:{tracker_id}")],
        [InlineKeyboardButton(text=f"{mark(notify_increase)} {t(lang, 'toggle_increase')}",
                              callback_data=f"ttoggle:increase:{tracker_id}")],
    ]
    if auction:
        rows.append(reminder_row(lang, tracker_id, remind_minutes, "trem"))
        rows.append([InlineKeyboardButton(text=("✅ " if not remind_minutes else "") + t(lang, "rem_btn_off"),
                                          callback_data=f"trem:0:{tracker_id}")])
    else:
        rows.append([InlineKeyboardButton(text=f"{mark(notify_restock)} {t(lang, 'toggle_restock')}",
                                          callback_data=f"ttoggle:restock:{tracker_id}")])
    rows.append([
        InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="settings_menu"),
        InlineKeyboardButton(text=t(lang, "btn_delete"), callback_data=f"del:{tracker_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
