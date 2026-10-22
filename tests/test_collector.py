import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from minsk_realty.collector.run import collect
from minsk_realty.db.models import Base, Listing, Snapshot, SourceRun
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.health.check import check_sources
from minsk_realty.sources.base import SourceBlocked
from minsk_realty.sources.kufar import KufarAdapter, to_raw

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeKufar(KufarAdapter):
    """Настоящий парсер Kufar поверх заранее заданной выдачи — без сети."""

    def __init__(self, ads, *, complete=True, fail_with=None):
        super().__init__(client=object())
        self._ads, self._complete, self._fail_with = ads, complete, fail_with

    def search(self, *, max_pages=500):
        self.last_search_complete = False
        for ad in self._ads:
            yield to_raw(ad)
        if self._fail_with:
            raise self._fail_with
        self.last_search_complete = self._complete


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'collector.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def ads(fixtures_dir):
    return json.loads((fixtures_dir / "kufar" / "search_sell_minsk.synthetic.json").read_text())[
        "ads"
    ]


def clock(start):
    times = iter(start + timedelta(seconds=i) for i in range(1000))
    return lambda: next(times)


def test_complete_run_saves_listings_and_deactivates_missing(factory, ads):
    first = collect(FakeKufar(ads), factory, now=clock(T0))
    assert (first.status, first.complete, first.listings_seen) == ("ok", True, 2)

    later = T0 + timedelta(hours=1)
    second = collect(FakeKufar(ads[:1]), factory, now=clock(later))
    assert second.complete
    with factory() as session:
        statuses = dict(session.execute(select(Listing.external_id, Listing.status)).all())
        assert statuses == {"1000000001": "active", "1000000002": "inactive"}
        # Цена не менялась — второй проход новых снимков не дал.
        assert len(session.scalars(select(Snapshot)).all()) == 2


def test_blocked_run_keeps_listings_active(factory, ads):
    collect(FakeKufar(ads), factory, now=clock(T0))
    run = collect(
        FakeKufar(ads[:1], fail_with=SourceBlocked("403 x3")),
        factory,
        now=clock(T0 + timedelta(hours=1)),
    )
    assert run.status == "blocked" and not run.complete
    with factory() as session:
        assert set(session.scalars(select(Listing.status))) == {"active"}


def test_too_many_parse_errors_make_run_incomplete(factory, ads):
    broken = [dict(ad, ad_parameters="not-a-list") for ad in ads]
    run = collect(FakeKufar(broken), factory, now=clock(T0))
    assert run.status == "ok" and run.parse_errors == 2 and not run.complete


def test_health_alerts(factory, ads):
    with factory() as session:
        assert check_sources(session, ["kufar"], now=T0) == []

    for i in range(3):
        collect(
            FakeKufar([], fail_with=RuntimeError("boom")),
            factory,
            now=clock(T0 + timedelta(hours=i)),
        )
    with factory() as session:
        alerts = check_sources(session, ["kufar"], now=T0 + timedelta(hours=25))
    assert any("подряд с ошибкой" in a for a in alerts)
    assert any("0 объявлений" in a for a in alerts)

    collect(FakeKufar(ads), factory, now=clock(T0 + timedelta(hours=26)))
    with factory() as session:
        assert check_sources(session, ["kufar"], now=T0 + timedelta(hours=27)) == []


def test_blocked_source_alert(factory):
    collect(FakeKufar([], fail_with=SourceBlocked("403")), factory, now=clock(T0))
    with factory() as session:
        [alert] = [a for a in check_sources(session, ["kufar"], now=T0) if "отказывает" in a]
        assert "kufar" in alert
        assert session.scalar(select(SourceRun.status)) == "blocked"
