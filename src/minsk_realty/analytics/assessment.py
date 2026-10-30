"""Вывод по квартире: строка-вердикт, минусы, плюсы, соседи по дому.

Только из фактов, без обращения к БД и сети. Тексты — из спеки (разделы 4.1, 4.2, 4.4, 4.5).
Иконки («✅», «⚠️» …) рисует отчёт по `kind`, здесь их нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from minsk_realty.analytics.comparables import ComparePoint, Comparison, epoch
from minsk_realty.analytics.fmt import group_thousands, pct, ppm
from minsk_realty.analytics.geo import MAX_STATION_M, Location

MAX_MINUSES = 4
MAX_PLUSES = 3
FAR_WALK_MIN = 20  # дальше — «далеко от метро»
NEAR_WALK_MIN = 10  # не дальше — «метро рядом»

_BARGAIN_TAIL = {"high": ", торг реален", "low": ", большой скидки не ждите"}


@dataclass(frozen=True)
class Minus:
    text: str
    hint: str | None
    detail: str | None = None  # факт-пояснение для страницы кабинета; бот его не показывает


@dataclass(frozen=True)
class Plus:
    text: str
    detail: str | None = None


@dataclass(frozen=True)
class Assessment:
    kind: Literal["suspicious", "below", "market", "above", "unknown"]
    verdict: str  # без иконки
    minuses: tuple[Minus, ...]
    pluses: tuple[Plus, ...]


def _ref(comparison: Comparison) -> str:
    """С чем сравнили: на шаге «район» это медиана района, иначе похожие квартиры."""
    if comparison.step != "district":
        return "похожих"
    return "медианы района" if comparison.district else "медианы по Минску"


def _median_ref(comparison: Comparison) -> str:
    return "медианы похожих" if comparison.step != "district" else _ref(comparison)


def _price_detail(comparison: Comparison) -> str:
    n = pct(comparison.diff_pct, signed=False)
    word = "дороже" if comparison.diff_pct > 0 else "ниже"
    return f"на {n} {word} {_median_ref(comparison)} ({ppm(comparison.median)})"


def _near_detail(location: Location | None) -> str | None:
    if location is None or location.station is None:
        return None
    return f"«{location.station.name}», ~{location.walk_min} мин пешком"


def _metro_detail(location: Location) -> str:
    if location.station is None:
        return f"ближайшее метро дальше {MAX_STATION_M // 1000} км"
    meters = f" ({group_thousands(round(location.station_m))} м)" if location.station_m else ""
    return f"до «{location.station.name}» ~{location.walk_min} мин пешком{meters}"


def _verdict(
    comparison: Comparison | None, bargaining_level: str, usd_per_m2: float | None
) -> tuple[str, str]:
    if comparison is None:
        if usd_per_m2 is None:
            return "unknown", "Цену за м² посчитать не можем: в объявлении нет площади или цены"
        return "unknown", "Похожих квартир рядом мало, сравнить цену не можем"
    kind = comparison.group
    ref = _ref(comparison)
    n = pct(comparison.diff_pct, signed=False)
    if kind == "suspicious":  # торг тут не обсуждаем: сначала выяснить причину
        return (
            kind,
            f"Подозрительно дёшево: на {n} ниже {ref}. Узнайте причину до просмотра (доля, состояние, документы)",
        )
    text = {
        "below": f"Стоит смотреть: цена на {n} ниже {ref}",
        "market": "Цена в рынке",
        "above": f"Дороже {ref} на {n}: смотреть, если готовы торговаться",
    }[kind]
    return kind, text + _BARGAIN_TAIL.get(bargaining_level, "")


def _far_from_metro(location: Location | None) -> bool:
    if location is None:
        return False
    return location.station is None or (
        location.walk_min is not None and location.walk_min > FAR_WALK_MIN
    )


def _storey(floor, floors_total) -> str:
    return f"этаж {floor} из {floors_total}" if floors_total else f"этаж {floor}"


def _minuses(comparison, floor, floors_total, year, now_year, location) -> tuple[Minus, ...]:
    ep = epoch(year, now_year)
    ref = _ref(comparison) if comparison else ""
    lift = f"{floors_total}-этажный дом {year} г." if floors_total and year else None
    rows = (
        (
            floor == 1,
            Minus(
                "Первый этаж", "спросите про подвал, сырость и решётки", _storey(1, floors_total)
            ),
        ),
        (
            floor is not None and floor == floors_total and floors_total > 1,
            Minus("Последний этаж", "спросите про крышу и протечки", _storey(floor, floors_total)),
        ),
        (
            floors_total is not None and floors_total <= 5 and year is not None and year < 2000,
            Minus("Скорее всего без лифта", None, lift),
        ),
        (
            year is not None and year <= 1990,
            Minus(f"Дом {year} г.", "узнайте, был ли капремонт и меняли ли стояки"),
        ),
        (ep == "building", Minus("Дом ещё строится", f"сдача в {year}, проверьте застройщика")),
        (
            _far_from_metro(location),
            Minus("Далеко от метро", None, _metro_detail(location) if location else None),
        ),
        (
            comparison is not None and comparison.group == "above",
            Minus(f"Дороже {ref}", None, _price_detail(comparison) if comparison else None),
        ),
    )
    return tuple(m for ok, m in rows if ok)[:MAX_MINUSES]


def _pluses(comparison, year, now_year, location) -> tuple[Plus, ...]:
    walk = location.walk_min if location else None
    ref = _ref(comparison) if comparison else ""
    rows = (
        (
            walk is not None and walk <= NEAR_WALK_MIN,
            Plus("Метро рядом", _near_detail(location)),
        ),
        (epoch(year, now_year) == "post2010", Plus("Новый дом", f"дом {year} г.")),
        (
            comparison is not None and comparison.group == "below",
            Plus(f"Цена ниже {ref}", _price_detail(comparison) if comparison else None),
        ),
    )
    return tuple(p for ok, p in rows if ok)[:MAX_PLUSES]


def assess(
    *,
    comparison: Comparison | None,
    usd_per_m2: float | None,
    bargaining_level: str,
    floor: int | None,
    floors_total: int | None,
    year: int | None,
    now_year: int,
    location: Location | None,
) -> Assessment:
    kind, verdict = _verdict(comparison, bargaining_level, usd_per_m2)
    return Assessment(
        kind=kind,
        verdict=verdict,
        minuses=_minuses(comparison, floor, floors_total, year, now_year, location),
        pluses=_pluses(comparison, year, now_year, location),
    )


def same_house(
    points: list[ComparePoint] | tuple[ComparePoint, ...],
    *,
    object_id: int | None,
    house_key: str | None,
) -> tuple[float, ...]:
    """Цены за м² других объектов в том же доме по возрастанию. Сколько показать — решает отчёт."""
    if house_key is None:
        return ()
    return tuple(
        sorted(
            p.usd_per_m2 for p in points if p.house_key == house_key and p.object_id != object_id
        )
    )
