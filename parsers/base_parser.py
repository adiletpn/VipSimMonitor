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

from scorer import NICE, TOP, classify, doubles, normalize, price_ok
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


class Blocked(Exception):
    def __init__(self, status: int, retry_after: str | None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class HttpError(Exception):
    pass


def astana_hour() -> int:
    return datetime.now(ASTANA_TZ).hour


def _fill(obj, ctx: dict):
    """Рекурсивная подстановка {mask}/{page}/{offset}/{size}. Строка ровно '{page}' становится int."""
    if isinstance(obj, str):
        if obj in ctx:
            return ctx[obj]
        if "{" in obj:
            for k, v in ctx.items():
                obj = obj.replace(k, str(v))
        return obj
    if isinstance(obj, dict):
        return {k: _fill(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, ctx) for v in obj]
    return obj


def _to_price(v) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"[^\d.,]", "", v).replace(",", ".")
        try:
            return int(float(digits)) if digits else None
        except ValueError:
            return None
    if isinstance(v, dict):
        for k in ("value", "amount", "price", "sum", "current"):
            if k in v:
                return _to_price(v[k])
    return None


class BaseParser:
    NAME = "base"
    TITLE = "Base"
    HOME = ""
    PHONE_KEYS = ("msisdn", "phone", "phonenumber", "phone_number", "number", "ctn", "value")

    __slots__ = ("cfg", "app_cfg", "db", "notifier", "connector", "session", "ua", "sleep_until",
                 "trips", "net_errors", "last_refresh", "state", "requests", "scanned", "fresh",
                 "caught", "last_ok", "_targets", "_idx", "cat_prices", "device_id")

    def __init__(self, app_cfg, db, notifier, connector: aiohttp.TCPConnector):
        self.app_cfg = app_cfg
        self.cfg = app_cfg.operators[self.NAME]
        self.db = db
        self.notifier = notifier
        self.connector = connector
        self.session: aiohttp.ClientSession | None = None
        self.ua = random.choice(USER_AGENTS)  # один UA на всю «жизнь» сессии, как у живого человека
        self.sleep_until = 0.0
        self.trips = 0
        self.net_errors = 0
        self.last_refresh = 0.0
        self.state = "⏳ старт"
        self.requests = self.scanned = self.fresh = self.caught = 0
        self.last_ok = 0.0
        self._targets: list[tuple[str, str, int, str]] = []
        self._idx = 0
        self.cat_prices: dict = {}  # id категории -> цена (если API отдаёт номер без цены)
        self.device_id = str(uuid.uuid4())  # постоянный «ID устройства», как у реального браузера

    # ---------- служебное ----------
    @property
    def shop_url(self) -> str:
        return self.cfg.get("shop_url") or self.HOME

    def _origin(self) -> str:
        src = self.cfg.get("warmup_url") or self.shop_url or self.cfg.get("url", "")
        p = urlsplit(src)
        return f"{p.scheme}://{p.netloc}" if p.netloc else ""

    def _base_headers(self, navigate: bool) -> dict:
        origin = self._origin()
        h = {
            "User-Agent": self.ua,
            "Accept-Language": "ru-RU,ru;q=0.9,kk;q=0.8",
            "Referer": (self.shop_url or origin + "/"),
        }
        if navigate:
            h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                                "image/webp,*/*;q=0.8",
                      "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document",
                      "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"})
        else:
            h.update({"Accept": "application/json, text/plain, */*", "Origin": origin,
                      "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})
        return h

    def _api_headers(self, ctx: dict | None = None) -> dict:
        """Браузерные заголовки + заголовки оператора с подстановкой {city}/{uuid}."""
        if ctx is None:
            ctx = self._ctx("", "", int(self.cfg.get("page_start", 1)))
        headers = self._base_headers(False)
        headers.update({k: str(_fill(v, ctx)) for k, v in (self.cfg.get("headers") or {}).items()
                        if not k.startswith(":")})
        return headers

    def _ctx(self, prefix: str, mask: str, page: int, category: str = "") -> dict:
        c = self.cfg
        size = int(c.get("page_size", 50))
        cities = c.get("cities") or [""]
        return {"{prefix}": prefix, "{mask}": mask, "{page}": page,
                "{offset}": (page - int(c.get("page_start", 1))) * size, "{size}": size,
                "{city}": random.choice(cities), "{uuid}": self.device_id, "{category}": category}

    def number_link(self, phone: str, price: int | None) -> str:
        tpl = self.cfg.get("number_url")
        if not tpl:
            return self.shop_url
        return tpl.replace("{phone}", phone).replace("{price}", str(price or 0))

    def configured(self) -> bool:
        return bool(self.cfg.get("enabled") and self.cfg.get("url"))

    def next_delay(self) -> float:
        """Человеческий джиттер: турбо 10–15 c, ночь (01–05 Астана) 45–60 c, обычно 20–35 c."""
        if self.app_cfg.turbo:
            return random.uniform(10.0, 15.0)
        if 1 <= astana_hour() < 5:
            return random.uniform(45.0, 60.0)
        return random.uniform(20.0, 35.0)

    def _build_targets(self) -> list[tuple[str, str, int, str]]:
        c = self.cfg
        blob = json.dumps([c.get("url"), c.get("params"), c.get("payload")], ensure_ascii=False)
        masks = list(c.get("masks") or [""]) if "{mask}" in blob else [""]
        prefixes = list(c.get("prefixes") or [""]) if "{prefix}" in blob else [""]
        cats = list(c.get("categories") or [""]) if "{category}" in blob else [""]
        start = int(c.get("page_start", 1))
        pages = range(start, start + max(1, int(c.get("pages", 1))))
        if "{page}" not in blob and "{offset}" not in blob:
            pages = range(start, start + 1)
        by_cat = c.get("masks_by_category") or {}  # у категории может быть свой набор масок
        targets = [(pr, m, p, cat) for cat in cats for m in (by_cat.get(cat) or masks)
                   for pr in prefixes for p in pages]
        random.shuffle(targets)
        return targets

    def _next_target(self) -> tuple[str, str, int, str]:
        if self._idx >= len(self._targets):
            if self._targets:
                gc.collect()  # полный круг масок/страниц пройден — подчищаем память
            self._targets = self._build_targets()
            self._idx = 0
        t = self._targets[self._idx]
        self._idx += 1
        return t

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                connector=self.connector, connector_owner=False,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=25, connect=10))
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    async def _read_limited(resp: aiohttp.ClientResponse) -> bytes:
        if resp.content_length and resp.content_length > MAX_BODY:
            raise HttpError(f"ответ {resp.content_length} байт — слишком большой")
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            buf += chunk
            if len(buf) > MAX_BODY:
                raise HttpError("ответ больше лимита")
        return bytes(buf)

    # ---------- сеть ----------
    async def refresh_session(self) -> None:
        """Лёгкий GET на главную витрины — обновляет сессионные куки, как обычный заход в браузере."""
        url = self.cfg.get("warmup_url") or self.HOME
        self.last_refresh = time.monotonic()
        if not url:
            return
        session = await self._ensure_session()
        try:
            async with session.get(url, headers=self._base_headers(True), cookies=self.cfg.get("cookies") or None,
                                   proxy=self.app_cfg.proxy_url, allow_redirects=True) as resp:
                await self._read_limited(resp)
                logging.getLogger(self.NAME).info("Прогрев кук: %s -> %s", url, resp.status)
        except Exception as e:
            logging.getLogger(self.NAME).warning("Прогрев кук не удался: %s", e)
        await self._load_categories(session)

    async def _load_categories(self, session: aiohttp.ClientSession) -> None:
        """Справочник категорий {id: цена}: нужен, когда в списке номеров есть только id категории."""
        url = self.cfg.get("categories_url")
        if not url:
            return
        headers = self._api_headers()
        try:
            async with session.get(url, headers=headers, cookies=self.cfg.get("cookies") or None,
                                   proxy=self.app_cfg.proxy_url) as resp:
                data = json.loads(await self._read_limited(resp))
            items = data.get("data", data) if isinstance(data, dict) else data
            self.cat_prices = {it["id"]: _to_price(it.get("price")) for it in items
                               if isinstance(it, dict) and "id" in it}
        except Exception as e:
            logging.getLogger(self.NAME).warning("Категории не загружены: %s", e)

    async def fetch(self, prefix: str, mask: str, page: int, category: str = "") -> bytes:
        c = self.cfg
        ctx = self._ctx(prefix, mask, page, category)
        headers = self._api_headers(ctx)
        kw = {"headers": headers, "cookies": c.get("cookies") or None, "proxy": self.app_cfg.proxy_url,
              "params": _fill(c.get("params") or {}, ctx) or None}
        method = (c.get("method") or "GET").upper()
        payload = c.get("payload")
        if method != "GET" and payload is not None:
            payload = _fill(payload, ctx)
            ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "").lower()
            if "x-www-form-urlencoded" in ctype and isinstance(payload, dict):
                kw["data"] = {k: str(v) for k, v in payload.items()}
            else:
                kw["json"] = payload
        session = await self._ensure_session()
        self.requests += 1
        async with session.request(method, _fill(c["url"], ctx), **kw) as resp:
            if resp.status in (403, 429):
                raise Blocked(resp.status, resp.headers.get("Retry-After"))
            if resp.status >= 400:
                raise HttpError(f"HTTP {resp.status}")
            return await self._read_limited(resp)

    # ---------- разбор ответа ----------
    def _phone_from_dict(self, node: dict) -> str | None:
        for k, v in node.items():
            if str(k).lower() in self.PHONE_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool):
                ph = normalize(v)
                if ph:
                    return ph
        for v in node.values():
            if isinstance(v, (str, int)) and not isinstance(v, bool):
                ph = normalize(v)
                if ph:
                    return ph
        return None

    def _price_from_dict(self, node: dict) -> int | None:
        for k, v in node.items():
            lk = str(k).lower()
            if lk in ("isfree", "free", "is_free") and v is True:
                return 0
            if any(p in lk for p in _PRICE_KEYS) and not any(s in lk for s in _PRICE_SKIP):
                pr = _to_price(v)
                if pr is not None:
                    return pr
        cat = node.get("category", node.get("categoryId"))
        if cat is not None and cat in self.cat_prices:
            return self.cat_prices[cat]
        return None

    def extract(self, body: bytes) -> dict[str, int | None]:
        """Универсальный разбор JSON любой вложенности (или HTML/текста): {номер: цена|None}."""
        text = body.decode("utf-8", "replace")
        out: dict[str, int | None] = {}
        try:
            data = json.loads(text)
        except ValueError:
            for m in _TEXT_PHONE.findall(text):
                ph = normalize(m)
                if ph:
                    out.setdefault(ph, None)
            return out
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                ph = self._phone_from_dict(node)
                if ph:
                    pr = self._price_from_dict(node)
                    old = out.get(ph)
                    out[ph] = pr if old is None else (old if pr is None else min(old, pr))
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                for v in node:
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    elif isinstance(v, (str, int)) and not isinstance(v, bool):
                        ph = normalize(v)
                        if ph:
                            out.setdefault(ph, None)
        return out

    async def process(self, found: dict[str, int | None]) -> None:
        scored = {ph: classify(ph) for ph in found}
        fresh = await self.db.register(self.NAME, [(ph, pr, scored[ph][0]) for ph, pr in found.items()])
        await self.db.touch_catches(list(found))
        self.scanned += len(found)
        self.fresh += len(fresh)
        cfg = self.app_cfg
        soft_prices = self.cfg.get("soft_prices") or ()
        soft = []
        # 🔥 ТОП — сразу карточкой; 💎 КРАСИВЫЙ — в списки и итог дня; двойки в Cool — только в список.
        # Дубли отсекает таблица catches (номер, который уже присылали, второй раз не придёт).
        for ph, (level, rules) in scored.items():
            price = found[ph]
            if not price_ok(price, cfg.max_price):
                continue
            if level == TOP:
                if not await self.db.add_catch(ph, self.TITLE, level, price, rules):
                    continue
                self.caught += 1
                text, kb = build_card(ph, self.TITLE, level, price, rules, self.number_link(ph, price))
                self.notifier.push(text, kb, free=(price == 0), total=level)
            elif level == NICE:
                soft.append((ph, level, price, rules))
            elif price in soft_prices:
                d = doubles(ph)
                if d:
                    soft.append((ph, level, price, d))
        if soft:
            await self.db.add_soft(self.TITLE, soft)

    # ---------- Circuit Breaker ----------
    def _trip(self, reason: str, retry_after: str | None = None) -> float:
        self.trips += 1
        pause = min(600.0, 300.0 * 1.5 ** (self.trips - 1) + random.uniform(0, 60))
        if retry_after and retry_after.isdigit():
            pause = max(pause, min(600.0, float(retry_after)))
        self.sleep_until = time.monotonic() + pause
        wake = datetime.now(ASTANA_TZ) + timedelta(seconds=pause)
        self.state = f"🔴 сон до {wake:%H:%M} ({reason})"
        logging.getLogger(self.NAME).warning("Circuit Breaker: %s — пауза %.0f c", reason, pause)
        if self.trips == 1:
            self.notifier.push(f"🛡 <b>{self.TITLE}</b>: {reason} — тихий сон {pause / 60:.0f} мин, "
                               f"остальные операторы работают.", silent=True, total=-1)
        return pause

    # ---------- главный цикл ----------
    async def run(self, stop: asyncio.Event) -> None:
        log = logging.getLogger(self.NAME)
        await _sleep(stop, random.uniform(1, 15))  # разнесём старт операторов во времени
        while not stop.is_set():
            if not self.configured():
                self.state = "⚪ не настроен (/update_auth)"
                await _sleep(stop, 30)
                continue
            if self.sleep_until > time.monotonic():
                await _sleep(stop, self.sleep_until - time.monotonic())
                if self.session:
                    self.session.cookie_jar.clear()
                self.last_refresh = 0.0
                continue
            if time.monotonic() - self.last_refresh > self.app_cfg.refresh_every:
                await self.refresh_session()
                await _sleep(stop, random.uniform(2, 6))
            target = self._next_target()
            try:
                found = self.extract(await self.fetch(*target))
                await self.process(found)
                self.trips = self.net_errors = 0
                self.last_ok = time.time()
                self.state = f"🟢 OK · {len(found)} в ответе" if found else "🟡 пустой ответ (маска/кука?)"
            except Blocked as e:
                self._trip(f"HTTP {e.status}", e.retry_after)
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, HttpError) as e:
                self.net_errors += 1
                self.state = f"🟠 ошибка: {str(e)[:60] or type(e).__name__}"
                log.warning("Сетевая ошибка #%d: %r", self.net_errors, e)
                if self.net_errors >= NET_ERRORS_TO_TRIP:
                    self.net_errors = 0
                    self._trip(f"{NET_ERRORS_TO_TRIP} сетевые ошибки подряд")
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state = f"🟠 сбой: {type(e).__name__}"
                log.exception("Необработанная ошибка парсера")
            await _sleep(stop, self.next_delay())


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
