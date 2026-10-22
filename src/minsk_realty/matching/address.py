"""Нормализация адреса: улица, дом с корпусом, разбор свободной строки адреса Kufar.

Правила намеренно строгие: если строку не удалось разобрать уверенно, возвращаем None.
Неразобранный дом ослабляет склейку (нужны фото), а неверно разобранный — даёт ложную.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# Тип улицы → каноническое сокращение. Ключи — токены после удаления точек.
STREET_KINDS = {
    "ул": "ул",
    "улица": "ул",
    "пр": "пр-т",  # в Минске «пр.» почти всегда проспект; проезд пишут «пр-д»
    "просп": "пр-т",
    "пр-т": "пр-т",
    "пр-кт": "пр-т",
    "проспект": "пр-т",
    "пер": "пер",
    "пер-к": "пер",
    "переулок": "пер",
    "б-р": "б-р",
    "бул": "б-р",
    "бульвар": "б-р",
    "пл": "пл",
    "площадь": "пл",
    "тр": "тракт",
    "тракт": "тракт",
    "ш": "ш",
    "шоссе": "ш",
    "пр-д": "проезд",
    "проезд": "проезд",
    "туп": "туп",
    "тупик": "туп",
    "наб": "наб",
    "набережная": "наб",
}

# Части адреса, которые не являются ни улицей, ни домом.
_TOWN_PARTS = {"минск", "г минск", "город минск", "беларусь", "республика беларусь"}
_SKIP_TOKENS = {"р-н", "район", "обл", "область", "мкр", "микрорайон", "кв", "квартира"}

# Латинские буквы, похожие на кириллические литеры дома («10A» вместо «10А»).
_LATIN_LETTERS = str.maketrans({"a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е"})

_HOUSE_BODY = r"(\d{1,4})\s*([а-я])?\s*(?:(?:/|к|корп|корпус)\s*(\d{1,3}))?"
_HOUSE_RE = re.compile(rf"^(?:д|дом)?\s*{_HOUSE_BODY}$")
# «ул Притыцкого 10к2»: улица и дом в одной части адреса.
_STREET_WITH_HOUSE_RE = re.compile(rf"^(.*?[а-я])\s+(?:д\s+|дом\s+)?{_HOUSE_BODY}$")
_ORDINAL_RE = re.compile(r"^(\d+)-?(?:й|я|е|ой|ая|ый|ий)$")
# Микрорайон с номером через дефис: «Сухарево-5», «Уручье-2». Это не улица.
# Без дефиса («Притыцкого 10») это улица с домом, поэтому дефис обязателен.
_MICRODISTRICT_RE = re.compile(r"^[а-я]{4,}\s*-\s*\d{1,2}$")
# Второе голое число после дома меньше этого — скорее корпус, чем квартира (см. parse_address).
_MAX_KORPUS = 9


@dataclass(frozen=True)
class Street:
    """Улица: тип (может быть неизвестен — Realt иногда отдаёт только название) и название."""

    kind: str | None
    name: tuple[str, ...]

    @property
    def key(self) -> str:
        return " ".join(self.name)

    @property
    def tail(self) -> str:
        """Последнее слово названия. Совпадает у всех улиц, которые `streets_match` считает
        одной («Коласа» и «Якуба Коласа»), поэтому годится как ключ отбора кандидатов в БД."""
        return self.name[-1]

    def __str__(self) -> str:
        return f"{self.kind} {self.key}" if self.kind else self.key


@dataclass(frozen=True)
class Address:
    street: Street | None
    house: str | None


def _clean(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`()]", " ", text)
    text = text.replace(".", ". ")
    text = re.sub(r"\s*/\s*", "/", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [t.strip(".") for t in _clean(text).replace(",", " ").split() if t.strip(".")]


def normalize_street(raw: str | None) -> Street | None:
    """«ул. Притыцкого», «Притыцкого ул.», «улица Притыцкого» → Street("ул", ("притыцкого",)).

    Инициалы («Я. Коласа») отбрасываются, порядковые числительные («2-й») сводятся к числу.
    Результат кешируется: при склейке одни и те же улицы нормализуются тысячи раз.
    """
    if not raw:
        return None
    return _normalize_street(raw)


@lru_cache(maxsize=65536)
def _normalize_street(raw: str) -> Street | None:
    kind = None
    name: list[str] = []
    for token in _tokens(raw):
        if token in STREET_KINDS and kind is None:
            kind = STREET_KINDS[token]
            continue
        ordinal = _ORDINAL_RE.match(token)
        if ordinal:
            name.append(ordinal.group(1))
        elif len(token) == 1 and token.isalpha():
            continue  # инициал
        else:
            name.append(token)
    if not name:
        return None
    return Street(kind, tuple(name))


def streets_match(a: Street | None, b: Street | None) -> bool | None:
    """True/False, если обе улицы известны; None, если сравнить нельзя.

    Названия совпадают целиком или короткое является «хвостом» длинного
    («Коласа» и «Якуба Коласа»). Числа в лишней части запрещены: «Ленина» ≠ «2-й Ленина».
    Тип улицы сравнивается, только если он известен у обеих.
    """
    if a is None or b is None:
        return None
    if a.kind and b.kind and a.kind != b.kind:
        return False
    if a.name == b.name:
        return True
    short, long_ = sorted((a.name, b.name), key=len)
    if long_[-len(short) :] != short or len(short[-1]) < 4:
        return False
    return not any(t.isdigit() for t in long_[: -len(short)])


def _format_house(num: str, letter: str | None, korpus: str | None) -> str:
    house = num.lstrip("0") or num
    if letter:
        house += letter
    if korpus:
        house += f"/{korpus.lstrip('0') or korpus}"
    return house


def normalize_house(raw: str | None) -> str | None:
    """«10», «д. 10», «10А», «10 к.2», «10 корп. 2», «10/2» → «10», «10а», «10/2».

    Корпус записывается через «/», как его собирает адаптер Realt (houseNumber/buildingNumber).
    Всё, что не разбирается целиком (например «10-2»), даёт None.
    """
    if raw is None:
        return None
    text = _clean(str(raw)).translate(_LATIN_LETTERS).replace(".", " ").replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    match = _HOUSE_RE.match(text)
    return _format_house(*match.groups()) if match else None


def _is_noise(part: str) -> bool:
    plain = re.sub(r"\s+", " ", part.replace(".", " ")).strip()
    return plain in _TOWN_PARTS or bool(_SKIP_TOKENS & set(plain.split()))


def parse_address(raw: str | None) -> Address:
    """Разобрать свободную строку адреса («Минск, ул. Притыцкого, 10») на улицу и дом.

    Город, район, микрорайон («Сухарево-5») и квартира отбрасываются. Улица — часть с типом
    улицы («ул.», «пр-т»); часть без типа берётся, только если частей с типом нет. Если
    кандидатов одного уровня несколько, улица остаётся None: угадывать нельзя.

    Дом — первое число после улицы. Второе голое число («…, 10, 25») — квартира или корпус:
    большое (> 9) считаем квартирой и отбрасываем, маленькое — неоднозначно (корпус 2 и дом
    без корпуса — разные здания), и тогда дом None.
    """
    if not raw:
        return Address(None, None)
    with_kind: list[tuple[Street, str | None]] = []
    without_kind: list[tuple[Street, str | None]] = []
    houses: list[str] = []
    for part in (p.strip() for p in _clean(raw).split(",")):
        if not part or _is_noise(part):
            continue
        plain = part.replace(".", " ")
        plain = re.sub(r"\s+", " ", plain).strip()
        if _MICRODISTRICT_RE.match(plain):
            continue
        as_house = normalize_house(plain)
        if as_house is not None:
            houses.append(as_house)
            continue
        combined = _STREET_WITH_HOUSE_RE.match(plain)
        if combined:
            street, house = (
                normalize_street(combined.group(1)),
                _format_house(*combined.groups()[1:]),
            )
        elif re.search(r"[а-я]{3,}", plain):
            street, house = normalize_street(plain), None
        else:
            continue
        if street is not None:
            (with_kind if street.kind else without_kind).append((street, house))
    candidates = with_kind or without_kind
    if len({c[0] for c in candidates}) != 1:
        return Address(None, houses[0] if len(houses) == 1 and not candidates else None)
    street, house = candidates[0]
    rest = houses
    if house is None and houses:
        house, rest = houses[0], houses[1:]
    if house is not None and rest:
        extra = rest[0]
        if not extra.isdigit() or int(extra) <= _MAX_KORPUS or len(rest) > 1:
            house = None  # корпус или квартира — не угадываем
    return Address(street, house)
