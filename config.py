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


