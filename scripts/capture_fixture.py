"""Снять живую фикстуру площадки и сразу вычистить персональные данные.

Запускать на сервере в Беларуси (Kufar отвечает «Доступ ограничен» из-за рубежа):

    python scripts/capture_fixture.py kufar
    python scripts/capture_fixture.py realt

Скрипт делает ОДИН запрос к первой странице выдачи, сохраняет очищенный ответ в
tests/fixtures/<source>/ и печатает список полей, чтобы закрыть пункты UNVERIFIED
в адаптерах. Перед коммитом фикстуру всё равно смотрит Grant (grant-guard).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from minsk_realty.privacy import scrub
from minsk_realty.sources import kufar, realt
from minsk_realty.sources.base import PoliteClient

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Адаптеры свободный текст не читают, а в нём продавцы пишут имена («Звоните, Анна»),
# которые scrub по ключам не ловит.
FREE_TEXT_KEYS = frozenset(
    {"body", "body_short", "description", "headline", "title", "comments", "specialComment"}
)
TEXT_PLACEHOLDER = "[текст удалён]"


def strip_free_text(value: Any) -> Any:
    """Заменить свободный текст объявлений заглушкой, сохранив структуру ответа."""
    if isinstance(value, dict):
        return {
            k: TEXT_PLACEHOLDER
            if k in FREE_TEXT_KEYS and isinstance(v, str) and v
            else strip_free_text(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [strip_free_text(item) for item in value]
    return value


def capture_kufar(client: PoliteClient) -> None:
    params = dict(kufar.SEARCH_PARAMS, size="20")
    data = strip_free_text(scrub(client.get(kufar.SEARCH_URL, params=params).json()))
    path = OUT / "kufar" / f"search_sell_minsk.{date.today()}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    keys = Counter(p.get("p") for ad in data.get("ads", []) for p in ad.get("ad_parameters", []))
    currencies = Counter(ad.get("currency") for ad in data.get("ads", []))
    print(f"saved {path}\nad_parameters: {dict(keys)}\ncurrency values: {dict(currencies)}")


def capture_realt(client: PoliteClient) -> None:
    page_props = realt.extract_page_props(client.get(realt.LIST_URL, params={"page": 1}).text)
    data = strip_free_text(
        scrub({"objects": page_props.get("objects"), "pagination": page_props.get("pagination")})
    )
    path = OUT / "realt" / f"sale_flats.{date.today()}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    keys = sorted({k for obj in data["objects"] or [] for k in obj})
    print(f"saved {path}\nobject keys: {keys}")


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else ""
    capture = {"kufar": capture_kufar, "realt": capture_realt}.get(source)
    if capture is None:
        sys.exit("usage: capture_fixture.py kufar|realt")
    client = PoliteClient()
    try:
        capture(client)
    finally:
        client.close()
