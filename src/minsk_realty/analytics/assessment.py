"""Вывод по квартире: строка-вердикт и соседи по дому.

Только из фактов, без обращения к БД и сети. Тексты — из спеки (разделы 4.1, 4.2, 4.4, 4.5).
Иконки («✅», «⚠️» …) рисует отчёт по `kind`, здесь их нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from minsk_realty.analytics.comparables import ComparePoint, Comparison
from minsk_realty.analytics.fmt import pct


@dataclass(frozen=True)
class Assessment:
    kind: Literal["suspicious", "below", "market", "above", "unknown"]
    verdict: str  # без иконки


def _ref(comparison: Comparison) -> str:
    """С чем сравнили: на шаге «район» это медиана района, иначе похожие квартиры."""
    if comparison.step != "district":
        return "похожих"
    return "медианы района" if comparison.district else "медианы по Минску"


def _verdict(comparison: Comparison | None, usd_per_m2: float | None) -> tuple[str, str]:
    if comparison is None:
        if usd_per_m2 is None:
            return "unknown", "Цену за м² посчитать не можем: в объявлении нет площади или цены"
        return "unknown", "Похожих квартир рядом мало, сравнить цену не можем"
    kind = comparison.group
    ref = _ref(comparison)
    n = pct(comparison.diff_pct, signed=False)
    if kind == "suspicious":
        return (
            kind,
            f"Подозрительно дёшево: на {n} ниже {ref}. Узнайте причину до просмотра (доля, состояние, документы)",
        )
    text = {
        "below": f"Стоит смотреть: цена на {n} ниже {ref}",
        "market": "Цена в рынке",
        "above": f"Дороже {ref} на {n}",
    }[kind]
    return kind, text


def assess(*, comparison: Comparison | None, usd_per_m2: float | None) -> Assessment:
    kind, verdict = _verdict(comparison, usd_per_m2)
    return Assessment(kind=kind, verdict=verdict)


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
