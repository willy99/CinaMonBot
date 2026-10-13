"""
Захист від SSRF: бот ходить за посиланнями користувачів, тож без перевірки будь-хто міг би змусити його
звернутися до localhost (адмінка, інші сервіси на машині), локальної мережі чи метаданих хмари
(169.254.169.254) і побачити шматок відповіді в назві/ціні «товару».

Дозволені лише http(s) і хости, ВСІ адреси яких публічні (ipaddress.is_global). Перевірка йде на кожен
запит: httpx — event hook (зокрема кожен крок редиректу), Playwright — route на кожен запит сторінки.

Залишковий ризик — DNS rebinding між перевіркою і з'єднанням (TTL≈0); для нього потрібен власний резолвер
у транспорті. Для бота на домашній машині прийнятно; на сервері з чутливою мережею — додати егрес-фаєрвол.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".arpa")
_CACHE_TTL = 300.0
_cache: dict[str, tuple[float, bool]] = {}


class ForbiddenTarget(Exception):
    """URL веде на внутрішню / приватну адресу."""


def _ip_public(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def host_is_public(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return _ip_public(host.strip("[]"))
    except ValueError:
        pass
    cached = _cache.get(host)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        addrs = await _resolve(host)
        ok = bool(addrs) and all(_ip_public(a) for a in addrs)
    except (socket.gaierror, UnicodeError, OSError):
        ok = False                       # не резолвиться — далі все одно впаде як network
    _cache[host] = (now, ok)
    return ok


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise ForbiddenTarget(url) from None
    if parsed.scheme not in ALLOWED_SCHEMES or not await host_is_public(parsed.hostname or ""):
        raise ForbiddenTarget(url)


async def httpx_request_hook(request) -> None:
    """httpx event_hooks["request"]: викликається на кожен запит, зокрема на кожен редирект."""
    await check_url(str(request.url))


def clear_cache() -> None:
    _cache.clear()
