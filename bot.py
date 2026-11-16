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


async def status_text(app) -> str:
    s = await app.db.stats()
    lines = [
        "<b>📊 Статус</b>",
        f"Присылаю номера до {app.cfg.max_price} ₸ от <b>{app.cfg.min_score}</b> баллов"
        + (" · 🚀 турбо" if app.cfg.turbo else ""),
        "",
        f"Сегодня проверено: <b>{s['scanned_day']}</b> номеров, найдено: <b>{s['catches_day']}</b>",
        f"За всё время найдено: <b>{s['catches_all']}</b>",
        "",
    ]
    for p in app.parsers.values():
        lines.append(f"{escape(p.TITLE)} — {escape(_state(p))} · проверено {p.scanned}")
    best = await app.db.best_available(app.cfg.min_score)
    if best:
        lines.append("\n<b>🏆 Лучшие, что сейчас на витрине</b> (нажми — откроется оформление):")
        for ph, op, sc, pr, seen in best:
            price = "0 ₸" if not pr else f"{pr:,} ₸".replace(",", " ")
            lines.append(f"{_num_link(app, ph, op, pr)} · <b>{sc}</b> · {escape(op)} · {price}")
    return "\n".join(lines)


async def section_view(app, key: str, page: int = 0) -> tuple[str, dict]:
    title, lo, hi = SECTIONS[key]
    need = _need(app, lo)
    total = await app.db.count_available(lo, hi, need)
    rows = await app.db.available(lo, hi, need, limit=PAGE, offset=page * PAGE)
    soft_note = " (+ номера с двойками)" if key == "cool" else ""
    lines = [f"<b>{title}</b> · от {_need(app, lo)} баллов{soft_note}",
             f"<i>Доступно на витрине (видел за 12 ч): {total}</i>", ""]
    if not rows:
        lines.append("Пока пусто — как только поймаю, появятся здесь.")
    for i, (ph, op, sc, pr, seen, fav, soft) in enumerate(rows, page * PAGE + 1):
        mark = "двойки" if soft and sc < _need(app, lo) else f"<b>{sc}</b>"
        lines.append(f"{i}. {'❤️ ' if fav else ''}{_num_link(app, ph, op, pr)} · {mark} · {escape(op)} · {_ago(seen)}")
    lines.append("\nНажми на номер — откроется оформление. Кнопка 🤍 с номером — в избранное.")
    nav = []
    if page > 0:
        nav.append(btn_cb("◀ Назад", f"list:{key}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(btn_cb("Ещё ▶", f"list:{key}:{page + 1}"))
    keyboard = _fav_rows(rows, f"{key}.{page}") + ([nav] if nav else []) + \
        [[btn_cb("🗑 Очистить этот раздел", f"clear:{key}")], *_MENU_ROWS]
    return "\n".join(lines), {"inline_keyboard": keyboard}


async def favs_view(app) -> tuple[str, dict]:
    rows = await app.db.favorites()
    lines = ["<b>❤️ Избранное</b>", ""]
    if not rows:
        lines.append("Пусто. Жми «❤️ Нравится» на карточке или 🤍 в списках.")
    fresh = time.time() - 12 * 3600
    for ph, op, sc, pr, seen, fav, soft in rows:
        state = f"✅ на витрине ({_ago(seen)})" if seen >= fresh else "⚠️ давно не видел — возможно, уже продан"
        lines.append(f"{_num_link(app, ph, op, pr)} · {escape(op)} · {state}")
    keyboard = _fav_rows(rows, "favs") + [*_MENU_ROWS]
    return "\n".join(lines), {"inline_keyboard": keyboard}


async def digest_text(app) -> str:
    """Итог дня: всё пойманное, что ещё висит на витрине, по разделам."""
    lines = ["<b>🌙 Итог дня</b> — пойманные номера, которые сейчас есть на витрине", ""]
    total = 0
    for title, lo, hi in SECTIONS.values():
        rows = await app.db.available(lo, hi, _need(app, lo), hours=24, limit=10)
        if not rows:
            continue
        n = await app.db.count_available(lo, hi, _need(app, lo), hours=24)
        total += n
        lines.append(f"<b>{title}</b> — {n} шт.")
        lines.extend(f"{'❤️ ' if fav else ''}{_num_link(app, ph, op, pr)} · "
                     f"{'двойки' if soft and sc < _need(app, lo) else f'<b>{sc}</b>'} · {escape(op)}"
                     for ph, op, sc, pr, _, fav, soft in rows)
        if n > len(rows):
            lines.append(f"… и ещё {n - len(rows)} — кнопка раздела ниже")
        lines.append("")
    if not total:
        lines.append("За сегодня ничего достойного не поймал — охота продолжается.")
    return "\n".join(lines)


