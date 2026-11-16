"""Tele2 Kazakhstan: коды 707, 747. Адрес API и тело запроса задаются через TELE2_* переменные или /update_auth tele2 (cURL/JSON)."""
from parsers.base_parser import BaseParser


class Tele2Parser(BaseParser):
    NAME = "tele2"
    TITLE = "Tele2"
    HOME = "https://tele2.kz/"
    PHONE_KEYS = ("msisdn", "number", "phone", "phonenumber", "value")
    __slots__ = ()
