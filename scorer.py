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


def score(d: str) -> tuple[int, list[str]]:
    """Возвращает (итог 0..100, список сработавших правил).

    Номер оценивается так, как его видит человек: +7 КОД БЛОК-ПАРА-ПАРА (707 670-71-66).
    Узоры, которые «прячутся» поперёк дефисов (707-6-707 в 707 670-71-66), не засчитываются.
    """
    rules = []
    total = 0
    p, q, x, y = d[:3], d[3:6], d[6:8], d[8:]  # код, блок, пара, пара
    sub = d[3:]  # 7 цифр после кода: q-x-y

    # --- Группа А: блок после кода связан с кодом ---
    if q == p:
        total += 55
        rules.append(f"А · Блок повторяет код: {p} {q} (+55)")
    elif q == p[::-1] or q == p[1:] + p[0] or q == p[2] + p[:2] or (
            q[0] == q[2] and len(set(q)) == 2 and set(q) <= set(p) | {"0"}):
        total += 45
        rules.append(f"А · Блок в ритм коду: {p} {q} (+45)")

    # --- Группа Б: симметрия и ритм в «{блок}-{пара}-{пара}» ---
    b = None
    if sub == sub[::-1] or d[4:] == d[4:][::-1]:
        b = (60, f"Зеркало {q}-{x}-{y}")
    elif _consecutive(sub) or _consecutive(d[4:]):
        b = (50, f"Лесенка {q}-{x}-{y}")
    elif q[0] == q[1] == q[2]:
        b = (45, f"Тройной блок {q}")
    elif x[1] == y[1] == "0" and x[0] != y[0] and abs(ord(x[0]) - ord(y[0])) == 1:
        b = (40, f"Круглые десятки {x}-{y}")
    if b:
        total += b[0]
        rules.append(f"Б · {b[1]} (+{b[0]})")

    # --- Группа В: хвост (последние 4 цифры) ---
    t = d[6:]
    if t[0] == t[1] == t[2] == t[3]:
        c = (100, "AAAA")
    elif t[1] == t[2] == t[3]:
        c = (55, "тройка в конце")  # 86-66, 18-88, 00-09 -> x-666 / x-888: классический полублат
    elif t[0] == t[1] == t[2]:
        c = (45, "тройка в начале")
    elif t[0] == t[3] and t[1] == t[2]:
        c = (45, "ABBA")
    elif t[0] == t[2] and t[1] == t[3]:
        c = (40, "ABAB")
    elif t[0] == t[1] and t[2] == t[3]:
        c = (35, "AABB")
    else:
        c = None
    if c:
        total += c[0]
        rules.append(f"В · Хвост {c[1]}: {t[:2]}-{t[2:]} (+{c[0]})")

    # --- Группа Г: плотность цифр ---
    uniq = len(set(d))
    if uniq == 2:
        total += 90
        rules.append("Г · Всего 2 уникальные цифры (+90)")
    elif uniq == 3:
        total += 65
        rules.append("Г · Всего 3 уникальные цифры (+65)")
    else:
        top = max(d.count(ch) for ch in set(d))
        if top >= 5:
            total += 30
            rules.append(f"Г · {top} одинаковых цифр вразброс (+30)")

    return min(total, 100), rules


def doubles(d: str) -> list[str]:
    """«Мягкие» узоры Cool (по выбору владельца): двойка в середине (xxx-22-xx) или в конце (xxx-xx-22).
    Двойка в блоке (699) отмечается дополнительно, если есть одна из пар выше."""
    q, x, y = d[3:6], d[6:8], d[8:]
    out = []
    if y[0] == y[1]:
        out.append(f"Двойка в конце: {x}-{y}")
    if x[0] == x[1]:
        out.append(f"Двойка в середине: {x}-{y}")
    if out and (q[0] == q[1] or q[1] == q[2]):
        out.append(f"Двойка в блоке: {q}")
    return out


def price_ok(price: int | None, max_price: int) -> bool:
    """0 ₸ / неизвестно — пропускаем, платные до MAX_PRICE — пропускаем, дороже — отсекаем."""
    return price is None or price <= max_price


def rank(total: int) -> str:
    if total >= 90:
        return "👑 БЛАТ (90+)"
    if total >= 65:
        return "🔥 ПОЧТИ БЛАТ (65+)"
    if total >= 55:
        return "💎 ПОЛУБЛАТ (55+)"
    return "📱 НОМЕР ПО ПОРОГУ"


TEST_NUMBERS = ("7077077777", "7071234321", "7470007070", "7779994545", "7076707166")
