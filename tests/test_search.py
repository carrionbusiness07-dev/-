"""Поиск квартир: индекс, фильтры, порядок, подсказки, бюджет, строка запроса, детали квартиры."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest
from analytics_data import DAY, HOUR, NOW, OBS_START

from minsk_realty.analytics.data import (
    EventRow,
    FlatDetails,
    ListingRow,
    MarketData,
    ObjectRow,
    SnapRow,
    listing_details,
    load_market,
)
from minsk_realty.analytics.medians import compute_baseline
from minsk_realty.analytics.search import (
    SearchItem,
    SearchQuery,
    build_index,
    parse_budget,
    search,
    suggestions,
    summary_line,
)
from minsk_realty.db.models import Base, Listing, RealtyObject, Snapshot
from minsk_realty.db.session import make_engine, make_session_factory


def _listing(id_, oid, source, *, status="active", last=NOW - HOUR, seller="agency"):
    return ListingRow(
        id=id_,
        object_id=oid,
        source=source,
        external_id=str(id_),
        url=f"https://example.invalid/{source}/{id_}",
        confidence="high",
        status=status,
        first_seen=OBS_START + id_ * DAY,
        last_seen=last,
        posted_at=None,
        seller_type=seller,
        agency_name=None,
        seller_key=f"s{id_}",
    )


def _snap(lid, amount, *, at=OBS_START, lat=None, lon=None):
    return SnapRow(
        listing_id=lid,
        seen_at=at,
        amount=Decimal(amount),
        currency="USD",
        usd=Decimal(amount),
        area=54.0,
        rooms=2,
        floor=5,
        floors_total=9,
        street=None,
        district=None,
        lat=lat,
        lon=lon,
    )


def test_one_card_per_object():
    data = MarketData(source_starts={"kufar": OBS_START, "realt": OBS_START})
    for oid in (1, 2, 3):
        data.objects[oid] = ObjectRow(
            oid, "ул притыцкого", str(oid), 2, 54.0, 5, 9, "фрунзенский", None, OBS_START
        )
    # Объект 1: Realt дороже, Kufar дешевле и с точной точкой у «Спортивной».
    data.listings[1] = _listing(1, 1, "realt", seller="owner")
    data.listings[2] = _listing(2, 1, "kufar", last=NOW - 2 * HOUR, seller="owner")
    data.snapshots[1] = [_snap(1, 95000)]
    data.snapshots[2] = [_snap(2, 92500, lat=53.90500, lon=27.47850)]
    # Объект 2 снят давно, объект 3 без цены — в выдачу не идут.
    data.listings[3] = _listing(3, 2, "kufar", status="inactive", last=NOW - 10 * DAY)
    data.snapshots[3] = [_snap(3, 80000)]
    data.listings[4] = _listing(4, 3, "kufar")
    data.snapshots[4] = [replace(_snap(4, 1), amount=None, currency=None, usd=None)]
    data.details[1] = FlatDetails(kitchen_m2=9.0)
    data.reindex()

    index = build_index(data, compute_baseline(data, NOW), NOW)

    [item] = index
    assert item.object_id == 1 and item.sources == ("kufar", "realt")
    assert (item.listing_id, item.url_source, item.url) == (2, "kufar", data.listings[2].url)
    assert (item.price_amount, item.price_currency, item.price_usd) == (
        Decimal(92500),
        "USD",
        92500,
    )
    assert (item.rooms, item.area, item.floor, item.floors_total) == (2, 54.0, 5, 9)
    assert item.district == "Фрунзенский" and item.street == "ул. Притыцкого"
    assert (item.metro, item.metro_min) == ("Спортивная", 5)
    assert item.owner is True and item.checked_at == NOW - HOUR
    assert item.first_seen == OBS_START + DAY and item.days == (NOW - OBS_START - DAY).days
    assert item.new_building is None and item.housing is None
    assert item.group is None and item.details == FlatDetails(kitchen_m2=9.0)


def test_drop_seen_on_two_platforms_counts_once():
    data = MarketData(source_starts={"kufar": OBS_START, "realt": OBS_START})
    data.objects[1] = ObjectRow(
        1, "ул притыцкого", "1", 2, 54.0, 5, 9, "фрунзенский", None, OBS_START
    )
    data.listings[1] = _listing(1, 1, "realt")
    data.listings[2] = _listing(2, 1, "kufar")
    data.snapshots[1] = [_snap(1, 90000)]
    data.snapshots[2] = [_snap(2, 90000)]
    at = NOW - 2 * DAY
    payload = {"old": "95000", "new": "90000", "currency": "USD"}
    data.events[1] = [EventRow(i, 1, i, "price_drop", at, at, True, payload) for i in (1, 2)]
    data.reindex()

    [item] = build_index(data, compute_baseline(data, NOW), NOW)

    assert item.drops == 1


def _item(oid, **kw) -> SearchItem:
    data = {
        "object_id": oid,
        "rooms": 2,
        "area": 50.0,
        "floor": 3,
        "floors_total": 9,
        "district": "Фрунзенский",
        "new_building": False,
        "price_amount": Decimal(80000),
        "price_currency": "USD",
        "price_usd": 80000.0,
        "usd_per_m2": 1600.0,
        "group": "market",
        "diff_pct": 0.0,
        "metro": None,
        "metro_min": None,
        "days": 10,
        "drops": 0,
        "sources": ("kufar",),
        "owner": None,
        "url": f"https://example.invalid/{oid}",
        "url_source": "kufar",
        "listing_id": oid,
        "checked_at": NOW,
        "first_seen": NOW - oid * DAY,
        "street": None,
        "year": None,
        "housing": None,
        "details": None,
    }
    return SearchItem(**(data | kw))


def _ids(index, query, sort="best"):
    return [i.object_id for i in search(index, query, sort).items]


def test_filters():
    index = (
        _item(1),
        _item(2, price_usd=95000.0),
        _item(3, rooms=4),
        _item(4, rooms=5),
        _item(5, rooms=None),
        _item(6, district="Центральный"),
        _item(7, district=None),
        _item(8, area=40.0),
        _item(9, new_building=True),
        _item(10, new_building=None),
        _item(11, floor=1),
        _item(12, floor=9),
        _item(13, floor=None),
        _item(14, floors_total=None),
    )
    everything = {i.object_id for i in index}
    assert set(_ids(index, SearchQuery())) == everything
    assert 2 not in _ids(index, SearchQuery(price_max=90000))
    assert _ids(index, SearchQuery(price_min=90000)) == [2]
    assert set(_ids(index, SearchQuery(rooms=frozenset({4})))) == {3, 4}
    assert 5 not in _ids(index, SearchQuery(rooms=frozenset({2})))
    assert set(_ids(index, SearchQuery(districts=frozenset({"Центральный"})))) == {6}
    assert 7 not in _ids(index, SearchQuery(districts=frozenset({"Фрунзенский"})))
    assert 8 not in _ids(index, SearchQuery(area_min=50))
    assert _ids(index, SearchQuery(housing="new")) == [9]
    assert set(_ids(index, SearchQuery(housing="old"))) == everything - {9, 10}
    assert set(_ids(index, SearchQuery(not_first=True))) == everything - {11, 13}
    assert set(_ids(index, SearchQuery(not_last=True))) == everything - {12, 13, 14}


def test_no_usd_never_in_budget():
    index = (_item(1, price_usd=None), _item(2))
    assert _ids(index, SearchQuery(price_max=10**9)) == [2]
    assert _ids(index, SearchQuery(price_min=1)) == [2]
    assert set(_ids(index, SearchQuery())) == {1, 2}


def test_no_area_only_without_area_filter():
    index = (_item(1, area=None), _item(2))
    assert _ids(index, SearchQuery(area_min=30)) == [2]
    assert set(_ids(index, SearchQuery())) == {1, 2}


def test_best_order():
    index = (
        _item(1, group=None, diff_pct=None, usd_per_m2=2000.0, price_usd=70000.0),
        _item(2, group="above", diff_pct=15.0),
        _item(3, group="suspicious", diff_pct=-30.0),
        _item(4, group="market", diff_pct=5.0, price_usd=60000.0),
        _item(5, group="market", diff_pct=-5.0),
        _item(6, group="below", diff_pct=-12.0),
        _item(7, group="below", diff_pct=-20.0, price_usd=None),
        _item(8, group=None, diff_pct=None, usd_per_m2=1500.0),
    )
    result = search(index, SearchQuery())
    assert [i.object_id for i in result.items] == [7, 6, 5, 4, 3, 2, 8, 1]
    assert result.counts == {"below": 2, "market": 2, "suspicious": 1, "above": 1, "none": 2}
    assert _ids(index, SearchQuery(), "cheap") == [4, 1, 2, 3, 5, 6, 8, 7]
    assert _ids(index, SearchQuery(), "fresh") == [1, 2, 3, 4, 5, 6, 7, 8]


def test_summary_line():
    index = (
        _item(1, group="below"),
        _item(2),
        _item(3, group="above"),
        _item(4, group=None),
        *(_item(i) for i in range(5, 22)),
    )
    assert (
        summary_line(search(index, SearchQuery()), "best")
        == "Нашёл 21 квартиру: 1 дешевле похожих, 18 в рынке, 1 дороже, 1 без сравнения. "
        "Показываю сначала выгодные."
    )
    two = search((_item(1, group="suspicious"), _item(2)), SearchQuery(), "cheap")
    assert summary_line(two, "cheap") == (
        "Нашёл 2 квартиры: 1 в рынке, 1 подозрительно дёшево. Показываю сначала дешёвые."
    )


def test_suggestions():
    index = (
        _item(1, price_usd=95000.0),
        _item(2, price_usd=99000.0),
        _item(3, district="Центральный"),
        _item(4, district="Центральный", price_usd=85000.0),
        _item(5, district="Центральный", price_usd=70000.0),
        _item(6, area=40.0),
        _item(7, rooms=3, price_usd=200000.0),
    )
    query = SearchQuery(
        price_max=90000, rooms=frozenset({2}), districts=frozenset({"Фрунзенский"}), area_min=50
    )
    assert search(index, query).items == ()
    found = suggestions(index, query)
    assert [(text, n) for text, _, n in found] == [
        ("Если любой район — 3 квартиры", 3),
        ("Если поднять бюджет до $100 000 — 2 квартиры", 2),
    ]
    assert found[1][1] == replace(query, price_max=100000)
    # Площадь даёт 1, комнаты — 0: нулевые не показываются вовсе.
    assert [n for _, _, n in suggestions(index, query, limit=5)] == [3, 2, 1]
    # Бюджет выше последней кнопки поднять некуда.
    assert all(
        "бюджет" not in t for t, _, _ in suggestions(index, replace(query, price_max=130000))
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("85000", (None, 85000)),
        ("85", (None, 85000)),
        ("70-90", (70000, 90000)),
        ("90-70", (70000, 90000)),
        ("70 – 90", (70000, 90000)),
        ("85 тыс", (None, 85000)),
        ("85 тыс.", (None, 85000)),
        ("$85 000", (None, 85000)),
        ("85к", (None, 85000)),
        ("abc", None),
        ("0", None),
        ("-5", None),
        ("", None),
    ],
)
def test_parse_budget(text, expected):
    assert parse_budget(text) == expected


def test_label_and_json_roundtrip():
    query = SearchQuery(
        price_max=90000,
        rooms=frozenset({2}),
        districts=frozenset({"Центральный", "Фрунзенский"}),
        area_min=50,
    )
    assert query.label() == "До $90 000 · 2-комн. · Фрунзенский, Центральный · от 50 м²"
    assert SearchQuery().label() == "Любые квартиры"
    full = SearchQuery(
        price_min=70000,
        price_max=90000,
        rooms=frozenset({1, 2, 4}),
        housing="new",
        not_first=True,
        not_last=True,
    )
    assert full.label() == (
        "$70 000–90 000 · 1, 2, 4+ комн. · новостройка · не первый этаж · не последний этаж"
    )
    assert SearchQuery(price_min=130000, housing="old").label() == "От $130 000 · вторичка"
    for q in (query, full, SearchQuery()):
        assert SearchQuery.from_json(q.to_json()) == q
    assert SearchQuery.from_json({}) == SearchQuery()


# --- Детали квартиры из extra -------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'search.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


def _db_listing(session, oid, ext, confidence="high") -> int:
    row = Listing(
        source="kufar",
        external_id=ext,
        url=f"https://example.invalid/{ext}",
        object_id=oid,
        match_confidence=confidence,
        seller_type="owner",
        status="active",
        first_seen=OBS_START,
        last_seen=NOW,
    )
    session.add(row)
    session.flush()
    return row.id


def _db_snap(session, lid, at: datetime, extra: dict) -> None:
    session.add(
        Snapshot(
            listing_id=lid, seen_at=at, content_hash=f"{lid}-{at}", price_amount=1, extra=extra
        )
    )


def test_details_merge(session):
    obj = RealtyObject(street_norm="ул тестовая", house="1", rooms=2, first_seen_at=OBS_START)
    session.add(obj)
    session.flush()
    old, fresh, low = (
        _db_listing(session, obj.id, e, c)
        for e, c in (("1", "high"), ("2", "medium"), ("3", "low"))
    )
    _db_snap(
        session, old, OBS_START, {"kitchen_m2": 7.0, "bathroom": "раздельный", "facts": ["bargain"]}
    )
    # Новый снимок старого объявления: детали те же, факт «торг» ушёл из описания.
    _db_snap(
        session,
        old,
        OBS_START + DAY,
        {"kitchen_m2": 7.0, "bathroom": "раздельный", "facts": ["furniture"]},
    )
    _db_snap(
        session, fresh, OBS_START + 2 * DAY, {"kitchen_m2": 9.0, "facts": ["share", "furniture"]}
    )
    _db_snap(session, low, OBS_START + 3 * DAY, {"walls": "панельный", "facts": ["urgent"]})
    session.flush()

    expected = FlatDetails(kitchen_m2=9.0, bathroom="раздельный", facts=("share", "furniture"))
    assert listing_details(session) == {obj.id: expected}
    assert listing_details(session, [obj.id + 1]) == {}
    assert load_market(session).details == {obj.id: expected}
    assert load_market(session, object_ids=[obj.id]).details == {obj.id: expected}
