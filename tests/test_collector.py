import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from minsk_realty.collector.__main__ import run_cycle
from minsk_realty.collector.run import INTERRUPTED, close_stale_runs, collect
from minsk_realty.db.models import Base, Event, Listing, Snapshot, SourceRun
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.health.check import Alert, AlertThrottle, check_sources
from minsk_realty.models import ListingSnapshot
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
    assert {a.key for a in alerts} == {"kufar:failing", "kufar:silence"}

    collect(FakeKufar(ads), factory, now=clock(T0 + timedelta(hours=26)))
    with factory() as session:
        assert check_sources(session, ["kufar"], now=T0 + timedelta(hours=27)) == []


def test_blocked_source_alert(factory):
    collect(FakeKufar([], fail_with=SourceBlocked("403")), factory, now=clock(T0))
    with factory() as session:
        [alert] = [a for a in check_sources(session, ["kufar"], now=T0) if a.key == "kufar:blocked"]
        assert "--unblock kufar" in alert.text
        assert session.scalar(select(SourceRun.status)) == "blocked"


class BadRowKufar(FakeKufar):
    """Второе объявление не проходит CHECK в БД (как слишком длинное значение в PostgreSQL)."""

    def parse(self, raw, *, seen_at=None):
        snap = super().parse(raw, seen_at=seen_at)
        if raw.external_id == "1000000002":
            return ListingSnapshot.model_construct(**{**dict(snap), "price_currency": "XXX"})
        return snap


def test_db_error_on_one_listing_keeps_the_rest(factory, ads):
    run = collect(BadRowKufar(ads), factory, now=clock(T0))
    assert (run.status, run.parse_errors, run.finished_at is not None) == ("ok", 1, True)
    with factory() as session:
        assert session.scalars(select(Listing.external_id)).all() == ["1000000001"]


def test_unparsable_listing_is_not_deactivated(factory, ads):
    collect(FakeKufar(ads * 3), factory, now=clock(T0))  # 6 объявлений, два id
    broken = [ads[0]] * 5 + [dict(ads[1], ad_parameters="not-a-list")]
    run = collect(FakeKufar(broken), factory, now=clock(T0 + timedelta(hours=1)))
    assert run.complete and run.parse_errors == 1
    with factory() as session:
        assert set(session.scalars(select(Listing.status))) == {"active"}


def test_parse_error_alert(factory, ads):
    collect(FakeKufar([dict(ad, ad_parameters="x") for ad in ads]), factory, now=clock(T0))
    with factory() as session:
        keys = {a.key for a in check_sources(session, ["kufar"], now=T0 + timedelta(hours=25))}
    assert keys == {"kufar:parse_errors", "kufar:silence"}


def test_alert_throttle():
    throttle = AlertThrottle()
    alert = Alert("kufar:blocked", "текст")
    assert throttle.should_send(alert, T0)
    assert not throttle.should_send(
        Alert("kufar:blocked", "другой текст"), T0 + timedelta(hours=23)
    )
    assert throttle.should_send(alert, T0 + timedelta(hours=25))


def test_run_cycle_skips_blocked_source_and_survives_errors(factory, monkeypatch):
    collect(FakeKufar([], fail_with=SourceBlocked("403")), factory, now=clock(T0))
    created = []

    class Exploding:
        def __init__(self):
            created.append("realt")
            raise RuntimeError("adapter init failed")

    monkeypatch.setattr(
        "minsk_realty.collector.__main__.ADAPTERS",
        {"kufar": lambda: created.append("kufar"), "realt": Exploding},
    )
    monkeypatch.setattr("minsk_realty.collector.__main__.notify", lambda text: None)
    run_cycle(factory, ["kufar", "realt"], set(), AlertThrottle())  # не должно бросить
    assert created == ["realt"]  # kufar пропущен: последний проход заблокирован


def test_run_cycle_runs_matching_after_collect(factory, ads, monkeypatch):
    shown = [ads[:1]]
    monkeypatch.setattr(
        "minsk_realty.collector.__main__.ADAPTERS", {"kufar": lambda: FakeKufar(shown[-1])}
    )
    monkeypatch.setattr("minsk_realty.collector.__main__.notify", lambda text: None)
    run_cycle(factory, ["kufar"], set(), AlertThrottle())
    with factory() as session:
        # Первый полный проход — исходный срез рынка: квартиры не «новые», их просто
        # впервые увидели.
        assert session.scalar(select(Listing.object_id))
        assert list(session.scalars(select(Event.type))) == []

    shown.append(ads)
    run_cycle(factory, ["kufar"], set(), AlertThrottle())
    with factory() as session:
        linked = session.execute(select(Listing.object_id, Listing.match_confidence)).all()
        assert len(linked) == 2 and all(obj and conf == "high" for obj, conf in linked)
        assert list(session.scalars(select(Event.type))) == ["new"]


def test_close_stale_runs_marks_killed_passes_interrupted(factory):
    with factory() as session:
        old = SourceRun(source="kufar", started_at=T0 - timedelta(hours=3), status="running")
        fresh = SourceRun(source="realt", started_at=T0 - timedelta(minutes=10), status="running")
        session.add_all([old, fresh])
        session.commit()

        assert close_stale_runs(session, at=T0) == 1
        assert (old.status, old.error, fresh.status) == ("failed", INTERRUPTED, "running")
        # При запуске постоянного сборщика «идущих» проходов быть не может: закрыть все.
        assert close_stale_runs(session, older_than=None, at=T0) == 1
        assert fresh.status == "failed"
        session.commit()
        # Прерванные проходы — не вина площадки: «3 сбоя подряд» из них не складываются.
        session.add(SourceRun(source="kufar", started_at=T0, status="running"))
        session.commit()
        close_stale_runs(session, older_than=None, at=T0 + timedelta(hours=1))
        session.commit()
        assert check_sources(session, ["kufar"], now=T0 + timedelta(hours=1)) == []
