"""Форматирование для русских текстов: деньги, даты, склонения, районы, улицы."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

MINSK_TZ = ZoneInfo("Europe/Minsk")

SOURCE_NAMES = {"kufar": "Kufar", "realt": "Realt", "onliner": "Onliner", "domovita": "Domovita"}
SELLER_NAMES = {"owner": "собственник", "agency": "агентство", "unknown": "продавец не указан"}
CONFIDENCE_NAMES = {"high": "высокая", "medium": "средняя", "low": "низкая"}
LEVEL_NAMES = {"high": "высокий", "medium": "средний", "low": "низкий", "unknown": "мало данных"}
MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Районы Минска. Kufar отдаёт «Фрунзенский», в БД объекта — «фрунзенский»; Realt района не отдаёт.
MINSK_DISTRICTS = (
    "Заводской",
    "Ленинский",
    "Московский",
    "Октябрьский",
    "Партизанский",
    "Первомайский",
    "Советский",
    "Фрунзенский",
    "Центральный",
)

_STREET_KINDS = {
    "ул": "ул.",
    "пер": "пер.",
    "пл": "пл.",
    "ш": "ш.",
    "наб": "наб.",
    "туп": "туп.",
}


def normalize_district(raw: str | None) -> str | None:
    """«фрунзенский», «Фрунзенский р-н», «Фрунзенский район» → «Фрунзенский». Иначе None."""
    if not raw:
        return None
    text = raw.lower().replace("ё", "е").replace("р-н", " ").replace("район", " ")
    first = text.split()[0] if text.split() else ""
    for name in MINSK_DISTRICTS:
        if first == name.lower():
            return name
    return None


def district_label(district: str | None) -> str:
    return f"{district} р-н" if district else "Минск"


def street_label(street_norm: str | None) -> str | None:
    """«ул притыцкого» → «ул. Притыцкого»; «пр-т независимости» → «пр-т Независимости»."""
    if not street_norm:
        return None
    parts = street_norm.split()
    if parts and parts[0] in (
        "ул",
        "пр-т",
        "пер",
        "б-р",
        "пл",
        "тракт",
        "ш",
        "проезд",
        "туп",
        "наб",
    ):
        kind, name = _STREET_KINDS.get(parts[0], parts[0]), parts[1:]
    else:
        kind, name = None, parts
    words = [w if w[:1].isdigit() else w[:1].upper() + w[1:] for w in name]
    return " ".join([kind, *words] if kind else words) or None


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def days_text(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def times_text(n: int) -> str:
    return f"{n} {plural(n, 'раз', 'раза', 'раз')}"


def objects_text(n: int) -> str:
    return f"{n} {plural(n, 'объект', 'объекта', 'объектов')}"


def group_thousands(value: int) -> str:
    # Неразрывный пробел не используем: Telegram и терминал показывают обычный надёжнее.
    return f"{value:,}".replace(",", " ")


def money(amount: Decimal | float | None, currency: str | None) -> str:
    """$92 500 / 300 000 BYN / €80 000. Без копеек: цены квартир целые."""
    if amount is None:
        return "цена не указана"
    whole = int(Decimal(str(amount)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    text = group_thousands(whole)
    if currency == "USD":
        return f"${text}" if whole >= 0 else f"−${text[1:]}"
    if currency == "EUR":
        return f"€{text}"
    return f"{text} {currency or ''}".rstrip()


def money_k(amount: Decimal | float, currency: str | None) -> str:
    """$88 500 → «$88.5k», для коротких диапазонов («$88–89k»)."""
    value = float(amount) / 1000
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    if currency == "USD":
        return f"${text}k"
    return f"{text} тыс. {currency or ''}".rstrip()


def offer_range(low: Decimal, high: Decimal, currency: str | None) -> str:
    """$78 500 и $80 000 → «$78 500–80 000» (тот же формат денег, что везде)."""
    lo, hi = money(low, currency), money(high, currency)
    if lo == hi:
        return lo
    if currency == "USD":
        return f"{lo}–{hi[1:]}"
    if currency == "EUR":
        return f"{lo}–{hi[1:]}"
    return f"{lo.rsplit(' ', 1)[0]}–{hi}"


def num(value: float) -> str:
    """54.5 → «54,5», 54.0 → «54»."""
    return f"{value:g}".replace(".", ",")


def ppm(value: float | None) -> str:
    return f"${group_thousands(round(value))}/м²" if value is not None else "—"


def pct(value: float, *, signed: bool = True) -> str:
    text = f"{abs(value):.1f}".replace(".0", "") if abs(value) < 10 else f"{abs(value):.0f}"
    if not signed:
        return f"{text}%"
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{text}%"


def local(dt: datetime) -> datetime:
    return dt.astimezone(MINSK_TZ)


def date_short(dt: datetime) -> str:
    return f"{local(dt):%d.%m}"


def date_full(dt: datetime) -> str:
    return f"{local(dt):%d.%m.%Y}"


def date_words(dt: datetime) -> str:
    d = local(dt)
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def rooms_label(rooms: int | None) -> str:
    if rooms is None:
        return "все комн."
    return "4+ комн." if rooms >= 4 else f"{rooms}-комн."
