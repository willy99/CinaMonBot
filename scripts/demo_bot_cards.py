"""
Картинки чату з ботом (стиль Telegram, темна тема) зі справжніх відповідей бота.

    venv/bin/python scripts/demo_bot_messages.py /tmp/bot.json      # тексти й кнопки з реальних обробників
    venv/bin/python scripts/demo_bot_cards.py /tmp/bot.json marketing/bot

Блок контактів у /start (email, адреса) прибирається — картинки йдуть у соцмережі.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "marketing/bot")
NAMES = {"uk": "Олена", "en": "Kate"}
LINK = "https://www.notino.ua/biorepair/total-protective-repair-zmicnjujucha-zubna-pasta/"
T = {
    "uk": {"bot": "бот", "time": ["09:41", "09:42", "09:42", "09:43", "18:20", "10:00"]},
    "en": {"bot": "bot", "time": ["09:41", "09:42", "09:42", "09:43", "18:20", "10:00"]},
}

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b0f14; font-family: -apple-system, "SF Pro Text", "Segoe UI", Roboto, sans-serif; }
.phone { width: 430px; background: #0e1621; border-radius: 28px; overflow: hidden; border: 1px solid #1c2733; }
.head { display: flex; align-items: center; gap: 12px; padding: 14px 16px; background: #17212b; }
.ava { width: 42px; height: 42px; border-radius: 50%; background: linear-gradient(135deg,#3987e5,#7c3aed);
       color: #fff; font-weight: 700; display: flex; align-items: center; justify-content: center; font-size: 19px; }
.name { color: #fff; font-weight: 600; font-size: 16px; }
.sub { color: #7f91a4; font-size: 13px; margin-top: 2px; }
.chat { padding: 14px 10px 18px; display: flex; flex-direction: column; gap: 8px;
        background: radial-gradient(circle at 20% 10%, #132233 0, #0e1621 60%); }
.msg { max-width: 86%; padding: 8px 11px 6px; border-radius: 14px; color: #f2f5f8; font-size: 15px; line-height: 1.38;
       position: relative; word-wrap: break-word; }
.in { background: #182533; align-self: flex-start; border-bottom-left-radius: 4px; }
.out { background: #2b5278; align-self: flex-end; border-bottom-right-radius: 4px; }
.time { color: #8fa4b8; font-size: 11.5px; text-align: right; margin-top: 3px; }
.out .time { color: #a9c3dc; }
.msg a { color: #71b8f5; text-decoration: none; }
.out a { color: #cfe6ff; }
.msg code { background: rgba(255,255,255,.08); padding: 0 4px; border-radius: 4px; font-family: ui-monospace, Menlo, monospace; font-size: 13.5px; }
.msg s { color: #9aa9b8; }
.kb { align-self: flex-start; width: 86%; display: flex; flex-direction: column; gap: 5px; margin-top: -3px; }
.kb .row { display: flex; gap: 5px; }
.kb .btn { flex: 1; text-align: center; background: rgba(43,82,120,.45); color: #e8f1fa; font-size: 14px; font-weight: 500;
           padding: 9px 6px; border-radius: 10px; }
.date { align-self: center; color: #c8d4e0; background: rgba(15,25,35,.7); font-size: 13px; padding: 3px 10px; border-radius: 12px; margin: 4px 0; }
"""


def clean(text: str, lang: str) -> str:
    text = text.replace("Tester", NAMES[lang])
    # Контакти/виконавець — не для соцмереж
    text = re.split(r"\n━━━", text)[0].rstrip()
    return text.replace("\n", "<br>")


def bubble(html: str, side: str, time: str) -> str:
    return f'<div class="msg {side}">{html}<div class="time">{time}</div></div>'


def keyboard(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    inner = "".join('<div class="row">' + "".join(f'<div class="btn">{b}</div>' for b in row) + "</div>" for row in rows)
    return f'<div class="kb">{inner}</div>'


def page(lang: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<div class='phone' id='phone'><div class='head'><div class='ava'>Ц</div><div>"
            f"<div class='name'>ЦінаБот</div><div class='sub'>@cina_mon_bot · {T[lang]['bot']}</div></div></div>"
            f"<div class='chat'>{body}</div></div></body></html>")


def scenes(data: dict, lang: str) -> dict[str, str]:
    tm = T[lang]["time"]
    d = data[lang]
    start = d["start"][0]
    add = d["add"]
    s = {}
    s["01-start"] = bubble("/start", "out", tm[0]) + bubble(clean(start["text"], lang), "in", tm[0]) + keyboard(start["buttons"])
    prompt, _checking, card, added = add
    s["02-add"] = (bubble("/add", "out", tm[1]) + bubble(clean(prompt["text"], lang), "in", tm[1]) +
                   bubble(f'<a>{LINK}</a>', "out", tm[2]) + bubble(clean(card["text"], lang), "in", tm[2]) +
                   bubble("15%", "out", tm[3]) + bubble(clean(added["text"], lang), "in", tm[3]))
    drop = d["drop"][0]
    s["03-price-drop"] = bubble(clean(drop["text"], lang), "in", tm[4]) + keyboard(drop["buttons"])
    dig = d["digest"][0]
    s["04-digest"] = bubble(clean(dig["text"], lang), "in", tm[5]) + keyboard(dig["buttons"])
    return s


def main() -> None:
    data = json.loads(SRC.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 470, "height": 1200}, device_scale_factor=3)
        p = ctx.new_page()
        for lang in ("uk", "en"):
            for name, body in scenes(data, lang).items():
                p.set_content(page(lang, body))
                p.wait_for_timeout(150)
                path = OUT / f"{name}-{lang}.png"
                p.locator("#phone").screenshot(path=str(path))
                print("saved", path)
        browser.close()


if __name__ == "__main__":
    main()
