"""Локалізація: паритет ключів uk/en, плейсхолдери, t(), resolve_lang, форматування грошей/дат/країн."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from string import Formatter

import pytest

from app import i18n
from app.i18n import (
    BOT_COMMANDS, DEFAULT_LANG, LANGUAGES, country_label, fmt_date, fmt_money, fmt_number, resolve_lang, t,
)


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


# ─── Паритет словників ───────────────────────────────────────────

def test_languages_match_translation_tables():
    assert set(LANGUAGES) == set(i18n._T)
    assert DEFAULT_LANG in i18n._T


def test_all_languages_have_same_keys():
    uk_keys = set(i18n._T["uk"])
    for lang, table in i18n._T.items():
        assert set(table) == uk_keys, f"{lang}: різниця ключів {set(table) ^ uk_keys}"


@pytest.mark.parametrize("key", sorted(i18n._T["uk"]))
def test_placeholders_match_between_languages(key):
    """Кожен ключ має однаковий набір {плейсхолдерів} в усіх мовах — інакше .format() впаде."""
    expected = _placeholders(i18n._T["uk"][key])
    for lang in i18n._T:
        assert _placeholders(i18n._T[lang][key]) == expected, f"{lang}.{key}"


def test_no_empty_translations():
    for lang, table in i18n._T.items():
        for key, value in table.items():
            assert isinstance(value, str) and value.strip(), f"{lang}.{key} порожній"


def test_bot_commands_parity():
    assert set(BOT_COMMANDS) == set(LANGUAGES)
    uk_names = [c for c, _ in BOT_COMMANDS["uk"]]
    assert len(uk_names) == len(set(uk_names)), "дублікати команд"
    for lang, commands in BOT_COMMANDS.items():
        assert [c for c, _ in commands] == uk_names, f"{lang}: інший порядок/набір команд"
        for name, description in commands:
            assert name == name.lower() and name.isidentifier(), name
            assert 1 <= len(description) <= 256, f"{lang}/{name}"


def test_bot_commands_cover_help_text():
    """Кожна команда з меню згадується в /help обома мовами."""
    for lang, commands in BOT_COMMANDS.items():
        help_text = t(lang, "help")
        for name, _ in commands:
            if name == "help":
                continue
            assert f"/{name}" in help_text, f"{lang}: /{name} немає в help"


# ─── t() ─────────────────────────────────────────────────────────

def test_t_returns_translation_and_formats():
    assert t("uk", "cancelled") == "❌ Скасовано."
    assert t("en", "cancelled") == "❌ Cancelled."
    assert t("en", "start_greeting", name="Bob") == "👋 Hi, Bob!"


def test_t_falls_back_to_uk_for_unknown_language():
    assert t("de", "cancelled") == t("uk", "cancelled")
    assert t("", "cancelled") == t("uk", "cancelled")
    assert t(None, "cancelled") == t("uk", "cancelled")  # type: ignore[arg-type]


def test_t_unknown_key_raises():
    with pytest.raises(KeyError):
        t("uk", "definitely_missing_key")
    with pytest.raises(KeyError):
        t("en", "definitely_missing_key")


def test_t_missing_key_in_one_language_falls_back_to_uk(monkeypatch):
    monkeypatch.setitem(i18n._T["uk"], "only_uk_key", "лише uk")
    assert t("en", "only_uk_key") == "лише uk"


# ─── resolve_lang ────────────────────────────────────────────────

@pytest.mark.parametrize("code, expected", [
    (None, "uk"), ("", "uk"),
    ("uk", "uk"), ("UK", "uk"), ("uk-UA", "uk"),
    ("ru", "uk"), ("ru-RU", "uk"), ("be", "uk"),
    ("en", "en"), ("en-US", "en"), ("en-GB", "en"),
    ("de", "en"), ("pl", "en"), ("fr-FR", "en"), ("zh-Hans-CN", "en"),
])
def test_resolve_lang(code, expected):
    assert resolve_lang(code) == expected


# ─── fmt_number / fmt_money ──────────────────────────────────────

@pytest.mark.parametrize("amount, lang, expected", [
    (1234.5, "uk", "1 234,50"),
    (1234.5, "en", "1,234.50"),
    (1234, "uk", "1 234"),
    (1234, "en", "1,234"),
    (Decimal("1000.00"), "uk", "1 000"),          # цілі без копійок
    (Decimal("0.5"), "uk", "0,50"),
    (Decimal("0.5"), "en", "0.50"),
    (Decimal("1234567.891"), "en", "1,234,567.89"),
    (Decimal("1234567.891"), "uk", "1 234 567,89"),
    (5, "en", "5"),
    (5, "de", "5"),                                # невідома мова → en-формат
    (1234.5, "de", "1,234.50"),
])
def test_fmt_number(amount, lang, expected):
    assert fmt_number(amount, lang) == expected


@pytest.mark.parametrize("amount, currency, lang, expected", [
    (1234, "UAH", "uk", "1 234 грн"),
    (1234, "UAH", "en", "₴1,234"),
    (1234.5, "UAH", "uk", "1 234,50 грн"),
    (1234.5, "UAH", "en", "₴1,234.50"),
    (1234, None, "uk", "1 234 грн"),               # None → UAH
    (1234, "", "en", "₴1,234"),
    (1234, "uah", "uk", "1 234 грн"),              # регістр
    (Decimal("299.99"), "USD", "en", "$299.99"),
    (Decimal("299.99"), "USD", "uk", "$299,99"),
    (10, "EUR", "en", "€10"),
    (10, "EUR", "uk", "€10"),
    (99, "PLN", "en", "99 zł"),
    (Decimal("99.90"), "PLN", "uk", "99,90 zł"),
    (50, "GBP", "en", "£50"),
    (100, "CZK", "uk", "100 Kč"),
    (100, "XYZ", "en", "100 XYZ"),                 # невідома валюта → код суфіксом
    (100, "xyz", "uk", "100 XYZ"),
    (Decimal("12.30"), "USD", "en", "$12.30"),
    (Decimal("12.3"), "USD", "uk", "$12,30"),
    (0, "USD", "en", "$0"),
    # невідома мова: число в en-форматі, але суфікс гривні з uk — у коді lang завжди resolve_lang'нутий
    (1234, "UAH", "de", "1,234 грн"),
])
def test_fmt_money(amount, currency, lang, expected):
    assert fmt_money(amount, currency, lang) == expected


def test_fmt_money_none():
    assert fmt_money(None, "UAH", "uk") == "—"
    assert fmt_money(None, "USD", "en") == "—"
    assert fmt_money(None, None, "uk") == "—"


# ─── fmt_date / country_label ────────────────────────────────────

def test_fmt_date():
    assert fmt_date(datetime(2026, 10, 11, 15, 30), "uk") == "11.10.2026"
    assert fmt_date(datetime(2026, 1, 2), "en") == "02.01.2026"
    assert fmt_date(None, "uk") == "—"
    assert fmt_date(None, "en") == "—"


@pytest.mark.parametrize("code, lang, expected", [
    ("UA", "uk", "🇺🇦 Україна"),
    ("UA", "en", "🇺🇦 Ukraine"),
    ("ua", "uk", "🇺🇦 Україна"),
    ("PL", "en", "🇵🇱 Poland"),
    ("GLOBAL", "uk", "🌍 Міжнародні"),
    ("GLOBAL", "en", "🌍 International"),
    ("UA", "de", "🇺🇦 Україна"),               # невідома мова → uk
])
def test_country_label_known(code, lang, expected):
    assert country_label(code, lang) == expected


def test_country_label_unknown_iso_code_gets_flag_and_code():
    label = country_label("ZZ", "uk")
    assert label.endswith(" ZZ")
    assert label.startswith("🇿🇿")


def test_country_label_garbage_code():
    assert country_label("", "uk") == "🏳️"
    assert country_label("EUROPE", "en") == "🏳️ EUROPE"
    assert country_label(None, "uk") == "🏳️"  # type: ignore[arg-type]


def test_every_registry_country_has_a_name():
    """Усі країни з реєстру платформ мають людську назву в обох мовах — інакше /sites показує код."""
    from app.services.scrapers.registry import PLATFORMS
    for p in PLATFORMS:
        for lang in LANGUAGES:
            assert p.country in i18n._COUNTRY_NAMES[lang], f"{p.domain}: країна {p.country} без назви ({lang})"
