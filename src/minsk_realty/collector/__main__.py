"""Запуск сборщика.

    python -m minsk_realty.collector            # бесконечный цикл, проход раз в 30–60 минут
    python -m minsk_realty.collector --once     # один проход (например, из cron)
    python -m minsk_realty.collector --sources realt
    python -m minsk_realty.collector --unblock kufar   # снять остановку после 403/429

Перед первым запуском: `alembic upgrade head`. Нужны переменные окружения DATABASE_URL
и SELLER_KEY_SALT (см. .env.example). Kufar работает только с сервера в Беларуси.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time
from datetime import UTC, datetime

from minsk_realty.collector.run import close_stale_runs, collect, is_blocked, last_pass_started
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.health.check import AlertThrottle, check_sources, notify, ping_watchdog
from minsk_realty.matching import process_after_run
from minsk_realty.sources import ADAPTERS

log = logging.getLogger("minsk_realty.collector")

INTERVAL_MINUTES = (30, 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minsk_realty.collector")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--sources", default=",".join(ADAPTERS), help="через запятую")
    parser.add_argument(
        "--unblock",
        action="append",
        default=[],
        help="запустить площадку, остановленную после 403/429 (человек проверил причину)",
    )
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

    # docker stop шлёт SIGTERM: превращаем его в SystemExit, чтобы проход закрыл запись в БД.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    factory = make_session_factory(make_engine())
    throttle = AlertThrottle()
    unblocked = set(args.unblock)
    if not args.once:
        # Постоянный сборщик один: «идущие» проходы от прошлого запуска — прерванные.
        # Разовый --once может работать рядом с ним и чужие проходы не трогает.
        with factory() as session:
            close_stale_runs(session, older_than=None)
            session.commit()
    while True:
        run_cycle(factory, sources, unblocked, throttle)
        unblocked.clear()  # --unblock действует на один проход: дальше снова решает журнал
        if args.once:
            return 0
        time.sleep(random.uniform(*INTERVAL_MINUTES) * 60)


def run_cycle(factory, sources, unblocked, throttle) -> None:
    """Один цикл: все площадки по очереди и проверка здоровья.

    Любая ошибка логируется и не прерывает цикл: иначе контейнер с restart: unless-stopped
    перезапускался бы сразу и обходил площадки без паузы 30–60 минут.
    """
    try:
        with factory() as session:
            close_stale_runs(session)
            session.commit()
    except Exception:
        log.exception("сбой при закрытии прерванных проходов")
    attempted = succeeded = 0
    for source in sources:
        try:
            with factory() as session:
                if is_blocked(session, source) and source not in unblocked:
                    log.warning("%s: остановлена после 403/429, пропускаю", source)
                    continue
                # Площадкам, которые сами отдают историю цены, частый опрос не нужен.
                interval = getattr(ADAPTERS[source], "min_interval", None)
                last = last_pass_started(session, source) if interval else None
                now = datetime.now(UTC)
                # Время прохода из будущего (часы компьютера убегали) не должно блокировать.
                if last is not None and last <= now and now - last < interval:
                    log.info(
                        "%s: прошлый проход в %s, следующий не раньше чем через %s",
                        source,
                        last.isoformat(timespec="minutes"),
                        interval,
                    )
                    continue
            attempted += 1
            adapter = ADAPTERS[source]()
            try:
                run = collect(adapter, factory)
                succeeded += run.status == "ok"
            finally:
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001 — закрытие клиента не отменяет записанный проход
                    log.warning("%s: не удалось закрыть HTTP-клиент", source)
        except Exception:
            log.exception("%s: сбой цикла сборщика", source)
        try:  # склейка и события (Emma): сбой не должен останавливать сбор
            with factory() as session:
                process_after_run(session, source)
                session.commit()
        except Exception:
            log.exception("%s: сбой склейки после прохода", source)
    try:
        with factory() as session:
            for alert in check_sources(session, sources):
                if throttle.should_send(alert):
                    notify(alert.text)
    except Exception:
        log.exception("сбой проверки здоровья источников")
    # Внешний сторож: «жив» после каждого цикла; «сбой», если не удалась ни одна площадка.
    ping_watchdog(failed=attempted > 0 and not succeeded)


if __name__ == "__main__":
    raise SystemExit(main())
