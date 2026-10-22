"""Миграция 0003 на непустой БД: ключ улицы у объектов и флаг trusted у старых событий."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from minsk_realty.db.session import make_engine

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def test_0003_backfills_existing_rows(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "0002")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO objects (id, street_norm, first_seen_at) VALUES "
                "(1, 'ул якуба коласа', '2026-10-01 09:00:00'), (2, NULL, '2026-10-01 09:00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO events (object_id, type, at, payload, dedup_key, notify, created_at) "
                "VALUES (1, 'new', '2026-10-01 09:00:00', '{}', 'new', 1, '2026-10-01 09:00:00'),"
                " (1, 'price_drop', '2026-10-02 09:00:00', '{}', 'price:1', 0,"
                " '2026-10-02 09:00:00')"
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as conn:
        tails = dict(conn.execute(sa.text("SELECT id, street_tail FROM objects")).all())
        flags = conn.execute(
            sa.text("SELECT dedup_key, trusted, retracted_at FROM events ORDER BY id")
        ).all()
    assert tails == {1: "коласа", 2: None}
    assert [tuple(row) for row in flags] == [("new", 1, None), ("price:1", 0, None)]
    command.check(config)
    command.downgrade(config, "0002")
    command.upgrade(config, "head")
    engine.dispose()
