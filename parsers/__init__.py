from parsers.activ import ActivParser
from parsers.altel import AltelParser
from parsers.tele2 import Tele2Parser

# Beeline не подключён: продажа номеров перенесена из веб-магазина в мобильное приложение Janymda,
# веб-API со списком номеров больше нет. Адаптер parsers/beeline.py оставлен на случай возврата витрины.
PARSERS = {cls.NAME: cls for cls in (Tele2Parser, AltelParser, ActivParser)}
