"""Telegram-бот без aiogram: long polling + маршрутизация команд/кнопок. Доступ строго для ADMIN_CHAT_ID.

Кнопки: разделы (бесплатные / Cool / Activ / Бронза) с листанием, ❤️ избранное, статус,
«Не интересно», «Очистить раздел», «Очистить всё» (с подтверждением, избранное не трогает).
Скрытые команды: /score N, /turbo, /live, /test [номера], /backup, /digest, /update_auth <op> <cURL|JSON>
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from html import escape

import aiohttp

from config import parse_auth_patch
from notifier import ram_mb
from parsers.base_parser import ASTANA_TZ
from scorer import TEST_NUMBERS, fmt_human, normalize, rank, score
from tg import TgError, btn_cb, kb

log = logging.getLogger("bot")
MAX_AUTH_FILE = 512 * 1024
_CMD = re.compile(r"^/(\w+)(?:@\w+)?\s*(.*)$", re.S)

# Разделы: (заголовок, цена от, цена до)
SECTIONS = {
    "free": ("🆓 Бесплатные (Tele2/Altel)", 0, 0),
    "cool": ("💎 Cool 4 500 ₸", 4500, 4500),
    "activ": ("📶 Activ/Kcell 500 ₸", 500, 500),
    "bronze": ("🥉 Activ/Kcell Бронза 5 500 ₸", 5500, 5500),
}
PAGE = 15
_MENU_ROWS = (
    [btn_cb("🆓 Бесплатные", "list:free:0"), btn_cb("💎 Cool 4 500", "list:cool:0")],
    [btn_cb("📶 Activ 500", "list:activ:0"), btn_cb("🥉 Бронза 5 500", "list:bronze:0")],
    [btn_cb("❤️ Избранное", "favs"), btn_cb("📊 Статус", "status")],
    [btn_cb("🗑 Очистить всё", "clear:all")],
)
STATUS_KB = kb(*_MENU_ROWS)
DIGEST_KB = STATUS_KB
CONFIRM_ALL_KB = kb([btn_cb("✅ Да, очистить всё", "clearok:all"), btn_cb("Отмена", "menu")])


