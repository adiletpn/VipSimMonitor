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


