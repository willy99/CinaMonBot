"""Показ часу: у базі UTC, людям — Europe/Kyiv (app/timeutil.py)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.timeutil import KYIV, fmt, kyiv_date_key, kyiv_day_start_utc, to_kyiv, utc_range_for_kyiv_days


def test_naive_is_treated_as_utc_and_shifted():
    # Літній час: UTC+3
    assert fmt(datetime(2026, 7, 1, 21, 30)) == "02.07.2026 00:30"
    # Зимовий час: UTC+2
    assert fmt(datetime(2026, 1, 15, 22, 30)) == "16.01.2026 00:30"


def test_aware_datetime_is_converted_not_shifted_twice():
    aware = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    assert fmt(aware, "%H:%M") == "15:00"
    kyiv = datetime(2026, 10, 11, 15, 0, tzinfo=KYIV)
    assert fmt(kyiv, "%H:%M") == "15:00"


def test_none_and_default():
    assert fmt(None) == "—" and fmt(None, default="") == ""
    assert to_kyiv(None) is None


def test_date_key_crosses_midnight_in_kyiv():
    # 22:30 UTC 10 жовтня = 01:30 11 жовтня за Києвом → належить добі 11.10
    assert kyiv_date_key(datetime(2026, 10, 10, 22, 30)) == "2026-10-11"
    assert kyiv_date_key(datetime(2026, 10, 10, 20, 30)) == "2026-10-10"


def test_day_start_utc():
    # Початок київської доби 11.10 (UTC+3) = 21:00 UTC 10.10
    assert kyiv_day_start_utc(datetime(2026, 10, 11).date()) == datetime(2026, 10, 10, 21, 0)


def test_range_keys_are_consecutive_and_end_today():
    since, keys = utc_range_for_kyiv_days(7)
    assert len(keys) == 7 and keys[-1] == datetime.now(KYIV).strftime("%Y-%m-%d")
    assert kyiv_date_key(since) == keys[0]
