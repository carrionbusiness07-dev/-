"""Командная строка склейки.

python -m minsk_realty.matching explain 42
python -m minsk_realty.matching explain listing:17
python -m minsk_realty.matching explain https://re.kufar.by/vi/1000000001
python -m minsk_realty.matching process [--full]   # склейка и события без сборщика
python -m minsk_realty.matching relink-unaddressed  # пересклеить Kufar, собранный без адреса

Если склейку в этот момент выполняет сборщик, `process` пропускает проход (код выхода 3),
а не ждёт и не падает: блокировка — matching/lock.py.
"""

from __future__ import annotations

import argparse
import logging

from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.matching.explain import explain
from minsk_realty.matching.pipeline import process_after_run
from minsk_realty.matching.relink import relink_unaddressed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minsk_realty.matching")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_explain = sub.add_parser("explain", help="почему склеилось или не склеилось")
    p_explain.add_argument("ref", help="id объекта, listing:<id> или ссылка на объявление")
    p_process = sub.add_parser("process", help="пересчитать склейку и события")
    p_process.add_argument("--full", action="store_true", help="все объявления, а не недавние")
    p_relink = sub.add_parser(
        "relink-unaddressed", help="пересклеить объявления, собранные без адреса"
    )
    p_relink.add_argument("--source", default="kufar")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    factory = make_session_factory(make_engine())
    with factory() as session:
        if args.cmd == "explain":
            ref = int(args.ref) if args.ref.isdigit() else args.ref
            print(explain(session, ref))
        else:
            if args.cmd == "relink-unaddressed":
                count, stats = relink_unaddressed(session, args.source)
                print(f"пересклеено объявлений: {count}")
            else:
                stats = process_after_run(session, full=args.full)
            print(stats)
            session.commit()
            if stats.skipped:
                return 3  # склейку уже выполняет сборщик: повторите позже
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
