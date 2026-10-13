"""Локалізація адмінки (app/admin/i18n.py): паритет ключів uk/en, плейсхолдери, at(), мова/тема поза UI, форматування."""
from __future__ import annotations

from datetime import datetime
from string import Formatter

import pytest

from app.admin import i18n
from app.admin.i18n import (
    ADMIN_LANGUAGES, DEFAULT_ADMIN_LANG, DEFAULT_THEME, THEME_MODES, adate, admin_lang, amoney, anum, at, event_label,
    slot_labels, theme_mode,
)
from app.admin.queries import NOTIF_EVENT_LABELS


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


# ─── Паритет словників ───────────────────────────────────────────

def test_languages_match_translation_tables():
    assert set(ADMIN_LANGUAGES) == set(i18n._A)
    assert DEFAULT_ADMIN_LANG in i18n._A


def test_all_languages_have_same_keys():
    uk_keys = set(i18n._A["uk"])
    for lang, table in i18n._A.items():
        assert set(table) == uk_keys, f"{lang}: різниця ключів {set(table) ^ uk_keys}"


@pytest.mark.parametrize("key", sorted(i18n._A["uk"]))
def test_placeholders_match_between_languages(key):
    expected = _placeholders(i18n._A["uk"][key])
    for lang in i18n._A:
        assert _placeholders(i18n._A[lang][key]) == expected, f"{lang}.{key}"


def test_no_empty_translations():
    for lang, table in i18n._A.items():
        for key, value in table.items():
            assert isinstance(value, str) and value.strip(), f"{lang}.{key} порожній"


def test_event_labels_cover_queries():
    """Кожен код події з queries.NOTIF_EVENT_LABELS має переклад event_<code>."""
    for code in NOTIF_EVENT_LABELS:
        assert f"event_{code}" in i18n._A["uk"], code
    assert event_label("decrease") == NOTIF_EVENT_LABELS["decrease"]      # uk збігається з queries
    assert event_label("no-such-event") == "no-such-event"


# ─── at() / мова / тема поза UI-контекстом ───────────────────────

def test_default_lang_and_theme_outside_ui():
    assert admin_lang() == "uk" == DEFAULT_ADMIN_LANG
    assert theme_mode() == "dark" == DEFAULT_THEME
    assert THEME_MODES == ("dark", "light")


def test_at_returns_uk_by_default_and_formats():
    assert at("nav_dashboard") == "Дашборд"
    assert at("pager_shown", start=1, end=25, total=60) == "Показано 1–25 з 60"


def test_at_unknown_key_raises():
    with pytest.raises(KeyError):
        at("no_such_key")


def test_at_falls_back_to_uk_for_unknown_language(monkeypatch):
    monkeypatch.setattr(i18n, "_storage_get", lambda key, default: "de")
    assert admin_lang() == "uk"
    assert at("nav_users") == "Користувачі"


def test_at_english(monkeypatch):
    monkeypatch.setattr(i18n, "_storage_get", lambda key, default: "en")
    assert admin_lang() == "en"
    assert at("nav_users") == "Users"
    assert at("pager_page", page=2, pages=3) == "page 2 of 3"


def test_slot_labels_is_js_object_with_translations(monkeypatch):
    assert slot_labels({"active": "status_active"}) == '{"active": "Активний"}'
    monkeypatch.setattr(i18n, "_storage_get", lambda key, default: "en")
    assert slot_labels({"active": "status_active", "error": "status_error"}) == '{"active": "Active", "error": "Error"}'


def test_set_lang_and_theme_validate():
    with pytest.raises(ValueError):
        i18n.set_admin_lang("de")
    with pytest.raises(ValueError):
        i18n.set_theme_mode("sepia")


# ─── Форматування ────────────────────────────────────────────────

def test_amoney_anum_adate_uk():
    assert amoney(1234, "UAH") == "1 234 грн"
    assert amoney(None, "UAH") == "—"
    assert anum(1234567) == "1 234 567"
    assert anum("—") == "—"
    assert anum(None) == "None"
    assert adate(datetime(2026, 10, 11, 15, 30)) == "11.10.2026"
    # У базі UTC → показ за Києвом (жовтень: UTC+3)
    assert adate(datetime(2026, 10, 11, 15, 30), with_time=True) == "11.10.2026 18:30"
    assert adate(datetime(2026, 10, 11, 22, 30)) == "12.10.2026"   # перехід через північ за Києвом
    assert adate(None) == "—"


def test_amoney_anum_adate_en(monkeypatch):
    monkeypatch.setattr(i18n, "_storage_get", lambda key, default: "en")
    assert amoney(1234, "UAH") == "₴1,234"
    assert amoney(99.5, "USD") == "$99.50"
    assert anum(1234567) == "1,234,567"
    assert adate(datetime(2026, 10, 11, 15, 30)) == "11 Oct 2026"
    assert adate(datetime(2026, 1, 2, 9, 5), with_time=True) == "2 Jan 2026 11:05"   # січень: UTC+2
