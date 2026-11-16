"""Точка входа: пул парсеров + очередь уведомлений + Telegram-бот + обслуживание. Graceful shutdown по SIGTERM/SIGINT."""
import asyncio
import gc
import logging
import os
import signal
import time

from datetime import datetime

import aiohttp

from bot import DIGEST_KB, digest_text, polling, status_text
from config import Config
from database import Database
from notifier import Notifier, ram_mb
from parsers import PARSERS
from parsers.base_parser import ASTANA_TZ
from scorer import doubles, score
from tg import TgBot, TgError, btn_cb, kb

log = logging.getLogger("main")
PULSE_EVERY = 15 * 60
MAINT_EVERY = 10 * 60
CLEANUP_EVERY = 24 * 3600


class App:
    __slots__ = ("cfg", "db", "bot", "notifier", "parsers", "connector", "started", "pulse_mark", "tg_offset")

    def __init__(self, cfg, db, bot, notifier, connector):
        self.cfg = cfg
        self.db = db
        self.bot = bot
        self.notifier = notifier
        self.connector = connector
        self.parsers = {}
        self.started = time.time()
        self.pulse_mark = (time.time(), 0)
        self.tg_offset: int | None = None

    def total_scanned(self) -> int:
        return sum(p.scanned for p in self.parsers.values())


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    """True — пора выходить."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def maintenance(app: App, stop: asyncio.Event) -> None:
    last_cleanup = 0.0
    if await _wait(stop, 60):
        return
    while not stop.is_set():
        try:
            if time.time() - last_cleanup >= CLEANUP_EVERY:
                deleted = await app.db.cleanup()
                last_cleanup = time.time()
                log.info("Автоочистка: удалено %d номеров старше 30 дней", deleted)
            else:
                await app.db.checkpoint()
            gc.collect()
            log.info("Обслуживание: RAM %d МБ, очередь %d", ram_mb(), app.notifier.queue.qsize())
        except Exception:
            log.exception("Ошибка обслуживания")
        if await _wait(stop, MAINT_EVERY):
            return


async def pulse(app: App, stop: asyncio.Event) -> None:
    while not await _wait(stop, 30):
        ts, scanned = app.pulse_mark
        if not app.cfg.live or time.time() - ts < PULSE_EVERY:
            continue
        now_scanned = app.total_scanned()
        minutes = max(1e-6, (time.time() - ts) / 60)
        delta = now_scanned - scanned
        app.pulse_mark = (time.time(), now_scanned)
        app.notifier.push(f"💓 <b>Пульс</b>: за {minutes:.0f} мин просеяно {delta} номеров "
                          f"({delta / minutes:.1f}/мин)\n\n{await status_text(app)}", silent=True, total=-1)


async def digest(app: App, stop: asyncio.Event) -> None:
    """Раз в день (DIGEST_HOUR по Астане, по умолчанию 23:00) присылает итог дня."""
    hour = int(os.getenv("DIGEST_HOUR", "23"))
    while not await _wait(stop, 60):
        now = datetime.now(ASTANA_TZ)
        today = now.strftime("%Y-%m-%d")
        if now.hour != hour or (await app.db.get_settings()).get("digest_day") == today:
            continue
        try:
            await app.bot.send_message(app.cfg.admin_chat_id, await digest_text(app), reply_markup=DIGEST_KB)
            await app.db.set_setting("digest_day", today)
        except Exception:
            log.exception("Не удалось отправить итог дня")


async def soft_summary(app: App, stop: asyncio.Event) -> None:
    """Раз в час — одно сообщение о новых номерах с двойками в Cool (вместо десятков отдельных карточек)."""
    since = int(time.time())
    while not await _wait(stop, 3600):
        now = int(time.time())
        try:
            rows = await app.db.soft_since(since)
            since = now
            n = sum(c for _, c in rows)
            if n:
                app.notifier.push(f"💎 <b>За час в Cool +{n} новых номеров с двойками</b>\n"
                                  "(двойка в конце / в середине / в блоке). Список — по кнопке, понравившиеся "
                                  "отмечай 🤍.", kb([btn_cb("💎 Открыть Cool", "list:cool:0")]), total=-1)
        except Exception:
            log.exception("Сводка по двойкам не отправлена")


