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


