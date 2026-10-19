"""Зібрати справжні відповіді бота (текст + кнопки) для демо-картинок: DATABASE_URL=<тимчасова база> python scripts/demo_bot_messages.py out.json"""
import asyncio, json, logging, os, sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, os.getcwd())
logging.disable(logging.CRITICAL)
from app.database.migrate import migrate
migrate()
from tests.helpers import capture_bot, make_message, make_bot, fsm_for, fsm_storage
from app.bot.handlers import main as h
from app.services.scrapers.scraper import NotinoScraper, ProductData
from app.database.session import get_session
from app.database.models import PriceTracker, PriceHistory, User, PriceEventType
from app.scheduler import price_checker as pc
from app.scheduler import digest as dg
from sqlalchemy import select

def pack(sent):
    return [{"text": s.text, "buttons": [[b.text for b in row] for row in (s.reply_markup.inline_keyboard if s.reply_markup and getattr(s.reply_markup, "inline_keyboard", None) else [])], "kind": s.kind} for s in sent]

async def main(lang):
    out = {}
    uid = 777000111 if lang == "uk" else 777000222
    fsm_storage()
    with capture_bot() as sent:
        await h.cmd_start(make_message(uid, "/start", lang, username="olena_k" if lang == "uk" else "kate_m"))
    out["start"] = pack(sent)

    url = "https://www.notino.ua/biorepair/total-protective-repair-zmicnjujucha-zubna-pasta/"
    product = ProductData(title="Biorepair Total Protective Repair · 75 мл", price=Decimal("250.00"), old_price=Decimal("260.00"),
                          currency="UAH", is_available=True, image_url=None, canonical_url=url, strategy="notino-state")
    with patch.object(NotinoScraper, "scrape", AsyncMock(return_value=product)):
        with capture_bot() as sent:
            await h.cmd_add(make_message(uid, "/add", lang), fsm_for(uid))
            await h.process_url(make_message(uid, url, lang), fsm_for(uid))
            await h.process_target(make_message(uid, "15%", lang), fsm_for(uid))
    out["add"] = pack(sent)

    # Сповіщення про зниження ціни: реальний _send_notification
    bot = make_bot()
    async with get_session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == uid))).scalar_one()
        tr = (await s.execute(select(PriceTracker).where(PriceTracker.user_id == u.id))).scalar_one()
        tr.title = "Кермо і педалі MOZA R5 Racing Bundle for PC" if lang == "uk" else "MOZA R5 Racing Bundle for PC"
        tr.source = "rozetka.com.ua"; tr.canonical_url = "https://rozetka.com.ua/ua/moza-rs20/p521/"
        tr.target_price = None; tr.target_percent = None
        await pc._send_notification(bot, s, tr, PriceEventType.DECREASE, Decimal("26999"), Decimal("20999"))
    kw = bot.send_message.await_args.kwargs
    rm = kw.get("reply_markup")
    out["drop"] = [{"text": kw["text"], "buttons": [[b.text for b in row] for row in (rm.inline_keyboard if rm else [])], "kind": "bot"}]

    # Дайджест: справжній render_digest на історії
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == uid))).scalar_one()
        items = [("Навушники Marshall Major IV Bluetooth", 3249, 2899, 2649), ("Кава в зернах Ferarra Caffe 1 кг", 829, 739, 659),
                 ("DIOR Sauvage · 60 мл", 2970, 2790, 2790), ("Apple AirPods Pro 2 USB-C", 9499, 8999, 8799)]
        for i, (title, old, new, mn) in enumerate(items):
            t = PriceTracker(user_id=u.id, url=f"https://rozetka.com.ua/p/{i}", canonical_url=f"https://rozetka.com.ua/p/{i}",
                             source="rozetka.com.ua", title=title, current_price=Decimal(new), currency="UAH", status="active",
                             created_at=now - timedelta(days=40))
            s.add(t); await s.flush()
            for d, price in ((35, mn), (20, old), (8, old), (0, new)):
                s.add(PriceHistory(tracker_id=t.id, price=Decimal(price), recorded_at=now - timedelta(days=d)))
    async with get_session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == uid))).scalar_one()
        text = await dg.build_digest(u)
    from app.bot.keyboards import digest_keyboard
    kb = digest_keyboard(lang)
    out["digest"] = [{"text": text, "buttons": [[b.text for b in row] for row in kb.inline_keyboard], "kind": "bot"}]
    return out

res = {"uk": asyncio.run(main("uk"))}
res["en"] = asyncio.run(main("en"))
json.dump(res, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
print("ok", {k: {kk: len(vv) for kk, vv in v.items()} for k, v in res.items()})
