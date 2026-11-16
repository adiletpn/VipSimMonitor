"""Движок оценки красоты 10-значного номера (без +7). 4 группы, в каждой — только один максимальный балл."""
import re

# Мобильные коды Казахстана (Altel/Tele2/Activ/Kcell/Beeline)
KZ_CODES = frozenset(("700", "701", "702", "703", "704", "705", "706", "707", "708", "709",
                      "747", "771", "775", "776", "777", "778"))
_NON_DIGIT = re.compile(r"\D")


def normalize(raw) -> str | None:
    """'+7 (707) 707-45-45' / '87077074545' / 7077074545 -> '7077074545' или None."""
    d = _NON_DIGIT.sub("", str(raw))
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    if len(d) == 10 and d[:3] in KZ_CODES:
        return d
    return None


def fmt_human(d: str) -> str:
    return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"


def fmt_plain(d: str) -> str:
    return "+7" + d


def _consecutive(w: str) -> bool:
    """Все цифры идут подряд с шагом +1 или -1: 234567 / 7654321."""
    step = ord(w[1]) - ord(w[0])
    return step in (1, -1) and all(ord(w[k + 1]) - ord(w[k]) == step for k in range(1, len(w) - 1))


