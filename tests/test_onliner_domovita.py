"""Адаптеры Onliner и Domovita на живой выдаче (снята 23.10.2026, без персональных данных)."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from minsk_realty.sources.base import PoliteClient
from minsk_realty.sources.domovita import (
    DomovitaAdapter,
    extract_next_data,
    listing_cards,
    original_price,
)
from minsk_realty.sources.domovita import to_raw as domovita_raw
from minsk_realty.sources.onliner import OnlinerAdapter, is_minsk, mirror_of, original_photo_url
from minsk_realty.sources.onliner import to_raw as onliner_raw

FIXTURES = Path(__file__).parent / "fixtures"
ONLINER = FIXTURES / "onliner" / "search_sale_minsk.2026-10-23.json"
DOMOVITA_HTML = FIXTURES / "domovita" / "flats_sale_minsk.2026-10-23.html"
DOMOVITA_PAGE2 = FIXTURES / "domovita" / "flats_sale_minsk_page2.2026-10-23.json"


@pytest.fixture(scope="module")
def onliner_page():
    return json.loads(ONLINER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def domovita_items():
    page = extract_next_data(DOMOVITA_HTML.read_text(encoding="utf-8"))
    return listing_cards(page["props"]["pageProps"])["items"]


def client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)


# --- Onliner ------------------------------------------------------------------------------


def test_onliner_mirrors_of_realt_are_recognised(onliner_page):
    items = [i for i in onliner_page["apartments"] if is_minsk(i)]
    mirrors = Counter(mirror_of(i) for i in items)
    # Больше половины выдачи Onliner — копии объявлений Realt (фото с files.realt.by).
    assert mirrors["realt"] == 47 and mirrors[None] == 33
    first = onliner_page["apartments"][0]
    assert original_photo_url(first["photo"]).startswith("https://files.realt.by/img/")


def test_onliner_minsk_filter(onliner_page):
    towns = Counter(is_minsk(i) for i in onliner_page["apartments"])
    assert towns == {True: 80, False: 16}  # пригороды и пустой адрес отброшены
    assert is_minsk({"location": {"address": "Беларусь, Минск, Мира просп., 16"}})
    assert is_minsk({"location": {"address": "Minsk, вуліца Жылуновіча, 43"}})
    assert not is_minsk({"location": {"address": "Колодищи, Березовая Аллея ул. 37"}})


def test_onliner_native_listings_parse(onliner_page):
    adapter = OnlinerAdapter(client=object())
    items = [i for i in onliner_page["apartments"] if is_minsk(i) and not mirror_of(i)]
    snaps = [adapter.parse(onliner_raw(i)) for i in items]
    assert all(s.rooms and s.area_total and s.floor and s.floors_total for s in snaps)
    assert all(s.price_amount and s.price_usd and s.address_raw for s in snaps)
    assert Counter(s.price_currency for s in snaps) == {"BYN": 21, "USD": 12}
    assert Counter(s.seller_type for s in snaps) == {"agency": 21, "owner": 12}
    snap = next(s for s in snaps if s.external_id == "1287048")
    assert snap.url == "https://r.onliner.by/pk/apartments/1287048"
    assert (snap.price_currency, snap.price_amount) == ("USD", Decimal("169000.00"))
    assert snap.posted_at.isoformat() == "2026-10-16T13:12:51+03:00"


def test_onliner_payload_has_no_seller_account(onliner_page):
    raw = onliner_raw(onliner_page["apartments"][0])
    assert "author_id" not in raw.payload and raw.payload["_seller_key"]


def test_onliner_search_skips_mirrors_and_pages(onliner_page):
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.host == "pk.api.onliner.by"
        assert request.url.params["bounds[lb][lat]"] == "53.7"
        body = dict(onliner_page, page={"limit": 96, "items": 96, "current": page, "last": 2})
        if page == 2:  # те же объявления под другими id — проверить, что страница читается
            body["apartments"] = [dict(i, id=i["id"] + 10**7) for i in onliner_page["apartments"]]
        return httpx.Response(200, json=body)

    adapter = OnlinerAdapter(client=client(handler))
    raws = list(adapter.search())
    assert pages == [1, 2] and adapter.last_search_complete
    assert len(raws) == 66 and adapter.mirrors_skipped == 94  # по 47 на каждой странице


def test_onliner_resolve_url():
    adapter = OnlinerAdapter(client=object())
    assert adapter.resolve_url("https://r.onliner.by/pk/apartments/1287048?x=1") == "1287048"
    assert adapter.resolve_url("https://r.onliner.by/ak/apartments/1287048") is None


# --- Domovita -----------------------------------------------------------------------------


def test_domovita_listings_parse(domovita_items):
    adapter = DomovitaAdapter(client=object())
    snaps = [adapter.parse(domovita_raw(i)) for i in domovita_items]
    assert len(snaps) == 20
    assert all(s.rooms and s.area_total and s.floor and s.street and s.house for s in snaps)
    assert all(s.lat and 53.7 < s.lat < 54.1 and s.lon and 27.3 < s.lon < 27.8 for s in snaps)
    assert Counter(s.price_currency for s in snaps) == {"BYN": 16, "USD": 3, "EUR": 1}
    snap = snaps[0]
    assert snap.external_id == "1-komnatnaa-kvartira-ul-bratskaa-42-114"
    assert (snap.price_currency, snap.price_amount) == ("EUR", Decimal(59208))
    assert snap.price_usd == Decimal("66424.87")
    assert (snap.street, snap.house, snap.district) == ("ул. Братская", "4/2", "Октябрьский район")
    assert snap.seller_type == "owner"
    assert snap.posted_at.isoformat() == "2026-07-30T04:24:53+03:00"
    # Domovita сама отдаёт историю цены объявления (в долларах).
    assert snap.extra["price_history"] == [["2026-08-13", 68306], ["2026-07-30", 69089]]
    assert snap.extra["microdistrict"] == "Маяковского"
    studio = next(s for s in snaps if "studia" in s.external_id)
    assert studio.rooms == 1 and studio.extra["studio"]


def test_domovita_price_currency_is_the_round_amount():
    assert original_price({"USD": 70123.45, "BYN": 210000.0, "EUR": 60111.9}) == (
        "BYN",
        Decimal(210000),
    )
    assert original_price({"USD": 70000, "BYN": 210123.4}) == ("USD", Decimal(70000))
    assert original_price({"USD": 70000.5, "BYN": 210123.4}) == (None, None)


def test_domovita_payload_is_a_whitelist(domovita_items):
    item = dict(domovita_items[0])
    item["contacts"] = {"phones": ["(29) 111-22-33"], "contact_name": "Анна"}
    item["description"] = "Звоните Анне +375291112233"
    payload = domovita_raw(item).payload
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "Анна" not in dumped and "111" not in dumped and "Анне" not in dumped
    assert "user_id" not in payload and payload["_seller_key"]
    assert payload["town"] == "Минск" and payload["district"] == "Октябрьский район"


def test_domovita_search_uses_data_route_and_recovers_build_id(domovita_items):
    html = DOMOVITA_HTML.read_text(encoding="utf-8")
    page2 = json.loads(DOMOVITA_PAGE2.read_text(encoding="utf-8"))
    total = 60  # три страницы по 20
    html = html.replace('"totalCount": 3063', f'"totalCount": {total}')
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, page = request.url.path, request.url.params.get("page", "1")
        calls.append((path.split("/")[1], page))
        if path == "/minsk/flats/sale":
            text = html if page == "1" else html.replace("3Y50mxX3BuQdnrEOcgmpQ", "NEWBUILD")
            return httpx.Response(200, text=text)
        if "/_next/data/3Y50mxX3BuQdnrEOcgmpQ/" in path and page == "3":
            return httpx.Response(404)  # сайт выложили заново посреди обхода
        assert path.endswith("/minsk/flats/sale.json") and request.url.params["city"] == "minsk"
        return httpx.Response(200, json=page2)

    adapter = DomovitaAdapter(client=client(handler))
    raws = list(adapter.search())
    assert adapter.last_search_complete
    assert calls == [
        ("minsk", "1"),
        ("_next", "2"),
        ("_next", "3"),
        ("minsk", "3"),  # 404 по старому buildId → HTML той же страницы
    ]
    # Платные объявления повторяются на соседних страницах — каждое берётся один раз.
    assert len(raws) == len({r.external_id for r in raws}) == 38


def test_domovita_resolve_url():
    adapter = DomovitaAdapter(client=object())
    url = "https://domovita.by/minsk/flats/sale/1-komnatnaa-kvartira-ul-bratskaa-42-114"
    assert adapter.resolve_url(url) == "1-komnatnaa-kvartira-ul-bratskaa-42-114"
    assert adapter.resolve_url("https://domovita.by/minsk/flats/sale?page=2") is None
