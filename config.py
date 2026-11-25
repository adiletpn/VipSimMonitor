"""Конфигурация: базовые значения из os.environ + динамические оверрайды из SQLite (таблица settings)."""
import json
import logging
import os
import shlex
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

log = logging.getLogger("config")

OPERATORS = ("tele2", "altel", "activ", "beeline")

# Ключи запроса, которые автоматически превращаются в плейсхолдеры при импорте cURL
PAGE_KEYS = frozenset(("page", "pagenumber", "page_number", "pagenum", "pageindex", "page_index", "p"))
OFFSET_KEYS = frozenset(("offset", "skip", "from", "start"))
MASK_KEYS = frozenset(("mask", "pattern", "search", "query", "q", "searchmask", "numbermask",
                       "number_mask", "template", "searchstring", "search_string", "msisdnmask"))
SKIP_HEADERS = frozenset(("content-length", "host", "connection", "accept-encoding", "cookie"))

DEFAULT_MASKS = ("", "777", "707", "000", "555", "111", "999", "888", "1234", "0000", "7777")

# Готовые настройки для витрин на платформе eshop (Tele2 и Altel, найдено в JS магазина).
# Свободные номера: category=-1 («Обычный», 0 ₸). Без prefix API отдаёт пустой список, max 100 номеров на ответ.
_ESHOP_MASKS = ["", "777", "000", "111", "222", "333", "444", "555", "666", "888", "999", "707",
                "1234", "2345", "0707", "1010", "2020", "5050", "9090", "0000"]


def _eshop(domain: str, operator_type: str, prefixes: list) -> dict:
    return {
        "url": f"https://{domain}/eshop/catalog/numbers",
        "method": "GET",
        "headers": {"clientType": "WEB", "platform": "web", "operatorType": operator_type,
                    "appVersion": "1.90.0", "city": "{city}"},
        # Выдача случайная и зависит от города: на каждый запрос берём случайный из 22 городов
        "cities": ["68", "73", "95", "69", "71", "74", "75", "76", "93", "91", "78", "79", "80", "81",
                   "96", "85", "87", "72", "88", "89", "90", "70"],
        "params": {"prefix": "{prefix}", "category": "{category}", "mask": "{mask}"},
        # -1 = «Обычный» (0 ₸), 101 = «Cool» (4 500 ₸). VIP (100, 14 500 ₸) выключен по решению владельца.
        "categories": ["101"],  # только Cool: в бесплатных ТОП-номеров нет (проверено прицельным поиском)
        "prefixes": prefixes,
        "masks": list(_ESHOP_MASKS),
        # Cool — главный раздел: двузначные маски 00…99 открывают почти весь пул (разбор: 43 тыс. номеров
        # против ~7 тыс. с трёх-четырёхзначными), бесплатные — короткий набор (там красивое почти не бывает).
        "masks_by_category": {"101": [""] + [f"{a}{b}" for a in range(10) for b in range(10)],
                              "-1": list(_ESHOP_MASKS)},
        "pages": 1,
        "categories_url": f"https://{domain}/eshop/catalog/numbers/categories",
        "warmup_url": f"https://{domain}/eshop/sim-web/ru/numbers",
        "shop_url": f"https://{domain}/eshop/sim-web/ru/numbers",
        # Тот же адрес, куда ведёт клик по номеру на сайте: сразу оформление этого номера
        "number_url": f"https://{domain}/eshop/sim-web/ru/checkout?phone={{phone}}&price={{price}}",
        # Cool (4 500 ₸): номера с двойками сохраняются в список без отдельного уведомления (их там ~половина)
        "soft_prices": [],  # двойки больше не собираем: по тесту вкуса не заходят
    }


OPERATOR_DEFAULTS = {
    "tele2": _eshop("tele2.kz", "TELE2", ["707", "747"]),
    "altel": _eshop("altel.kz", "ALTEL", ["700", "708"]),
    # Activ и Kcell — один бэкенд и один пул номеров. Бесплатных нет: самый дешёвый класс REGULAR = 500 ₸.
    # Цены классов: Простой 500, Бронза 5 500, Серебро 10 500, Золото 50 500, Платина 100 500 ₸.
    # Выдача случайная, 100 номеров на запрос; поиск по маске сервер не поддерживает.
    "activ": {
        "url": "https://www.activ.kz/api/v1/camunda-workers/external/reservation-list",
        "method": "POST",
        "headers": {"Content-Type": "application/json", "X-Customer-Operator-type": "ACTIV",
                    "X-Destination-Brand": "ACTIV", "X-Device-UUID": "{uuid}"},
        "params": {"page": "{page}", "size": "100"},
        # У Activ/Kcell есть листание: каждая страница — новые 100 номеров
        "page_start": 0,
        "pages": 10,
        "payload": {"msisdnCategories": ["{category}"], "phoneNumbers": []},
        # Только BRONZE (5 500 ₸). REGULAR («Простой», 500 ₸) убран: из 500 номеров под вкус не подошёл ни один.
        "categories": ["BRONZE"],
        "masks": [""],
        "warmup_url": "https://www.activ.kz/ru/new-number",
        "shop_url": "https://www.activ.kz/ru/new-number",
    },
}


def _default_db_path() -> str:
    path = os.getenv("DB_PATH", "/data/vip_sim.db")
    folder = os.path.dirname(path) or "."
    if os.path.isdir(folder) and os.access(folder, os.W_OK):
        return path
    log.warning("Папка %s недоступна (Volume не смонтирован?) — использую ./vip_sim.db", folder)
    return "./vip_sim.db"


def _json_env(name: str, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        log.error("Переменная %s содержит невалидный JSON — игнорирую", name)
        return default


def parse_cookie_string(raw: str) -> dict:
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _env_operator(name: str) -> dict:
    p = name.upper() + "_"
    cookies = os.getenv(p + "COOKIES", "").strip()
    if cookies.startswith("{"):
        cookies = _json_env(p + "COOKIES", {})
    else:
        cookies = parse_cookie_string(cookies)
    masks = os.getenv(p + "MASKS")
    prefixes = os.getenv(p + "PREFIXES")
    op = {
        "url": os.getenv(p + "URL", "").strip(),
        "method": os.getenv(p + "METHOD", "GET").strip().upper(),
        "headers": _json_env(p + "HEADERS", {}),
        "cookies": cookies,
        "params": _json_env(p + "PARAMS", {}),
        "payload": _json_env(p + "PAYLOAD", None),
        "masks": [m.strip() for m in masks.split(",")] if masks is not None else list(DEFAULT_MASKS),
        "prefixes": [x.strip() for x in prefixes.split(",") if x.strip()] if prefixes else [],
        "categories_url": os.getenv(p + "CATEGORIES_URL", "").strip(),
        "number_url": os.getenv(p + "NUMBER_URL", "").strip(),
        "cities": [],
        "categories": [],
        "soft_prices": [],
        "masks_by_category": {},
        "pages": int(os.getenv(p + "PAGES", "3")),
        "page_start": int(os.getenv(p + "PAGE_START", "1")),
        "page_size": int(os.getenv(p + "PAGE_SIZE", "50")),
        "warmup_url": os.getenv(p + "WARMUP_URL", "").strip(),
        "shop_url": os.getenv(p + "SHOP_URL", "").strip(),
        "enabled": os.getenv(p + "ENABLED", "1") not in ("0", "false", "False"),
    }
    if not op["url"] and name in OPERATOR_DEFAULTS:  # ENV не задан — берём встроенный рабочий конфиг
        for k, v in OPERATOR_DEFAULTS[name].items():
            if (k == "masks" and masks is not None) or (k == "prefixes" and prefixes):
                continue
            op[k] = v
    return op


class Config:
    __slots__ = ("bot_token", "admin_chat_id", "db_path", "min_score", "vip_min_score", "max_price", "proxy_url",
                 "refresh_every", "live", "turbo_until", "operators", "_db")

    def __init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        self.admin_chat_id = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
        self.db_path = _default_db_path()
        self.min_score = int(os.getenv("MIN_SCORE", "55"))
        self.max_price = int(os.getenv("MAX_PRICE", "5500"))
        # Дорогие номера (от 10 000 ₸, VIP) присылаем только если они действительно «чёткие»
        self.vip_min_score = int(os.getenv("VIP_MIN_SCORE", "65"))
        self.proxy_url = os.getenv("PROXY_URL", "").strip() or None
        self.refresh_every = int(os.getenv("REFRESH_EVERY", "2700"))  # прогрев кук раз в 45 минут
        self.live = False
        self.turbo_until = 0.0
        self.operators = {name: _env_operator(name) for name in OPERATORS}
        self._db = None
        if not self.bot_token or not self.admin_chat_id:
            raise SystemExit("Не заданы BOT_TOKEN и/или ADMIN_CHAT_ID")

    # ---------- синхронизация с SQLite ----------
    async def sync(self, db) -> None:
        """Накладывает сохранённые через бота настройки поверх значений из окружения."""
        self._db = db
        s = await db.get_settings()
        if "min_score" in s:
            self.min_score = int(s["min_score"])
        self.live = s.get("live") == "1"
        self.turbo_until = float(s.get("turbo_until", 0) or 0)
        for name in OPERATORS:
            raw = s.get("op:" + name)
            if raw:
                try:
                    self.merge_operator(name, json.loads(raw), persist=False)
                except ValueError:
                    log.error("Битые настройки оператора %s в БД — пропускаю", name)

    async def set_min_score(self, value: int) -> None:
        self.min_score = max(0, min(100, int(value)))
        await self._db.set_setting("min_score", str(self.min_score))

    async def set_live(self, on: bool) -> None:
        self.live = on
        await self._db.set_setting("live", "1" if on else "0")

    async def set_turbo(self, seconds: int) -> None:
        self.turbo_until = time.time() + seconds if seconds > 0 else 0.0
        await self._db.set_setting("turbo_until", str(self.turbo_until))

    @property
    def turbo(self) -> bool:
        return time.time() < self.turbo_until

    def merge_operator(self, name: str, patch: dict, persist: bool = True) -> dict:
        op = self.operators[name]
        for key, value in patch.items():
            if key in ("headers", "cookies", "params") and isinstance(value, dict):
                if patch.get("_replace"):
                    op[key] = dict(value)
                else:
                    op[key].update(value)
            elif key in op:
                op[key] = value
        return op

    async def update_operator(self, name: str, patch: dict) -> dict:
        op = self.merge_operator(name, patch)
        stored = {k: op[k] for k in ("url", "method", "headers", "cookies", "params", "payload", "masks",
                                     "prefixes", "categories_url", "number_url", "cities", "categories", "soft_prices", "masks_by_category",
                                     "pages", "page_start", "page_size",
                                     "warmup_url", "shop_url", "enabled")}
        await self._db.set_setting("op:" + name, json.dumps(stored, ensure_ascii=False))
        return op


# ---------- разбор /update_auth ----------
KNOWN_KEYS = frozenset(("url", "method", "headers", "cookies", "params", "payload", "masks", "prefixes",
                        "categories_url", "number_url", "cities", "categories", "soft_prices",
                        "masks_by_category", "pages",
                        "page_start", "page_size", "warmup_url", "shop_url", "enabled", "_replace"))


def _templatize(obj, flags: dict):
    """Подставляет {page}/{offset}/{mask} вместо значений пагинации и маски в dict-подобных структурах."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in PAGE_KEYS and isinstance(v, (int, str)) and str(v).isdigit():
                flags["page_start"] = 0 if str(v) == "0" else 1
                out[k] = "{page}"
            elif lk in OFFSET_KEYS and isinstance(v, (int, str)) and str(v).isdigit():
                out[k] = "{offset}"
            elif lk in MASK_KEYS and isinstance(v, str):
                out[k] = "{mask}"
            else:
                out[k] = _templatize(v, flags)
        return out
    if isinstance(obj, list):
        return [_templatize(v, flags) for v in obj]
    return obj


def parse_curl(cmd: str) -> dict:
    """Превращает «Copy as cURL (bash)» из DevTools в конфиг оператора."""
    cmd = cmd.replace("\\\n", " ").replace("$'", "'").strip()
    tokens = shlex.split(cmd)
    url, method, data = "", None, None
    headers, cookies = {}, {}
    i = 1
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if t in ("-X", "--request"):
            method, i = nxt.upper(), i + 2
        elif t in ("-H", "--header"):
            if ":" in nxt:
                k, v = nxt.split(":", 1)
                if k.strip().lower() == "cookie":
                    cookies.update(parse_cookie_string(v))
                elif k.strip().lower() not in SKIP_HEADERS:
                    headers[k.strip()] = v.strip()
            i += 2
        elif t in ("-b", "--cookie"):
            cookies.update(parse_cookie_string(nxt))
            i += 2
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            data, i = nxt, i + 2
        elif t in ("-A", "--user-agent"):
            headers["User-Agent"], i = nxt, i + 2
        elif t.startswith("-"):
            i += 1
        else:
            if not url:
                url = t
            i += 1
    if not url:
        raise ValueError("в cURL не найден URL")
    flags = {}
    parts = urlsplit(url)
    params = _templatize(dict(parse_qsl(parts.query, keep_blank_values=True)), flags)
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    payload = None
    if data is not None:
        try:
            payload = _templatize(json.loads(data), flags)
        except ValueError:
            payload = _templatize(dict(parse_qsl(data, keep_blank_values=True)), flags)
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    out = {
        "url": url,
        "method": method or ("POST" if data is not None else "GET"),
        "headers": headers,
        "cookies": cookies,
        "params": params,
        "payload": payload,
        "warmup_url": f"{parts.scheme}://{parts.netloc}/",
        "_replace": True,
    }
    out.update(flags)
    return out


def parse_auth_patch(text: str) -> dict:
    """Принимает cURL, JSON с ключами конфига или «голый» JSON заголовков/кук."""
    text = text.strip()
    if text.lower().startswith("curl "):
        return parse_curl(text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON-объект")
    if data.keys() & KNOWN_KEYS:
        return data
    # Голый JSON: считаем заголовками; Cookie-заголовок раскладываем в куки
    headers, cookies = {}, {}
    for k, v in data.items():
        if str(k).lower() == "cookie":
            cookies.update(parse_cookie_string(str(v)))
        else:
            headers[str(k)] = str(v)
    patch = {}
    if headers:
        patch["headers"] = headers
    if cookies:
        patch["cookies"] = cookies
    return patch
