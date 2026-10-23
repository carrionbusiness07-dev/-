"""Адаптеры на живой выдаче площадок (снята 23.10.2026 из Минска, без персональных данных)."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from minsk_realty.matching.address import parse_address
from minsk_realty.sources.kufar import KufarAdapter, to_raw
from minsk_realty.sources.realt import RealtAdapter
from minsk_realty.sources.realt import to_raw as realt_raw

FIXTURES = Path(__file__).parent / "fixtures"
KUFAR = FIXTURES / "kufar" / "search_sell_minsk.2026-10-23.json"
REALT = FIXTURES / "realt" / "sale_flats.2026-10-23.json"


@pytest.fixture(scope="module")
def kufar_ads():
    return json.loads(KUFAR.read_text(encoding="utf-8"))["ads"]


@pytest.fixture(scope="module")
def realt_objects():
    return json.loads(REALT.read_text(encoding="utf-8"))["objects"]


def by_id(ads, ad_id):
    return next(ad for ad in ads if str(ad["ad_id"]) == ad_id)


def test_every_live_kufar_ad_parses(kufar_ads):
    adapter = KufarAdapter(client=object())
    snaps = [adapter.parse(to_raw(ad)) for ad in kufar_ads]
    assert len(snaps) == 20
    assert all(s.rooms and s.area_total and s.price_amount and s.price_usd for s in snaps)
    assert all(s.floor for s in snaps)
    assert sum(1 for s in snaps if s.floors_total) >= 19  # у одного объявления этажности нет
    assert {s.town for s in snaps} == {"Минск"}
    assert Counter(s.price_currency for s in snaps) == {"EUR": 7, "BYN": 7, "USD": 6}
    # Адрес лежит в `v`, `vl` у него пустой: без адреса не работает склейка Kufar↔Realt.
    assert all(s.address_raw and s.address_raw.endswith("Минск") for s in snaps)


def test_kufar_live_addresses_give_street_and_house(kufar_ads):
    adapter = KufarAdapter(client=object())
    parsed = [parse_address(adapter.parse(to_raw(ad)).address_raw) for ad in kufar_ads]
    assert sum(1 for a in parsed if a.street and a.house) >= 18


def test_kufar_price_is_taken_in_the_sellers_currency(kufar_ads):
    """EUR-объявление: цена в евро из calculator, а не пересчёт площадки в доллары."""
    adapter = KufarAdapter(client=object())
    eur = adapter.parse(to_raw(by_id(kufar_ads, "1090906333")))
    assert (eur.price_currency, eur.price_amount) == ("EUR", Decimal(129168))
    assert eur.price_usd == Decimal("144912.30")
    assert (eur.floor, eur.floors_total) == (4, 12)
    assert eur.extra["microdistrict"] == "Минск-Мир"

    usd = adapter.parse(to_raw(by_id(kufar_ads, "1090906384")))
    assert (usd.price_currency, usd.price_amount) == ("USD", Decimal(270000))


def test_kufar_raw_payload_has_no_seller_requisites(kufar_ads):
    for ad in kufar_ads:
        keys = {p.get("p") for p in to_raw(ad).payload.get("account_parameters", [])}
        assert keys <= {"address"}


def test_every_live_realt_object_parses(realt_objects):
    adapter = RealtAdapter(client=object())
    snaps = [adapter.parse(realt_raw(obj)) for obj in realt_objects]
    assert len(snaps) == 360
    assert all(s.area_total and s.price_amount and s.floor and s.street for s in snaps)
    assert sum(1 for s in snaps if s.price_usd) == 360
    assert Counter(s.price_currency for s in snaps) == {"BYN": 205, "USD": 89, "EUR": 66}
    assert all(s.posted_at for s in snaps)
