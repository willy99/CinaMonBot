"""
Налаштування, які адмін міняє з дашборду (/settings) без рестарту. Логіка — у спільному пакеті
(botkit.runtime_settings): перекриття .env у таблиці app_settings, кеш ~10 с між процесами бота й адмінки.
Тут — лише що саме можна редагувати.

    from app.services import runtime_settings as rt
    limit = rt.cached("FREE_TIER_MAX_ITEMS")        # sync, з кешу (бот оновлює його кожні 10 с)
    interval = await rt.get("PREMIUM_CHECK_INTERVAL_MINUTES")
"""
from __future__ import annotations

from botkit import runtime_settings as _rt
from botkit.runtime_settings import Spec

# Ключ = ім'я поля Settings; підписи — в app/admin/i18n.py (setting_<key>, setting_<key>_help)
EDITABLE_SPECS: dict[str, Spec] = {
    "FREE_TIER_CHECK_INTERVAL_MINUTES": Spec(int, 30, 1440, "monitoring", "unit_minutes"),
    "PREMIUM_CHECK_INTERVAL_MINUTES": Spec(int, 10, 720, "monitoring", "unit_minutes"),
    "RESTOCK_COOLDOWN_DAYS": Spec(int, 0, 60, "monitoring", "unit_days"),
    "SCRAPE_DELAY_SECONDS": Spec(float, 0.5, 30, "scraping", "unit_seconds"),
    "SCRAPE_MAX_RETRIES": Spec(int, 1, 5, "scraping", ""),
    "FREE_TIER_MAX_ITEMS": Spec(int, 1, 100, "plans", ""),
    "PREMIUM_PRICE_UAH": Spec(int, 1, 10000, "plans", "unit_uah"),
}
GROUPS = ("monitoring", "scraping", "plans")
_rt.register(EDITABLE_SPECS, GROUPS)

# Публічне API — з пакета (той самий кеш і функції для всіх модулів проєкту)
EDITABLE = _rt.EDITABLE
get, cached, refresh, set_value, reset_value, snapshot = _rt.get, _rt.cached, _rt.refresh, _rt.set_value, _rt.reset_value, _rt.snapshot
coerce, default, load_overrides, reset_cache = _rt.coerce, _rt.default, _rt.load_overrides, _rt.reset_cache
