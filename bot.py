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


def _fav_rows(rows, back: str) -> list:
    """Кнопки ❤️/🤍 по номерам (по 2 в ряд). back — куда перерисовать после нажатия."""
    return [[btn_cb(f"{'❤️' if r[5] else '🤍'} {fmt_human(r[0])}", f"fav:{r[0]}:{back}")] for r in rows]


# ---------- тексты ----------
def _ago(ts: int) -> str:
    m = max(0, int(time.time() - ts) // 60)
    return f"{m} мин назад" if m < 60 else f"{m // 60} ч назад"


def _state(p) -> str:
    s = p.state
    if s.startswith("🟢"):
        return "🟢 работает"
    if s.startswith("🔴"):
        return "😴 пауза (сайт попросил притормозить) — " + s.split("сон ", 1)[-1].split(" (")[0]
    if s.startswith("⏳"):
        return "⏳ запускается"
    return s


def _num_link(app, ph: str, op: str, pr) -> str:
    p = next((x for x in app.parsers.values() if x.TITLE == op), None)
    link = p.number_link(ph, pr) if p else ""
    return f'<a href="{escape(link)}">{fmt_human(ph)}</a>' if link else fmt_human(ph)


def _need(app, price_from: int) -> int:
    return app.cfg.vip_min_score if price_from >= 10000 else app.cfg.min_score


