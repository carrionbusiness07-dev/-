"""Оценка квартиры: год постройки и координаты в срезе рынка. Сети нет."""

from datetime import UTC, datetime, timedelta

import pytest

from minsk_realty.analytics.data import (
    ListingRow,
    MarketData,
    SnapRow,
    building_years,
    load_market,
    parse_year,
)
from minsk_realty.db.models import Base, Listing, RealtyObject, Snapshot
from minsk_realty.db.session import make_engine, make_session_factory

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'assessment.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


def test_parse_year():
    assert parse_year(1978, 2026) == 1978
    assert parse_year(" 1978 ", 2026) == 1978
    for bad in (0, 1700, 2100, None, "abc", "", True, 1978.5, "19.78"):
        assert parse_year(bad, 2026) is None
    assert parse_year(2030, 2026) == 2030  # строящийся дом
    assert parse_year(2037, 2026) is None


def _object(session) -> int:
    obj = RealtyObject(first_seen_at=T0)
    session.add(obj)
    session.flush()
    return obj.id


def _listing(session, oid, source, ext, **kw) -> int:
    row = Listing(
        source=source,
        external_id=ext,
        url=f"https://example.test/{ext}",
        object_id=oid,
        match_confidence="high",
        first_seen=T0,
        last_seen=T0,
        **kw,
    )
    session.add(row)
    session.flush()
    return row.id


def _snap(session, lid, days, **kw) -> None:
    session.add(
        Snapshot(listing_id=lid, seen_at=T0 + timedelta(days=days), content_hash=f"h{days}", **kw)
    )
    session.flush()


def test_building_years_latest_wins(session):
    oid, other = _object(session), _object(session)
    kufar = _listing(session, oid, "kufar", "k1")
    realt = _listing(session, oid, "realt", "r1")
    lone = _listing(session, other, "kufar", "k2")
    _snap(session, kufar, 0, extra={"year_built": 1975})
    _snap(session, kufar, 2, extra={"year_built": 1976})
    _snap(session, kufar, 3, extra={})  # свежий снимок без года не затирает год
    _snap(session, realt, 1, extra={"buildingYear": "1900"})
    _snap(session, lone, 0, extra={"year_built": 9999})  # невалидный год пропускается

    assert building_years(session) == {oid: 1976}
    assert building_years(session, [other]) == {}
    assert building_years(session, [oid]) == {oid: 1976}
    # load_market заполняет годы и при фильтре только по объявлениям
    assert load_market(session).building_years == {oid: 1976}
    assert load_market(session, listing_ids=[realt]).building_years == {oid: 1976}
    assert load_market(session, object_ids=[other]).building_years == {}


def _row(lid, source, oid=1, confidence="high") -> ListingRow:
    return ListingRow(
        id=lid,
        object_id=oid,
        source=source,
        external_id=str(lid),
        url="u",
        confidence=confidence,
        status="active",
        first_seen=T0,
        last_seen=T0,
        posted_at=None,
        seller_type="unknown",
        agency_name=None,
        seller_key=None,
    )


def _sr(lid, days, lat, lon) -> SnapRow:
    return SnapRow(
        lid, T0 + timedelta(days=days), None, None, None, None, None, None, None, None, None,
        lat=lat, lon=lon,
    )  # fmt: skip


def _market(rows, snaps) -> MarketData:
    return MarketData(
        listings={r.id: r for r in rows},
        snapshots={lid: s for lid, s in snaps.items()},
    )


def test_object_point_prefers_precise_source():
    data = _market(
        [_row(1, "kufar"), _row(2, "realt")],
        {1: [_sr(1, 5, 53.9, 27.5)], 2: [_sr(2, 1, 53.91, 27.51)]},
    )
    approx = frozenset({"kufar"})
    assert data.object_point(1, approx) == (53.91, 27.51, False)
    assert data.listing_point(1, approx) == (53.9, 27.5, True)
    assert data.listing_point(2, approx) == (53.91, 27.51, False)

    only_kufar = _market([_row(1, "kufar")], {1: [_sr(1, 5, 53.9, 27.5)]})
    assert only_kufar.object_point(1, approx) == (53.9, 27.5, True)
    assert only_kufar.object_point(2, approx) is None


def test_point_skips_invalid_and_untrusted():
    approx = frozenset({"kufar"})
    data = _market(
        [_row(1, "realt"), _row(2, "realt"), _row(3, "realt", confidence="low")],
        {
            1: [_sr(1, 1, 53.9, 27.5), _sr(1, 2, None, None)],  # снимок без координат пропущен
            2: [_sr(2, 9, 0.0, 0.0)],  # вне Минска — пропущен, берём следующее объявление
            3: [_sr(3, 9, 53.95, 27.6)],  # ненадёжная склейка не в счёт
        },
    )
    assert data.object_point(1, approx) == (53.9, 27.5, False)
    assert data.listing_point(2, approx) is None
