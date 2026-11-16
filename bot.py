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


# ---------- команды ----------
async def cmd_start(app, msg, args):
    names = ", ".join(p.TITLE for p in app.parsers.values())
    await app.bot.send_message(
        msg["chat"]["id"],
        f"👋 Я круглосуточно ищу красивые номера <b>{names}</b>.\n\n"
        f"Как только найду подходящий — пришлю его сюда с кнопкой <b>«⚡ ЗАБРАТЬ НОМЕР»</b>, "
        "она откроет оформление именно этого номера. Каждый вечер в 23:00 — итог дня.\n\n"
        "Кнопки ниже — списки пойманных номеров по разделам, которые сейчас есть на витрине.",
        reply_markup=STATUS_KB)


async def cmd_status(app, msg, args):
    await app.bot.send_message(msg["chat"]["id"], await status_text(app), reply_markup=STATUS_KB)


async def cmd_digest(app, msg, args):
    await app.bot.send_message(msg["chat"]["id"], await digest_text(app), reply_markup=DIGEST_KB)


async def cmd_score(app, msg, args):
    if not args.isdigit():
        text = f"Сейчас порог: <b>{app.cfg.min_score}</b>. Пример: <code>/score 65</code>"
    else:
        await app.cfg.set_min_score(int(args))
        text = f"✅ Теперь присылаю номера от <b>{app.cfg.min_score}</b> баллов"
    await app.bot.send_message(msg["chat"]["id"], text)


async def cmd_turbo(app, msg, args):
    if app.cfg.turbo:
        await app.cfg.set_turbo(0)
        text = "🐢 Турбо выключено"
    else:
        await app.cfg.set_turbo(3600)
        text = "🚀 Турбо на 1 час: проверяю в 2 раза чаще"
    await app.bot.send_message(msg["chat"]["id"], text)


async def cmd_live(app, msg, args):
    await app.cfg.set_live(not app.cfg.live)
    app.pulse_mark = (time.time(), app.total_scanned())
    await app.bot.send_message(msg["chat"]["id"], "💓 Тихий отчёт раз в 15 минут включён" if app.cfg.live
                               else "🔕 Отчёты выключены")


async def cmd_test(app, msg, args):
    out = []
    for raw in (args.split() or TEST_NUMBERS)[:10]:
        d = normalize(raw)
        if not d:
            out.append(f"❌ {escape(raw)} — не мобильный номер Казахстана")
            continue
        total, rules = score(d)
        out.append(f"<b>{fmt_human(d)}</b> → <b>{total}</b> {rank(total)}")
        out.extend(f"  • {escape(r)}" for r in rules)
    await app.bot.send_message(msg["chat"]["id"], "\n".join(out))


async def cmd_backup(app, msg, args):
    tmp = app.cfg.db_path + ".backup"
    try:
        await app.db.backup_to(tmp)
        stamp = datetime.now(ASTANA_TZ).strftime("%Y%m%d_%H%M")
        await app.bot.send_document(msg["chat"]["id"], tmp, f"vip_sim_{stamp}.db",
                                    f"💾 Бэкап · {os.path.getsize(tmp) // 1024} КБ · RAM {ram_mb()} МБ")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def cmd_update_auth(app, msg, args):
    chat = msg["chat"]["id"]
    name, _, body = args.partition(" ")
    name = name.lower()
    if name not in app.parsers:
        await app.bot.send_message(chat, f"Формат: <code>/update_auth &lt;{'|'.join(app.parsers)}&gt; "
                                         "&lt;cURL или JSON&gt;</code>")
        return
    doc = msg.get("document")
    if doc:
        if (doc.get("file_size") or 0) > MAX_AUTH_FILE:
            await app.bot.send_message(chat, "Файл слишком большой (лимит 512 КБ)")
            return
        body = (await app.bot.download(doc["file_id"], MAX_AUTH_FILE)).decode("utf-8", "replace")
    try:
        patch = parse_auth_patch(body)
    except (ValueError, json.JSONDecodeError) as e:
        await app.bot.send_message(chat, f"❌ Не разобрал: {escape(str(e))}")
        return
    op = await app.cfg.update_operator(name, patch)
    parser = app.parsers[name]
    parser.sleep_until = parser.last_refresh = 0.0
    parser.trips = parser.net_errors = 0
    parser._targets, parser._idx = [], 0
    if parser.session:
        parser.session.cookie_jar.clear()
    try:
        await app.bot.delete_message(chat, msg["message_id"])  # в сообщении могли быть куки/токены
    except TgError:
        pass
    await app.bot.send_message(chat, f"✅ {escape(parser.TITLE)} обновлён: {op['method']} "
                                     f"<code>{escape(op['url'][:200])}</code>")


COMMANDS = {
    "start": cmd_start, "status": cmd_status, "stats": cmd_status, "digest": cmd_digest, "score": cmd_score,
    "turbo": cmd_turbo, "live": cmd_live, "test": cmd_test, "backup": cmd_backup, "update_auth": cmd_update_auth,
}


