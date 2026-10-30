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
    compact,
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


def test_onliner_empty_page_before_last_is_not_complete(onliner_page):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body = dict(onliner_page, page={"limit": 96, "items": 96, "current": page, "last": 38})
        if page == 2:
            body["apartments"] = []  # сбой или мягкая блокировка посреди выдачи
        return httpx.Response(200, json=body)

    adapter = OnlinerAdapter(client=client(handler))
    assert len(list(adapter.search())) == 33
    assert not adapter.last_search_complete


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
    assert Counter(s.price_currency for s in snaps) == {"BYN": 15, "USD": 4, "EUR": 1}
    # 220 000 $ при курсе даёт круглые 649 946 BYN: ничья решается в пользу доллара,
    # иначе курс каждый день давал бы ложные «подешевела/подорожала».
    rakovskaa = next(s for s in snaps if "rakovskaa" in s.url)
    assert (rakovskaa.price_currency, rakovskaa.price_amount) == ("USD", Decimal(220000))
    snap = snaps[0]
    # id — номер карточки: адрес страницы меняется при правке объявления.
    assert snap.external_id == "1247572"
    assert snap.url.endswith("/1-komnatnaa-kvartira-ul-bratskaa-42-114")
    assert (snap.price_currency, snap.price_amount) == ("EUR", Decimal(59208))
    assert snap.price_usd == Decimal("66424.87")
    assert (snap.street, snap.house, snap.district) == ("ул. Братская", "4/2", "Октябрьский район")
    assert snap.seller_type == "owner"
    assert snap.posted_at.isoformat() == "2026-07-30T04:24:53+03:00"
    # Domovita сама отдаёт историю цены объявления (в долларах, даты по Минску).
    assert snap.extra["price_history"] == [["2026-08-14", 68306], ["2026-07-30", 69089]]
    assert snap.extra["microdistrict"] == "Маяковского"
    studio = next(s for s in snaps if "studia" in s.url)
    assert studio.rooms == 1 and studio.extra["studio"]


def test_domovita_price_currency_is_the_round_amount():
    assert original_price({"USD": 70123.45, "BYN": 210000.0, "EUR": 60111.9}) == (
        "BYN",
        Decimal(210000),
    )
    assert original_price({"USD": 70000, "BYN": 210123.4}) == ("USD", Decimal(70000))
    assert original_price({"USD": 220000, "BYN": 649946.0}) == ("USD", Decimal(220000))
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


def next_data_html(items, total, build):
    data = {
        "buildId": build,
        "props": {"pageProps": {"listingCardsFromSSR": {"totalCount": total, "items": items}}},
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script>"
    )


def test_domovita_search_uses_data_route_and_recovers_build_id(domovita_items):
    page2 = json.loads(DOMOVITA_PAGE2.read_text(encoding="utf-8"))
    page3 = [
        dict(i, id=i["id"] + 10**7) for i in page2["pageProps"]["listingCardsFromSSR"]["items"]
    ]
    total = 60  # три страницы по 20
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, page = request.url.path, request.url.params.get("page", "1")
        calls.append((path.split("/")[1], page))
        if path == "/minsk/flats/sale":
            if page == "1":
                return httpx.Response(200, text=next_data_html(domovita_items, total, "OLD"))
            return httpx.Response(200, text=next_data_html(page3, total, "NEW"))
        if "/_next/data/OLD/" in path and page == "3":
            return httpx.Response(404)  # сайт выложили заново посреди обхода
        assert path.endswith("/minsk/flats/sale.json") and request.url.params["city"] == "minsk"
        return httpx.Response(200, json=page2)

    adapter = DomovitaAdapter(client=client(handler))
    raws = list(adapter.search())
    assert calls == [
        ("minsk", "1"),
        ("_next", "2"),
        ("_next", "3"),
        ("minsk", "3"),  # 404 по старому buildId → HTML той же страницы
        ("_next", "4"),  # 3-я страница дала новые объявления — хвост мог уехать дальше
    ]
    # Объявление на стыке страниц (сдвиг выдачи) берётся один раз.
    assert len(raws) == len({r.external_id for r in raws}) == 58
    assert adapter.last_search_complete  # 58 из 60 — почти вся выдача


def test_domovita_partial_pass_is_not_complete(domovita_items):
    """Не дошли до конца выдачи — объявления, которых не видели, гасить нельзя."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/minsk/flats/sale":
            return httpx.Response(200, text=next_data_html(domovita_items, 3063, "B"))
        return httpx.Response(200, json={"pageProps": {"listingCardsFromSSR": {"items": []}}})

    adapter = DomovitaAdapter(client=client(handler))
    assert len(list(adapter.search())) == 20
    assert not adapter.last_search_complete  # пустая 2-я страница из 154 — обрыв


def test_domovita_house_letter_after_slash(domovita_items):
    item = dict(domovita_items[0])
    item["address"] = dict(item["address"], house_number="9/а")
    snap = DomovitaAdapter(client=object()).parse(domovita_raw(item))
    assert snap.house == "9а"


def test_domovita_resolve_url():
    adapter = DomovitaAdapter(client=object())
    url = "https://domovita.by/minsk/flats/sale/1-komnatnaa-kvartira-ul-bratskaa-42-114"
    # Ссылка даёт адрес страницы; объявление по нему находит db.repository.find_listing.
    assert adapter.resolve_url(url) == "1-komnatnaa-kvartira-ul-bratskaa-42-114"
    assert adapter.resolve_url("https://domovita.by/minsk/flats/sale?page=2") is None


def test_domovita_link_finds_listing_stored_under_numeric_id(tmp_path, domovita_items):
    from minsk_realty.analytics.report import resolve_ref
    from minsk_realty.db.models import Base
    from minsk_realty.db.repository import save_snapshot
    from minsk_realty.db.session import make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'd.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        snap = DomovitaAdapter(client=object()).parse(domovita_raw(domovita_items[0]))
        listing, _ = save_snapshot(session, snap)
        session.commit()
        url = "https://domovita.by/minsk/flats/sale/1-komnatnaa-kvartira-ul-bratskaa-42-114?utm=x"
        found, source, understood = resolve_ref(session, url)
        assert (found.id, source, understood) == (listing.id, "domovita", True)
        assert found.external_id == "1247572"


def test_not_found_onliner_explains_realt_copies():
    from datetime import UTC, datetime

    from minsk_realty.analytics import render_check_text
    from minsk_realty.analytics.report import CheckReport

    report = CheckReport(
        status="not_found",
        query="https://r.onliner.by/pk/apartments/1",
        generated_at=datetime(2026, 10, 24, tzinfo=UTC),
        observation_start=None,
        source="onliner",
    )
    text = render_check_text(report)
    assert "раз в 30–60 минут" in text and "realt.by" in text
    domovita = render_check_text(
        CheckReport(
            status="not_found",
            query="x",
            generated_at=report.generated_at,
            observation_start=None,
            source="domovita",
        )
    )
    assert "раз в 6 часов" in domovita


def test_domovita_404_past_the_end_finishes_the_pass(domovita_items):
    """Платные объявления на каждой странице раздвигают выдачу: настоящих страниц больше
    расчётных. Страница за концом отвечает 404 — это конец выдачи, а не сбой прохода."""
    page_items = {
        page: [dict(i, id=i["id"] + page * 10**6) for i in domovita_items][:17] + domovita_items[:3]
        for page in range(1, 5)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page not in page_items:
            return httpx.Response(404)
        html = next_data_html(page_items[page], 68, "B")
        if request.url.path.startswith("/_next/"):
            return httpx.Response(
                200,
                json={
                    "pageProps": {
                        "listingCardsFromSSR": {"totalCount": 68, "items": page_items[page]}
                    }
                },
            )
        return httpx.Response(200, text=html)

    adapter = DomovitaAdapter(client=client(handler))
    raws = list(adapter.search())
    # 4 страницы по 17 своих объявлений + 3 повторяющихся платных = 71 уникальное из 68 заявленных
    assert len(raws) == 71
    assert adapter.last_search_complete


def test_domovita_compact_turns_ads_text_into_facts(domovita_items):
    item = dict(domovita_items[0], ads_text="Срочно, торг", title="заголовок")
    payload = compact(item)
    assert "ads_text" not in payload and "title" not in payload
    assert payload["_facts"] == ["bargain", "urgent"]
    snap = DomovitaAdapter(client=object()).parse(domovita_raw(item))
    assert snap.extra["facts"] == ["bargain", "urgent"]
    assert (
        "facts" not in DomovitaAdapter(client=object()).parse(domovita_raw(domovita_items[0])).extra
    )
