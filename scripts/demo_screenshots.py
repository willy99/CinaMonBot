"""
Скріншоти адмінки на демо-базі (scripts/demo_db.py) — для LinkedIn, презентацій, README.

    DEMO_URL=http://127.0.0.1:8093 DEMO_ADMIN_PASSWORD=... DEMO_USER_ID=<id юзера з MOZA> \
        venv/bin/python scripts/demo_screenshots.py marketing/screenshots

Адмінку треба запустити окремо на демо-базі (порт не 8088 — там живий сервіс).
PNG @2x, 1440×900; для кожної сторінки — вікно і (де корисно) повна сторінка.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("DEMO_URL", "http://127.0.0.1:8093")
PASSWORD = os.environ["DEMO_ADMIN_PASSWORD"]
USER_ID = os.environ.get("DEMO_USER_ID", "")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "marketing/screenshots")


def settle(page: Page, ms: int = 1800) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(ms)      # ECharts анімація + refreshable-секції


def shot(page: Page, name: str, full: bool = False) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print("saved", path)


def login(page: Page) -> None:
    page.goto(f"{BASE}/login")
    page.wait_for_selector("input")
    inputs = page.locator("input")
    inputs.nth(0).fill("demo")
    inputs.nth(1).fill(PASSWORD)
    inputs.nth(1).press("Enter")
    page.wait_for_url(f"{BASE}/", timeout=15000)
    settle(page)


def capture_set(page: Page, suffix: str) -> None:
    page.goto(f"{BASE}/"); settle(page, 2500)
    shot(page, f"01-dashboard-{suffix}")
    shot(page, f"01-dashboard-{suffix}-full", full=True)

    page.goto(f"{BASE}/platforms"); settle(page)
    shot(page, f"02-platforms-{suffix}")

    if USER_ID:
        page.goto(f"{BASE}/users/{USER_ID}"); settle(page)
        shot(page, f"03-user-card-{suffix}")
        row = page.locator("tr", has_text="MOZA")
        if row.count():
            row.first.locator("i", has_text="show_chart").first.click()
            page.wait_for_selector(".q-dialog", timeout=8000)
            page.wait_for_timeout(1800)
            shot(page, f"04-price-history-{suffix}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

    page.goto(f"{BASE}/notifications"); settle(page)
    shot(page, f"05-notifications-{suffix}")

    page.goto(f"{BASE}/feedback"); settle(page)
    shot(page, f"06-feedback-{suffix}")

    page.goto(f"{BASE}/system"); settle(page, 2500)
    shot(page, f"07-system-{suffix}")


def switch(page: Page, lang_label: str, toggle_theme: bool) -> None:
    page.goto(f"{BASE}/"); settle(page, 800)
    page.get_by_role("button", name=lang_label, exact=True).first.click()
    page.wait_for_timeout(1500)
    if toggle_theme:
        # Кнопка теми — іконка сонця/місяця поруч із перемикачем мови
        btn = page.locator("button:has(i:text-matches('^(light_mode|dark_mode)$'))").first
        btn.click()
        page.wait_for_timeout(1500)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="uk-UA")
        page = ctx.new_page()
        login(page)
        capture_set(page, "uk-dark")
        switch(page, "EN", toggle_theme=True)
        capture_set(page, "en-light")
        browser.close()


if __name__ == "__main__":
    main()
