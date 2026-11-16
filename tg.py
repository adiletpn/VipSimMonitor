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

    async def call(self, method: str, _form: aiohttp.FormData | None = None, _http_timeout: float | None = None,
                   **params):
        kw = {"data": _form} if _form is not None else {"json": {k: v for k, v in params.items() if v is not None}}
        if _http_timeout:
            kw["timeout"] = aiohttp.ClientTimeout(total=_http_timeout)
        async with self.session.post(self.base + method, **kw) as resp:
            body = await resp.json(content_type=None)
        if body.get("ok"):
            return body.get("result")
        extra = body.get("parameters") or {}
        if body.get("error_code") == 429 and "retry_after" in extra:
            raise RetryAfter(int(extra["retry_after"]))
        raise TgError(f"{body.get('error_code')}: {body.get('description')}")

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None, silent: bool = False):
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                               reply_markup=reply_markup, disable_notification=silent or None,
                               link_preview_options={"is_disabled": True})

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
        return await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                               parse_mode="HTML", reply_markup=reply_markup,
                               link_preview_options={"is_disabled": True})

