"""Минимальный клиент Telegram Bot API на aiohttp (замена aiogram: −~100 МБ RAM, без pydantic)."""
import logging
import os

import aiohttp

log = logging.getLogger("tg")
API_URL = os.getenv("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")


class TgError(Exception):
    pass


class RetryAfter(TgError):
    def __init__(self, seconds: int):
        super().__init__(f"flood control, retry after {seconds}s")
        self.retry_after = seconds


def kb(*rows) -> dict:
    return {"inline_keyboard": [list(r) for r in rows]}


def btn_url(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def btn_cb(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


class TgBot:
    __slots__ = ("token", "base", "session")

    def __init__(self, token: str, connector: aiohttp.BaseConnector):
        self.token = token
        self.base = f"{API_URL}/bot{token}/"
        self.session = aiohttp.ClientSession(connector=connector, connector_owner=False,
                                             timeout=aiohttp.ClientTimeout(total=40))

