"""Altel: коды 700, 708. Адрес API и тело запроса задаются через ALTEL_* переменные или /update_auth altel (cURL/JSON)."""
from parsers.base_parser import BaseParser


class AltelParser(BaseParser):
    NAME = "altel"
    TITLE = "Altel"
    HOME = "https://altel.kz/"
    PHONE_KEYS = ("msisdn", "number", "phone", "phonenumber", "value")
    __slots__ = ()
