import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from minsk_realty.db.models import Base, Listing, Snapshot, SourceRun
from minsk_realty.db.repository import mark_unseen_inactive, save_snapshot
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.sources.kufar import KufarAdapter, to_raw

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MINSK = timezone(timedelta(hours=3))
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def session(factory):
    with factory() as session:
        yield session


def run(started_at, *, status="ok", complete=True):
    return SourceRun(source="kufar", started_at=started_at, status=status, complete=complete)


@pytest.fixture
def snap(fixtures_dir):
    data = json.loads((fixtures_dir / "kufar" / "search_sell_minsk.synthetic.json").read_text())
    return KufarAdapter(client=object()).parse(to_raw(data["ads"][0]), seen_at=T0)


def test_first_save_creates_listing_and_snapshot(session, snap):
    listing, snapshot = save_snapshot(session, snap)
    assert listing.first_seen == listing.last_seen == T0
    assert snapshot.price_amount == Decimal("92500.00") and snapshot.price_currency == "USD"
    assert listing.seller_type == "agency" and listing.seller_key == snap.seller_key


def test_unchanged_listing_only_moves_last_seen(session, snap):
    save_snapshot(session, snap)
    listing, snapshot = save_snapshot(
        session, snap.model_copy(update={"seen_at": T0 + timedelta(hours=1)})
    )
    assert snapshot is None
    assert listing.last_seen == T0 + timedelta(hours=1)
    assert session.scalar(select(Snapshot).where(Snapshot.listing_id == listing.id)) is not None
    assert len(listing.snapshots) == 1


def test_price_change_writes_new_snapshot(session, snap):
    save_snapshot(session, snap)
    cheaper = snap.model_copy(
        update={"seen_at": T0 + timedelta(days=3), "price_amount": Decimal(90000)}
    )
    listing, snapshot = save_snapshot(session, cheaper)
    assert snapshot is not None
    assert [s.price_amount for s in listing.snapshots] == [Decimal("92500.00"), Decimal("90000.00")]


def test_usd_rate_change_alone_is_not_a_change(session, snap):
    """Цена в BYN та же, пересчёт площадки в USD поменялся — это не изменение цены."""
    byn = snap.model_copy(update={"price_currency": "BYN", "price_amount": Decimal(305250)})
    save_snapshot(session, byn)
    moved = byn.model_copy(update={"seen_at": T0 + timedelta(days=1), "price_usd": Decimal(91000)})
    assert save_snapshot(session, moved)[1] is None


def test_mark_unseen_inactive(session, snap):
    save_snapshot(session, snap)
    run_started = T0 + timedelta(hours=1)
    assert mark_unseen_inactive(session, run(run_started)) == 1
    assert session.scalar(select(Listing.status)) == "inactive"
    listing, _ = save_snapshot(session, snap.model_copy(update={"seen_at": run_started}))
    assert listing.status == "active"


@pytest.mark.parametrize(
    "partial",
    [run(T0 + timedelta(hours=1), complete=False), run(T0 + timedelta(hours=1), status="blocked")],
)
def test_partial_or_failed_run_does_not_deactivate(session, snap, partial):
    save_snapshot(session, snap)
    assert mark_unseen_inactive(session, partial) == 0
    assert session.scalar(select(Listing.status)) == "active"


def test_times_survive_sqlite_roundtrip_as_utc(factory, snap):
    with factory() as session:
        save_snapshot(
            session,
            snap.model_copy(update={"seen_at": datetime(2026, 10, 1, 14, 30, tzinfo=MINSK)}),
        )
        session.commit()
    with factory() as session:
        listing = session.scalar(select(Listing))
        assert listing.last_seen == datetime(2026, 10, 1, 11, 30, tzinfo=UTC)
        assert listing.last_seen.tzinfo is not None
        # 14:30 по Минску = 11:30 UTC, это раньше 12:00 UTC: объявление должно погаснуть.
        assert mark_unseen_inactive(session, run(T0)) == 1


def test_last_seen_never_moves_back(session, snap):
    save_snapshot(session, snap.model_copy(update={"seen_at": T0 + timedelta(hours=2)}))
    listing, _ = save_snapshot(session, snap)
    assert listing.last_seen == T0 + timedelta(hours=2)


def test_same_price_with_different_decimal_form_is_not_a_change(session, snap):
    save_snapshot(session, snap.model_copy(update={"price_amount": Decimal(92500)}))
    later = snap.model_copy(
        update={"seen_at": T0 + timedelta(hours=1), "price_amount": Decimal("92500.00")}
    )
    assert save_snapshot(session, later)[1] is None


def test_migrations_match_models(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")
    command.check(config)  # падает, если модели разошлись с миграциями
    tables = set(inspect(make_engine(url)).get_table_names())
    assert {"objects", "listings", "snapshots", "events", "photo_hashes", "source_runs"} <= tables
    command.downgrade(config, "base")
