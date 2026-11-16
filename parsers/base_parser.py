"""Базовый парсер: ротация масок/страниц, человеческие паузы, прогрев кук, прокси, вежливый Circuit Breaker."""
import asyncio
import gc
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import aiohttp

from scorer import doubles, normalize, price_ok, score
from notifier import build_card

try:
    from zoneinfo import ZoneInfo
    ASTANA_TZ = ZoneInfo("Asia/Almaty")
except Exception:  # нет tzdata в образе — Казахстан живёт по UTC+5
    ASTANA_TZ = timezone(timedelta(hours=5))

MAX_BODY = 3 * 1024 * 1024
NET_ERRORS_TO_TRIP = 3
_TEXT_PHONE = re.compile(r"(?:\+?7|8)?[\s\-(]*7\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_PRICE_SKIP = ("old", "discount", "before", "full", "max", "min")
_PRICE_KEYS = ("price", "cost", "amount", "sum", "fee", "стоим", "цена")

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Mobile/15E148 Safari/604.1",
)


