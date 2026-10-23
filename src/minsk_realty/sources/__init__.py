"""Адаптеры площадок. Каждый адаптер изолирован: поломка одной площадки не роняет остальные.

`ADAPTERS` — единый реестр: по нему работают сборщик, разбор ссылок в боте и отчёт /check.
"""

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
