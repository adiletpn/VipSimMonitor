"""Очередь уведомлений с приоритетом бесплатных номеров и защитой от флуд-лимитов Telegram."""
import asyncio
import collections
import itertools
import time
import logging
import resource
import sys
from html import escape

import aiohttp

from scorer import LEVEL_NAME, fmt_human, fmt_plain
from tg import RetryAfter, TgBot, TgError, btn_cb, btn_url, kb as make_kb

log = logging.getLogger("notifier")
SEND_PAUSE = 0.7
CARDS_PER_HOUR = 5     # больше — уже спам: остальные ТОП-номера уходят одним списком раз в час
BUNDLE_MAX_LINES = 40


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
        f"<b>{LEVEL_NAME.get(total, '📱 НОМЕР')}</b>",
        "",
        f"📱 <b>{fmt_human(phone)}</b>",
        f"📋 <code>{fmt_plain(phone)}</code>",
        "",
        f"📡 Оператор: <b>{escape(operator_title)}</b>",
        f"💰 Цена: {price_label(price)}",
        "✨ Чем хорош:",
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
    __slots__ = ("bot", "chat_id", "queue", "_seq", "sent", "dropped", "_card_times", "bundle")

    def __init__(self, bot: TgBot, chat_id: int, maxsize: int = 300):
        self.bot = bot
        self.chat_id = chat_id
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)
        self._seq = itertools.count()
        self.sent = 0
        self.dropped = 0
        self._card_times: collections.deque = collections.deque()
        self.bundle: list[str] = []

    def push_top(self, text: str, kb, line: str, free: bool = False) -> None:
        """Карточка ТОП-номера, но не больше CARDS_PER_HOUR в час; лишние копятся в общий список."""
        now = time.monotonic()
        while self._card_times and now - self._card_times[0] > 3600:
            self._card_times.popleft()
        if len(self._card_times) < CARDS_PER_HOUR:
            self._card_times.append(now)
            self.push(text, kb, free=free, total=100)
        else:
            self.bundle.append(line)

    async def bundle_loop(self, stop: asyncio.Event) -> None:
        """Раз в час — одно сообщение со всеми ТОП-номерами, не влезшими в лимит карточек."""
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=3600)
                return
            except asyncio.TimeoutError:
                pass
            if not self.bundle:
                continue
            lines, self.bundle = self.bundle, []
            head = f"🔥 <b>Ещё ТОП-номера за час: {len(lines)}</b> (нажми — откроется оформление)\n"
            body = "\n".join(lines[:BUNDLE_MAX_LINES])
            tail = f"\n… и ещё {len(lines) - BUNDLE_MAX_LINES} — в разделах бота" if len(lines) > BUNDLE_MAX_LINES else ""
            self.push(head + body + tail, None, total=100)

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

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            try:
                await self._send(item[3], item[4], item[5])
            except Exception:
                log.exception("Сбой отправки уведомления")
            finally:
                self.queue.task_done()
            await asyncio.sleep(SEND_PAUSE)

    async def drain(self, timeout: float = 10) -> None:
        """При остановке пытаемся дослать то, что уже в очереди."""
        try:
            while not self.queue.empty():
                item = self.queue.get_nowait()
                await asyncio.wait_for(self._send(item[3], item[4], item[5]), timeout=timeout)
                self.queue.task_done()
                await asyncio.sleep(SEND_PAUSE)
        except (asyncio.TimeoutError, Exception) as e:
            log.warning("Досылка очереди прервана: %s", e)
