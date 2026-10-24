"""Вывод по квартире: строка-вердикт, минусы, плюсы, «кому подойдёт», соседи по дому.

Только из фактов, без обращения к БД и сети. Тексты — из спеки (разделы 4.1, 4.2, 4.4, 4.5).
Иконки («✅», «⚠️» …) рисует отчёт по `kind`, здесь их нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from minsk_realty.analytics.comparables import ComparePoint, Comparison, epoch
from minsk_realty.analytics.fmt import pct
from minsk_realty.analytics.geo import Location

MAX_MINUSES = 4
MAX_PLUSES = 3
MAX_AUDIENCE = 2
FAR_WALK_MIN = 20  # дальше — «далеко от метро»
NEAR_WALK_MIN = 10  # не дальше — «метро рядом»
STUDIO_AREA_M2 = 45
BIG_AREA_M2 = 70

_BARGAIN_TAIL = {"high": ", торг реален", "low": ", большой скидки не ждите"}


@dataclass(frozen=True)
class Minus:
    text: str
    hint: str | None


@dataclass(frozen=True)
class Assessment:
    kind: Literal["suspicious", "below", "market", "above", "unknown"]
    verdict: str  # без иконки
    minuses: tuple[Minus, ...]
    pluses: tuple[str, ...]
    audience: tuple[str, ...]


def _verdict(comparison: Comparison | None, bargaining_level: str) -> tuple[str, str]:
    if comparison is None:
        return "unknown", "Похожих квартир рядом мало, сравнить цену не можем"
    kind = comparison.group
    n = pct(comparison.diff_pct, signed=False)
    if kind == "suspicious":  # торг тут не обсуждаем: сначала выяснить причину
        return (
            kind,
            f"Подозрительно дёшево: на {n} ниже похожих. Узнайте причину до просмотра (доля, состояние, документы)",
        )
    text = {
        "below": f"Стоит смотреть: цена на {n} ниже похожих",
        "market": "Цена в рынке",
        "above": f"Дороже похожих на {n}: смотреть, если готовы торговаться",
    }[kind]
    return kind, text + _BARGAIN_TAIL.get(bargaining_level, "")


def _far_from_metro(location: Location | None) -> bool:
    if location is None:
        return False
    return location.station is None or (
        location.walk_min is not None and location.walk_min > FAR_WALK_MIN
    )


def _minuses(comparison, floor, floors_total, year, now_year, location) -> tuple[Minus, ...]:
    ep = epoch(year, now_year)
    rows = (
        (floor == 1, Minus("Первый этаж", "спросите про подвал, сырость и решётки")),
        (
            floor is not None and floor == floors_total and floors_total > 1,
            Minus("Последний этаж", "спросите про крышу и протечки"),
        ),
        (
            floors_total is not None and floors_total <= 5 and year is not None and year < 2000,
            Minus("Скорее всего без лифта", None),
        ),
        (
            year is not None and year <= 1990,
            Minus(f"Дом {year} г.", "узнайте, был ли капремонт и меняли ли стояки"),
        ),
        (ep == "building", Minus("Дом ещё строится", f"сдача в {year}, проверьте застройщика")),
        (_far_from_metro(location), Minus("Далеко от метро", None)),
        (comparison is not None and comparison.group == "above", Minus("Дороже похожих", None)),
    )
    return tuple(m for ok, m in rows if ok)[:MAX_MINUSES]


def _pluses(comparison, year, now_year, location) -> tuple[str, ...]:
    walk = location.walk_min if location else None
    rows = (
        (walk is not None and walk <= NEAR_WALK_MIN, "Метро рядом"),
        (epoch(year, now_year) == "post2010", "Новый дом"),
        (comparison is not None and comparison.group == "below", "Цена ниже похожих"),
    )
    return tuple(t for ok, t in rows if ok)[:MAX_PLUSES]


def _audience(rooms, area, location) -> tuple[str, ...]:
    walk = location.walk_min if location else None
    big = (rooms is not None and rooms >= 3) or (area is not None and area >= BIG_AREA_M2)
    rows = (
        (
            rooms == 1
            and area is not None
            and area <= STUDIO_AREA_M2
            and walk is not None
            and walk <= 15,
            "одному или под сдачу",
        ),
        (rooms in (1, 2), "паре или небольшой семье"),
        (big and _far_from_metro(location), "семье с машиной"),
        (big, "семье с детьми"),
    )
    return tuple(t for ok, t in rows if ok)[:MAX_AUDIENCE]


def assess(
    *,
    comparison: Comparison | None,
    bargaining_level: str,
    rooms: int | None,
    area: float | None,
    floor: int | None,
    floors_total: int | None,
    year: int | None,
    now_year: int,
    location: Location | None,
) -> Assessment:
    kind, verdict = _verdict(comparison, bargaining_level)
    return Assessment(
        kind=kind,
        verdict=verdict,
        minuses=_minuses(comparison, floor, floors_total, year, now_year, location),
        pluses=_pluses(comparison, year, now_year, location),
        audience=_audience(rooms, area, location),
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
