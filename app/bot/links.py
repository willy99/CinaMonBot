"""
Посилання на товари з довільного тексту: кнопка «Поділитися» в застосунку магазину,
підпис до фото, кілька посилань одним повідомленням, файл .txt/.csv.

    urls = urls_from_message(message)          # з тексту або підпису, з урахуванням entities
    urls = extract_urls("Дивись: https://rozetka.com.ua/ua/x/p1/ і ще https://comfy.ua/y")

Правила:
- беремо http(s)-посилання за регуляркою + приховані посилання Telegram (text_link) +
  посилання без схеми, які Telegram сам розпізнав як url ("rozetka.com.ua/…" → https://);
- прибираємо розділові знаки в кінці (крапка, кома, дужка, «»);
- відкидаємо посилання на Telegram (t.me, telegram.me) — це не товари;
- без дублікатів, у порядку появи, не більше `limit`.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"'«»\]\[{}|\\^`]+", re.I)
_TRAILING = ".,;:!?)]}…»\"'’”"
_SKIP_HOSTS = ("t.me", "telegram.me", "telegram.dog")
MAX_FILE_BYTES = 256 * 1024


def _clean(url: str) -> str | None:
    url = url.strip()
    # Розділові знаки в кінці — не частина адреси. Закриваючу дужку лишаємо, якщо вона парна:
    # "…/wiki/X_(Y)" — частина адреси, "(див. https://…/x/)" — ні
    while url and url[-1] in _TRAILING:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = host[4:] if host.startswith("www.") else host
    if host in _SKIP_HOSTS or any(host.endswith("." + h) for h in _SKIP_HOSTS):
        return None
    return url


def extract_urls(text: str | None, entities: list[Any] | None = None, limit: int = 50) -> list[str]:
    if not text and not entities:
        return []
    text = text or ""
    found: list[tuple[int, str]] = [(m.start(), m.group(0)) for m in URL_RE.finditer(text)]
    for e in entities or []:
        etype = getattr(e, "type", None)
        etype = getattr(etype, "value", etype)
        if etype == "text_link" and getattr(e, "url", None):
            found.append((getattr(e, "offset", 0), e.url))
        elif etype == "url":
            try:
                raw = e.extract_from(text)
            except Exception:  # noqa: BLE001 — зламані офсети не мають валити хендлер
                continue
            if raw and not re.match(r"https?://", raw, re.I):
                found.append((getattr(e, "offset", 0), raw))
    found.sort(key=lambda x: x[0])
    out: list[str] = []
    seen: set[str] = set()
    for _, raw in found:
        url = _clean(raw)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
            if len(out) >= limit:
                break
    return out


def urls_from_message(message: Any, limit: int = 50) -> list[str]:
    """Посилання з тексту повідомлення або з підпису до фото/файлу."""
    if getattr(message, "text", None):
        return extract_urls(message.text, getattr(message, "entities", None), limit)
    if getattr(message, "caption", None):
        return extract_urls(message.caption, getattr(message, "caption_entities", None), limit)
    return []


def is_link_list_file(document: Any) -> bool:
    """Текстовий файл зі списком посилань (.txt / .csv, до 256 КБ)."""
    if document is None:
        return False
    name = (getattr(document, "file_name", "") or "").lower()
    mime = (getattr(document, "mime_type", "") or "").lower()
    size = getattr(document, "file_size", 0) or 0
    text_like = name.endswith((".txt", ".csv")) or mime in ("text/plain", "text/csv")
    return text_like and 0 < size <= MAX_FILE_BYTES
