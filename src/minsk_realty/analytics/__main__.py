"""Командная строка аналитики (БД — из DATABASE_URL).

python -m minsk_realty.analytics hypothesis [--now 2026-10-22T12:00:00+03:00] [--json]
python -m minsk_realty.analytics weekly [--now ...] [--json]
python -m minsk_realty.analytics check https://re.kufar.by/vi/1000000001 [--json]
python -m minsk_realty.analytics overview [--json]
python -m minsk_realty.analytics health [--json]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from minsk_realty.analytics import (
    build_check_report,
    hypothesis_report,
    market_overview,
    render_check_text,
    render_hypothesis_text,
    render_weekly_text,
    source_health_summary,
    to_jsonable,
    weekly_summary,
)
from minsk_realty.db.session import make_engine, make_session_factory


def _now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value)
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minsk_realty.analytics")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_ in (
        ("hypothesis", "метрики гипотезы и рекомендация (неделя 6)"),
        ("weekly", "недельная сводка для канала"),
        ("overview", "обзор рынка для кабинета"),
        ("health", "здоровье источников"),
        ("check", "отчёт /check по ссылке или listing:<id>"),
    ):
        p = sub.add_parser(name, help=help_)
        if name == "check":
            p.add_argument("ref")
        p.add_argument("--now", help="момент расчёта, ISO 8601 (по умолчанию — сейчас)")
        p.add_argument("--json", action="store_true", help="структура вместо текста")
    args = parser.parse_args(argv)
    now = _now(args.now)

    factory = make_session_factory(make_engine())
    with factory() as session:
        if args.cmd == "hypothesis":
            result = hypothesis_report(session, now)
            text = render_hypothesis_text
        elif args.cmd == "weekly":
            result = weekly_summary(session, now)
            text = render_weekly_text
        elif args.cmd == "check":
            ref = int(args.ref) if args.ref.isdigit() else args.ref
            result = build_check_report(session, ref, now)
            text = render_check_text
        elif args.cmd == "overview":
            result, text = market_overview(session, now), None
        else:
            result, text = source_health_summary(session, now), None
    if args.json or text is None:
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    else:
        print(text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
