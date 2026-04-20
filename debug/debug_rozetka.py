"""
Запусти: python debug_rozetka.py
Покаже що реально знаходить парсер на сторінці Rozetka.
"""
import asyncio
import httpx
from bs4 import BeautifulSoup
import re

URL = "https://rozetka.com.ua/ua/apple-iphone-17-pro-max-256gb-silver-mfym4af-a/p543550380/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}

async def debug():
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        resp = await client.get(URL)
        print(f"Status: {resp.status_code}")
        print(f"Final URL: {resp.url}")
        print()

        soup = BeautifulSoup(resp.text, "lxml")

        # ── Шукаємо заголовок ──────────────────────────────────
        print("=== H1 tags ===")
        for h1 in soup.find_all("h1")[:5]:
            print(f"  class={h1.get('class')} | text={h1.get_text(strip=True)[:80]}")

        print()
        print("=== Теги з 'title' в class ===")
        for tag in soup.find_all(class_=re.compile(r"title", re.I))[:5]:
            print(f"  <{tag.name}> class={tag.get('class')} | text={tag.get_text(strip=True)[:80]}")

        # ── Шукаємо ціну ───────────────────────────────────────
        print()
        print("=== Теги з 'price' в class ===")
        for tag in soup.find_all(class_=re.compile(r"price", re.I))[:8]:
            print(f"  <{tag.name}> class={tag.get('class')} | text={tag.get_text(strip=True)[:60]}")

        print()
        print("=== data-testid атрибути ===")
        for tag in soup.find_all(attrs={"data-testid": True})[:10]:
            print(f"  <{tag.name}> testid={tag['data-testid']} | text={tag.get_text(strip=True)[:60]}")

        # ── Зберігаємо HTML для ручного огляду ─────────────────
        with open("rozetka_debug.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        print()
        print("✅ HTML збережено у rozetka_debug.html")
        print(f"   Розмір: {len(resp.text):,} символів")

asyncio.run(debug())