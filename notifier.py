"""Очередь уведомлений с приоритетом бесплатных номеров и защитой от флуд-лимитов Telegram."""
import asyncio
import itertools
import logging
import resource
import sys
from html import escape

import aiohttp

from scorer import fmt_human, fmt_plain, rank
from tg import RetryAfter, TgBot, TgError, btn_cb, btn_url, kb as make_kb

log = logging.getLogger("notifier")
SEND_PAUSE = 0.7


def ram_mb() -> int:
    """Пиковый RSS процесса. Linux отдаёт КБ, macOS — байты."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss // 1024 // 1024 if sys.platform == "darwin" else rss // 1024


def price_label(price: int | None) -> str:
    if price is None:
        return "❔ не указана (проверь на витрине)"
    if price == 0:
        return "🆓 БЕСПЛАТНО (0 ₸)"
    return f"💸 {price:,} ₸".replace(",", " ")


def build_card(phone: str, operator_title: str, total: int, price: int | None,
               rules: list[str], shop_url: str) -> tuple[str, dict | None]:
    lines = [
        f"<b>{rank(total)}</b> · <b>{total}</b>/100",
        "",
        f"📱 <b>{fmt_human(phone)}</b>",
        f"📋 <code>{fmt_plain(phone)}</code>",
        "",
        f"📡 Оператор: <b>{escape(operator_title)}</b>",
        f"💰 Цена: {price_label(price)}",
        "🧮 Почему красивый:",
        *(f"  • {escape(r)}" for r in rules),
        "",
        "⚡ <i>Жми кнопку → выбери eSIM → ИИН → подтверждение. 60 секунд, номера разбирают быстро.</i>",
    ]
    kb = None
    if shop_url:
        kb = make_kb([btn_url("⚡ ЗАБРАТЬ НОМЕР", shop_url)],
                     [btn_cb("❤️ Нравится", f"fav:{phone}"), btn_cb("🗑 Не интересно", f"hide:{phone}")])
    return "\n".join(lines), kb


class Notifier:
    __slots__ = ("bot", "chat_id", "queue", "_seq", "sent", "dropped")

    def __init__(self, bot: TgBot, chat_id: int, maxsize: int = 300):
        self.bot = bot
        self.chat_id = chat_id
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)
        self._seq = itertools.count()
        self.sent = 0
        self.dropped = 0

    def push(self, text: str, kb=None, free: bool = False, total: int = 0, silent: bool = False) -> None:
        """Бесплатные и самые дорогие по баллам уходят первыми."""
        item = ((0 if free else 1), -total, next(self._seq), text, kb, silent)
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("Очередь уведомлений переполнена — сообщение отброшено")

    async def _send(self, text: str, kb, silent: bool) -> None:
        for attempt in range(5):
            try:
                await self.bot.send_message(self.chat_id, text, reply_markup=kb, silent=silent)
                self.sent += 1
                return
            except RetryAfter as e:
                log.warning("Флуд-лимит Telegram: жду %s c", e.retry_after)
                await asyncio.sleep(e.retry_after + 1)
            except (TgError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error("Ошибка Telegram API (попытка %d): %s", attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        self.dropped += 1

