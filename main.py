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


