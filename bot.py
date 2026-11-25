"""Telegram-бот без aiogram: long polling + маршрутизация команд/кнопок. Доступ строго для ADMIN_CHAT_ID.

Кнопки: разделы (бесплатные / Cool / Activ / Бронза) с листанием, ❤️ избранное, статус,
«Не интересно», «Очистить раздел», «Очистить всё» (с подтверждением, избранное не трогает).
Скрытые команды: /turbo, /live, /test [номера], /backup, /digest, /update_auth <op> <cURL|JSON>
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
from scorer import LEVEL_NAME, NICE, TEST_NUMBERS, TOP, classify, fmt_human, normalize
from tg import TgError, btn_cb, kb

log = logging.getLogger("bot")
MAX_AUTH_FILE = 512 * 1024
_CMD = re.compile(r"^/(\w+)(?:@\w+)?\s*(.*)$", re.S)

# Разделы: (заголовок, цена от, цена до)
SECTIONS = {
    "cool": ("💎 Cool 4 500 ₸", 4500, 4500),
    "bronze": ("🥉 Activ/Kcell Бронза 5 500 ₸", 5500, 5500),
}
PAGE = 15
_MENU_ROWS = (
    [btn_cb("💎 Cool 4 500", "list:cool:0"), btn_cb("🥉 Бронза 5 500", "list:bronze:0")],
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
    return NICE  # в списках — 🔥 ТОП и 💎 КРАСИВЫЕ (+ двойки в Cool и избранное)


def _mark(level: int, soft: int) -> str:
    return "🔥" if level >= TOP else "💎" if level >= NICE else "двойки"


async def status_text(app) -> str:
    s = await app.db.stats()
    lines = [
        "<b>📊 Статус</b>",
        f"Присылаю только <b>🔥 ТОП-номера</b> (до {app.cfg.max_price} ₸). 💎 Красивые — в разделах и итоге дня"
        + (" · 🚀 турбо" if app.cfg.turbo else ""),
        "",
        f"Сегодня проверено: <b>{s['scanned_day']}</b> номеров, найдено: <b>{s['catches_day']}</b>",
        f"За всё время найдено: <b>{s['catches_all']}</b>",
        "",
    ]
    for p in app.parsers.values():
        lines.append(f"{escape(p.TITLE)} — {escape(_state(p))} · проверено {p.scanned}")
    best = await app.db.best_available(NICE)
    if best:
        lines.append("\n<b>🏆 Лучшие, что сейчас на витрине</b> (нажми — откроется оформление):")
        for ph, op, sc, pr, seen in best:
            price = "0 ₸" if not pr else f"{pr:,} ₸".replace(",", " ")
            lines.append(f"{_mark(sc, 0)} {_num_link(app, ph, op, pr)} · {escape(op)} · {price}")
    return "\n".join(lines)


async def section_view(app, key: str, page: int = 0) -> tuple[str, dict]:
    title, lo, hi = SECTIONS[key]
    need = _need(app, lo)
    total = await app.db.count_available(lo, hi, need)
    rows = await app.db.available(lo, hi, need, limit=PAGE, offset=page * PAGE)
    soft_note = ""
    lines = [f"<b>{title}</b> · 🔥 ТОП и 💎 красивые{soft_note}",
             f"<i>Доступно на витрине (видел за 12 ч): {total}</i>", ""]
    if not rows:
        lines.append("Пока пусто — как только поймаю, появятся здесь.")
    for i, (ph, op, sc, pr, seen, fav, soft) in enumerate(rows, page * PAGE + 1):
        mark = _mark(sc, soft)
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
                     f"{_mark(sc, soft)} · {escape(op)}"
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
        "Сюда приходят только <b>🔥 ТОП-номера</b>: тройка в хвосте (891-88-85, 58-88, 77-77), блок наоборот кода "
"(708 807), в ритм коду (708 808) или как код (707 707) — с кнопкой <b>«⚡ ЗАБРАТЬ НОМЕР»</b>. Не больше 5 карточек в час, "
        "остальные — одним списком. 💎 Красивые попроще — в разделах, итог дня — в 23:00.\n\n"
        "Кнопки ниже — списки пойманных номеров по разделам, которые сейчас есть на витрине.",
        reply_markup=STATUS_KB)


async def cmd_status(app, msg, args):
    await app.bot.send_message(msg["chat"]["id"], await status_text(app), reply_markup=STATUS_KB)


async def cmd_digest(app, msg, args):
    await app.bot.send_message(msg["chat"]["id"], await digest_text(app), reply_markup=DIGEST_KB)


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
        level, rules = classify(d)
        out.append(f"<b>{fmt_human(d)}</b> → {LEVEL_NAME.get(level, '— обычный')}")
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
    "start": cmd_start, "status": cmd_status, "stats": cmd_status, "digest": cmd_digest,
    "turbo": cmd_turbo, "live": cmd_live, "test": cmd_test, "backup": cmd_backup, "update_auth": cmd_update_auth,
}


# ---------- кнопки ----------
async def _edit_or_send(app, chat: int, mid: int, text: str, keyboard: dict, send: bool = True) -> None:
    try:
        await app.bot.edit_message_text(chat, mid, text, reply_markup=keyboard)
    except TgError as e:
        if "not modified" in str(e):
            return
        if send:
            await app.bot.send_message(chat, text, reply_markup=keyboard)


async def on_callback(app, cq: dict) -> None:
    data = cq.get("data") or ""
    msg = cq["message"]
    chat, mid = msg["chat"]["id"], msg["message_id"]
    if data == "status":
        await app.bot.answer_callback(cq["id"])
        await app.bot.send_message(chat, await status_text(app), reply_markup=STATUS_KB)
    elif data == "menu":
        await app.bot.answer_callback(cq["id"])
        await _edit_or_send(app, chat, mid, "Меню 👇", STATUS_KB)
    elif data.startswith("list:"):
        _, key, *rest = data.split(":")
        if key in SECTIONS:
            await app.bot.answer_callback(cq["id"])
            text, keyboard = await section_view(app, key, int(rest[0]) if rest else 0)
            if rest and rest[0] != "0":  # листание — в том же сообщении
                await _edit_or_send(app, chat, mid, text, keyboard)
            else:
                await app.bot.send_message(chat, text, reply_markup=keyboard)
    elif data == "favs":
        await app.bot.answer_callback(cq["id"])
        text, keyboard = await favs_view(app)
        await app.bot.send_message(chat, text, reply_markup=keyboard)
    elif data.startswith("fav:"):
        _, phone, *back = data.split(":")
        on = await app.db.toggle_fav(phone)
        await app.bot.answer_callback(cq["id"], "Добавил в избранное ❤️" if on else "Убрал из избранного")
        if back:  # нажали в списке — перерисовываем, чтобы сердечки обновились
            if back[0] == "favs":
                text, keyboard = await favs_view(app)
            else:
                key, page = back[0].split(".")
                text, keyboard = await section_view(app, key, int(page))
            await _edit_or_send(app, chat, mid, text, keyboard, send=False)
    elif data.startswith("hide:"):
        await app.db.hide(data[5:])
        await app.bot.answer_callback(cq["id"], "Убрал — больше не покажу")
        try:
            await app.bot.delete_message(chat, mid)
        except TgError:
            pass
    elif data == "clear:all":
        await app.bot.answer_callback(cq["id"])
        await app.bot.send_message(chat, "Очистить <b>все</b> пойманные номера? Избранное ❤️ останется.",
                                   reply_markup=CONFIRM_ALL_KB)
    elif data.startswith("clear:") or data == "clearok:all":
        key = data.split(":", 1)[1]
        if key == "all":
            n = await app.db.hide_range(0, 10 ** 9)
        elif key in SECTIONS:
            n = await app.db.hide_range(SECTIONS[key][1], SECTIONS[key][2])
        else:
            await app.bot.answer_callback(cq["id"])
            return
        await app.bot.answer_callback(cq["id"], f"Убрал {n} номеров")
        try:
            await app.bot.edit_message_text(chat, mid, f"🗑 Очищено: {n} номеров. Избранное не тронуто. "
                                                       "Новые находки появятся снова.", reply_markup=STATUS_KB)
        except TgError:
            pass
    else:
        await app.bot.answer_callback(cq["id"])


# ---------- приём обновлений ----------
async def handle_update(app, upd: dict) -> None:
    admin = app.cfg.admin_chat_id
    if "callback_query" in upd:
        cq = upd["callback_query"]
        if (cq.get("message") or {}).get("chat", {}).get("id") != admin:
            return
        await on_callback(app, cq)
        return
    msg = upd.get("message")
    if not msg or msg.get("chat", {}).get("id") != admin:
        return
    m = _CMD.match(msg.get("text") or msg.get("caption") or "")
    if m and m.group(1).lower() in COMMANDS:
        await COMMANDS[m.group(1).lower()](app, msg, m.group(2).strip())


async def polling(app, stop: asyncio.Event) -> None:
    """Long polling getUpdates. Подтверждённый offset хранится в app.tg_offset (досылается при остановке)."""
    bot = app.bot
    await bot.call("deleteWebhook", drop_pending_updates=False)
    me = await bot.call("getMe")
    log.info("Поллинг запущен: @%s", me.get("username"))
    while not stop.is_set():
        try:
            updates = await bot.call("getUpdates", offset=app.tg_offset, timeout=25,
                                     allowed_updates=["message", "callback_query"], _http_timeout=40)
        except asyncio.CancelledError:
            raise
        except TgError as e:  # 409 Conflict при перекрытии деплоев и т.п.
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(5)
            continue
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("getUpdates сеть: %r", e)
            await asyncio.sleep(3)
            continue
        for upd in updates or []:
            app.tg_offset = upd["update_id"] + 1
            try:
                await handle_update(app, upd)
            except Exception:
                log.exception("Ошибка обработки апдейта")
