"""Налаштування «на льоту»: перекриття в app_settings, межі, кеш, скидання до .env."""
from __future__ import annotations

import pytest

from botkit import runtime_settings as rt


@pytest.fixture
def specs(kit):
    rt.register({"CHECK_MINUTES": rt.Spec(int, 15, 1440, "monitoring", "unit_minutes"),
                 "DELAY_SECONDS": rt.Spec(float, 0.5, 30, "scraping", "unit_seconds")},
                ("monitoring", "scraping"))
    yield
    rt.register({}, ())


async def test_override_cache_and_reset(specs):
    assert await rt.get("CHECK_MINUTES") == 60                      # дефолт із settings
    assert await rt.set_value("CHECK_MINUTES", "90", by="pavel") == 90
    assert await rt.get("CHECK_MINUTES") == 90 and rt.cached("CHECK_MINUTES") == 90
    snap = {s["key"]: s for s in await rt.snapshot()}
    assert snap["CHECK_MINUTES"]["overridden"] and snap["CHECK_MINUTES"]["updated_by"] == "pavel"
    assert snap["DELAY_SECONDS"]["kind"] == "float" and not snap["DELAY_SECONDS"]["overridden"]
    await rt.reset_value("CHECK_MINUTES")
    assert await rt.get("CHECK_MINUTES") == 60


@pytest.mark.parametrize("raw", ["5", "2000", "abc", "1.5"])
async def test_out_of_range_rejected(specs, raw):
    with pytest.raises(ValueError):
        await rt.set_value("CHECK_MINUTES", raw)


async def test_unregistered_key_reads_settings(specs):
    assert await rt.get("USER_REQUESTS_PER_MINUTE") == 20
    with pytest.raises(KeyError):
        await rt.set_value("USER_REQUESTS_PER_MINUTE", 5)
