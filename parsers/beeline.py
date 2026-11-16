"""Beeline Kazakhstan: коды 705, 771, 776, 777. Адрес API и тело запроса задаются через BEELINE_* переменные или /update_auth beeline (cURL/JSON)."""
from parsers.base_parser import BaseParser


class BeelineParser(BaseParser):
    NAME = "beeline"
    TITLE = "Beeline"
    HOME = "https://beeline.kz/"
    PHONE_KEYS = ("ctn", "msisdn", "number", "phone", "phonenumber", "value")
    __slots__ = ()
