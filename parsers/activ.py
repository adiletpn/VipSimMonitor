"""Activ/Kcell: коды 701, 702, 775, 778. Адрес API и тело запроса задаются через ACTIV_* переменные или /update_auth activ (cURL/JSON)."""
from parsers.base_parser import BaseParser


class ActivParser(BaseParser):
    NAME = "activ"
    TITLE = "Activ/Kcell"
    HOME = "https://www.activ.kz/ru/new-number"
    PHONE_KEYS = ("msisdn", "phone", "number", "phonenumber", "value")
    __slots__ = ()
