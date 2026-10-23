"""Адаптеры площадок. Каждый адаптер изолирован: поломка одной площадки не роняет остальные.

`ADAPTERS` — единый реестр: по нему работают сборщик, разбор ссылок в боте и отчёт /check.
"""

from datetime import timedelta

from minsk_realty.sources.domovita import DomovitaAdapter
from minsk_realty.sources.kufar import KufarAdapter
from minsk_realty.sources.onliner import OnlinerAdapter
from minsk_realty.sources.realt import RealtAdapter

ADAPTERS = {
    "kufar": KufarAdapter,
    "realt": RealtAdapter,
    "onliner": OnlinerAdapter,
    "domovita": DomovitaAdapter,
}
NAMES = {"kufar": "Kufar", "realt": "Realt", "onliner": "Onliner", "domovita": "Domovita"}
DOMAINS = {
    "kufar": "kufar.by",
    "realt": "realt.by",
    "onliner": "onliner.by",
    "domovita": "domovita.by",
}


def url_adapters() -> tuple:
    """По экземпляру каждого адаптера для разбора ссылок (resolve_url в сеть не ходит)."""
    return tuple(cls() for cls in ADAPTERS.values())


def names_text(conjunction: str = "или") -> str:
    """«Kufar, Realt, Onliner или Domovita»."""
    names = list(NAMES.values())
    return f"{', '.join(names[:-1])} {conjunction} {names[-1]}"


# Обычный цикл сборщика; площадки с `min_interval` обходятся реже.
DEFAULT_POLL = "раз в 30–60 минут"
# Почему объявления площадки может не быть в базе, хотя оно в продаже.
NOT_COLLECTED_NOTE = {
    "onliner": (
        "Часть объявлений Onliner — это объявления Realt, которые Onliner показывает у себя. "
        "Их мы отдельно не собираем: найдите эту квартиру на realt.by и пришлите ссылку оттуда."
    ),
}


def min_interval(source: str) -> timedelta:
    return getattr(ADAPTERS.get(source), "min_interval", None) or timedelta(0)


def poll_text(source: str) -> str:
    """«раз в 30–60 минут» или «раз в 6 часов» — как часто сборщик обходит площадку."""
    hours = int(min_interval(source).total_seconds() // 3600)
    if not hours:
        return DEFAULT_POLL
    word = (
        "час"
        if hours % 10 == 1 and hours % 100 != 11
        else ("часа" if hours % 10 in (2, 3, 4) and hours % 100 not in (12, 13, 14) else "часов")
    )
    return f"раз в {hours} {word}"
