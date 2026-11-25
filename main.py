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
from scorer import classify, doubles
from tg import TgBot, TgError

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


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s")

    cfg = Config()
    db = Database(cfg.db_path)
    await db.connect()
    await cfg.sync(db)
    # Баллы уловов из прошлых версий скорера пересчитываются текущим (иначе в списках висят устаревшие оценки)
    def _rescore(ph, soft):
        level, rules = classify(ph)
        return level, rules or (doubles(ph) if soft else [])
    log.info("Пересчитано уловов: %d", await db.rescore(_rescore))

    connector = aiohttp.TCPConnector(limit=15, ttl_dns_cache=300, enable_cleanup_closed=True)
    bot = TgBot(cfg.bot_token, connector)
    notifier = Notifier(bot, cfg.admin_chat_id)
    app = App(cfg, db, bot, notifier, connector)
    app.parsers = {name: cls(cfg, db, notifier, connector) for name, cls in PARSERS.items()}

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(p.run(stop), name=f"parser:{n}") for n, p in app.parsers.items()]
    tasks += [asyncio.create_task(notifier.run(stop), name="notifier"),
              asyncio.create_task(maintenance(app, stop), name="maintenance"),
              asyncio.create_task(pulse(app, stop), name="pulse"),
              asyncio.create_task(digest(app, stop), name="digest"),
              asyncio.create_task(notifier.bundle_loop(stop), name="bundle")]
    poller = asyncio.create_task(polling(app, stop), name="polling")
    poller.add_done_callback(lambda _: stop.set())

    active = [p.TITLE for p in app.parsers.values() if p.configured()]
    notifier.push(f"🟢 <b>VIP SIM Monitor запущен</b>\nАктивны: {', '.join(active) or 'никто — настрой /update_auth'}"
                  f"\nПорог {cfg.min_score} · БД {cfg.db_path} · RAM {ram_mb()} МБ", silent=True, total=-1)
    log.info("Старт: операторы %s, RAM %d МБ", active, ram_mb())

    await stop.wait()
    log.info("Получен сигнал остановки — завершаюсь аккуратно")

    # 1. Обрываем long-poll getUpdates и подтверждаем обработанные апдейты (без Conflict и без повторов)
    poller.cancel()
    # 2. Гасим парсеры и фоновые задачи
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, poller, return_exceptions=True)
    if isinstance(results[-1], Exception) and not isinstance(results[-1], asyncio.CancelledError):
        log.error("Поллинг упал: %r", results[-1])
    if app.tg_offset:
        try:
            await bot.call("getUpdates", offset=app.tg_offset, timeout=0, _http_timeout=5)
        except (TgError, aiohttp.ClientError, asyncio.TimeoutError):
            pass
    # 3. Досылаем уже пойманные номера
    await notifier.drain(timeout=5)
    # 4. Закрываем HTTP-сессии, коннектор, бота и БД (со сбросом WAL)
    for p in app.parsers.values():
        await p.close()
    await bot.close()
    await connector.close()
    await db.close()
    log.info("Остановлено чисто")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
