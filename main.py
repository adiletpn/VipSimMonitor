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


