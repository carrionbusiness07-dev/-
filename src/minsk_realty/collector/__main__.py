"""Запуск сборщика.

    python -m minsk_realty.collector            # бесконечный цикл, проход раз в 30–60 минут
    python -m minsk_realty.collector --once     # один проход (например, из cron)
    python -m minsk_realty.collector --sources realt

Перед первым запуском: `alembic upgrade head`. Нужны переменные окружения DATABASE_URL
и SELLER_KEY_SALT (см. .env.example). Kufar работает только с сервера в Беларуси.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time

from minsk_realty.collector.run import collect
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.health.check import check_sources, notify
from minsk_realty.sources.kufar import KufarAdapter
from minsk_realty.sources.realt import RealtAdapter

ADAPTERS = {"kufar": KufarAdapter, "realt": RealtAdapter}
INTERVAL_MINUTES = (30, 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minsk_realty.collector")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--sources", default=",".join(ADAPTERS), help="через запятую")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # INFO пишет URL с токеном бота
    if not os.environ.get("SELLER_KEY_SALT"):
        sys.exit("SELLER_KEY_SALT не задан — см. .env.example")
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = set(sources) - set(ADAPTERS)
    if unknown:
        sys.exit(f"неизвестные площадки: {', '.join(sorted(unknown))}")

    factory = make_session_factory(make_engine())
    while True:
        for source in sources:
            adapter = ADAPTERS[source]()
            try:
                collect(adapter, factory)
            finally:
                adapter.close()
        with factory() as session:
            for alert in check_sources(session, sources):
                notify(alert)
        if args.once:
            return 0
        time.sleep(random.uniform(*INTERVAL_MINUTES) * 60)


if __name__ == "__main__":
    raise SystemExit(main())
