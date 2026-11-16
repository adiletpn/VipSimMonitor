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
        "categories": ["-1", "101"],
        "prefixes": prefixes,
        "masks": list(_ESHOP_MASKS),
        "pages": 1,
        "categories_url": f"https://{domain}/eshop/catalog/numbers/categories",
        "warmup_url": f"https://{domain}/eshop/sim-web/ru/numbers",
        "shop_url": f"https://{domain}/eshop/sim-web/ru/numbers",
        # Тот же адрес, куда ведёт клик по номеру на сайте: сразу оформление этого номера
        "number_url": f"https://{domain}/eshop/sim-web/ru/checkout?phone={{phone}}&price={{price}}",
        # Cool (4 500 ₸): номера с двойками сохраняются в список без отдельного уведомления (их там ~половина)
        "soft_prices": [4500],
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
        "params": {"page": "0", "size": "100"},
        "payload": {"msisdnCategories": ["{category}"], "phoneNumbers": []},
        # REGULAR = «Простой» (500 ₸), BRONZE = «Бронза» (5 500 ₸, много зеркальных номеров)
        "categories": ["REGULAR", "BRONZE"],
        "masks": [""],
        "pages": 1,
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


