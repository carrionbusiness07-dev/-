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

from minsk_realty.matching.address import normalize_house, normalize_street


def address_key(street: str | None, house: str | None) -> tuple[tuple[str, ...], str] | None:
    """Адрес для сравнения (нормализатор склейки); None, если улицы или дома нет."""
    parsed, num = normalize_street(street), normalize_house(house)
    return (parsed.name, num) if parsed and num else None


def approx_share(
    rows: Iterable[tuple[str, float, float, str | None, str | None]],
) -> dict[str, float]:
    """rows: (площадка, lat, lon, улица, дом). Доля объявлений площадки в общих точках.

    Строки без улицы или дома не участвуют: отсутствие данных — не «другой дом».
    """
    # Точка → какие адреса в ней стоят (в пределах площадки).
    addresses: dict[tuple[str, float, float], set] = defaultdict(set)
    placed: list[tuple[str, tuple[str, float, float]]] = []
    for source, lat, lon, street, house in rows:
        key = address_key(street, house)
        if key is None:
            continue
        point = (source, round(lat, 5), round(lon, 5))
        addresses[point].add(key)
        placed.append((source, point))
    total: dict[str, int] = defaultdict(int)
    shared: dict[str, int] = defaultdict(int)
    for source, point in placed:
        total[source] += 1
        shared[source] += len(addresses[point]) > 1
    return {source: shared[source] / total[source] for source in total}


def main() -> None:
    from sqlalchemy import func, select

    from minsk_realty.db.models import Listing, RealtyObject, Snapshot
    from minsk_realty.db.session import make_engine, make_session_factory

    last = (
        select(Snapshot.listing_id, func.max(Snapshot.seen_at).label("seen_at"))
        .group_by(Snapshot.listing_id)
        .subquery()
    )
    query = (
        select(
            Listing.source,
            Snapshot.lat,
            Snapshot.lon,
            RealtyObject.street_norm,
            RealtyObject.house,
            Snapshot.street,
            Snapshot.house,
        )
        .join(Snapshot, Snapshot.listing_id == Listing.id)
        .join(
            last, (last.c.listing_id == Snapshot.listing_id) & (last.c.seen_at == Snapshot.seen_at)
        )
        .outerjoin(RealtyObject, RealtyObject.id == Listing.object_id)
        .where(Listing.status == "active", Snapshot.lat.is_not(None), Snapshot.lon.is_not(None))
    )
    with make_session_factory(make_engine())() as session:
        raw = session.execute(query).all()
    # Адрес берём у склеенного объекта (склейка уже разобрала адрес Kufar и Onliner),
    # иначе из снимка. Улицу и дом из разных источников не смешиваем.
    rows = []
    for source, lat, lon, obj_street, obj_house, snap_street, snap_house in raw:
        street, house = (
            (obj_street, obj_house)
            if address_key(obj_street, obj_house)
            else (snap_street, snap_house)
        )
        rows.append((source, lat, lon, street, house))
    counts: dict[str, int] = defaultdict(int)
    comparable: dict[str, int] = defaultdict(int)
    for source, _, _, street, house in rows:
        counts[source] += 1
        comparable[source] += address_key(street, house) is not None
    shares = approx_share(rows)
    print(f"{'площадка':<12}{'объявлений':>12}{'с адресом':>12}{'доля общих точек':>20}")
    for source in sorted(counts):
        share = f"{shares[source]:.3f}" if comparable[source] else "—"
        print(f"{source:<12}{counts[source]:>12}{comparable[source]:>12}{share:>20}")


if __name__ == "__main__":
    main()
