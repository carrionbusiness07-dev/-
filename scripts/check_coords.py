"""Насколько точны координаты площадок: какая доля объявлений стоит в «общей» точке.

    docker compose exec -T web python - < scripts/check_coords.py

Если у площадки координаты с точностью до 5 знаков совпадают у объявлений на разных улицах
или в разных домах, это координата района или улицы, а не дома. Такие площадки нельзя
использовать для расстояния до метро и «соседей рядом» (см. AnalyticsConfig.approx_coord_sources).
Только чтение из базы.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def approx_share(
    rows: Iterable[tuple[str, float, float, str | None, str | None]],
) -> dict[str, float]:
    """rows: (площадка, lat, lon, улица, дом). Доля объявлений площадки в общих точках."""
    # Точка → какие адреса в ней стоят (в пределах площадки).
    addresses: dict[tuple[str, float, float], set[tuple[str, str]]] = defaultdict(set)
    placed: list[tuple[str, tuple[str, float, float]]] = []
    for source, lat, lon, street, house in rows:
        point = (source, round(lat, 5), round(lon, 5))
        addresses[point].add(((street or "").strip().lower(), (house or "").strip().lower()))
        placed.append((source, point))
    total: dict[str, int] = defaultdict(int)
    shared: dict[str, int] = defaultdict(int)
    for source, point in placed:
        total[source] += 1
        shared[source] += len(addresses[point]) > 1
    return {source: shared[source] / total[source] for source in total}


def main() -> None:
    from sqlalchemy import func, select

    from minsk_realty.db.models import Listing, Snapshot
    from minsk_realty.db.session import make_engine, make_session_factory

    last = (
        select(Snapshot.listing_id, func.max(Snapshot.seen_at).label("seen_at"))
        .group_by(Snapshot.listing_id)
        .subquery()
    )
    query = (
        select(Listing.source, Snapshot.lat, Snapshot.lon, Snapshot.street, Snapshot.house)
        .join(Snapshot, Snapshot.listing_id == Listing.id)
        .join(
            last, (last.c.listing_id == Snapshot.listing_id) & (last.c.seen_at == Snapshot.seen_at)
        )
        .where(Listing.status == "active", Snapshot.lat.is_not(None), Snapshot.lon.is_not(None))
    )
    with make_session_factory(make_engine())() as session:
        rows = [tuple(r) for r in session.execute(query)]
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row[0]] += 1
    shares = approx_share(rows)
    print(f"{'площадка':<12}{'объявлений':>12}{'доля общих точек':>20}")
    for source in sorted(shares):
        print(f"{source:<12}{counts[source]:>12}{shares[source]:>20.2f}")


if __name__ == "__main__":
    main()
