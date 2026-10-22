"""Правила склейки: объявление против объекта (его надёжно привязанных объявлений).

Порядок решения:
0. У объявления никогда не было комнат или площади → данных нет (`insufficient`): новую
   склейку не делаем, существующую не трогаем. Признаки — последние известные непустые
   значения, поэтому разовый пустой снимок сюда не приводит.
1. Жёсткие противоречия с любым объявлением объекта → не склеиваем.
   Комнаты, площадь > ±1 м², этаж, этажность, город, улица, дом, координаты > 500 м,
   фото противоречат (у обоих ≥ 3 фото, совпало меньше нужного), тот же продавец держит
   оба объявления на одной площадке дольше льготного срока.
2. Тот же продавец на той же площадке, старое объявление ещё активно → ждём (defer).
3. Улики: фото, адрес (улица + дом), полный отпечаток (адрес + этаж + этажность),
   координаты в радиусе. Все улики требуют совпадения комнат и площади ±1 м².
   Фото — это не меньше max(2, ⌈50% × меньшего набора⌉) совпавших пар без «типовых» хешей
   (фасад, двор, заглушка — один хеш у заведомо разных квартир). Соседи по дому делят
   1–2 снимка из 8, одна квартира на двух площадках — почти все.
4. Уверенность:
   high   — фото + совпадение места; без этажа у обеих сторон (Kufar) нужно ≥ 3 фото;
   medium — полный отпечаток уровня 1; или фото без данных о месте (или с местом, но
            без этажа и всего 2 фото); или адрес + тот же продавец перевыложил после снятия;
   low    — только адрес без этажа (Kufar) или только координаты;
   иначе  — не склеиваем.
5. Без подтверждения фото объявление, которое одновременно висело на той же площадке с
   объявлением объекта у другого продавца, не склеиваем — ни low, ни medium. В панельном
   доме одинаковые квартиры есть в каждом подъезде на каждом этаже: отпечаток уровня 1 у
   них совпадает, и без фото «одна квартира у двух агентств» и «соседи» неразличимы.
   Ложная склейка дала бы чужие «снижения цены» и ложный cross_listed; пропущенная
   склейка лишь откладывает факт «два агентства» до появления фото.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from minsk_realty.matching.address import streets_match
from minsk_realty.matching.config import MatchingConfig
from minsk_realty.matching.features import ListingFeatures, distance_m
from minsk_realty.matching.photos import count_photo_matches

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
TRUSTED = ("high", "medium")

# Сила улики «место».
LOC_NONE, LOC_GEO, LOC_ADDRESS, LOC_FINGERPRINT = 0, 1, 2, 3
LOC_NAMES = {
    LOC_NONE: "нет",
    LOC_GEO: "координаты",
    LOC_ADDRESS: "адрес без этажа",
    LOC_FINGERPRINT: "отпечаток уровня 1",
}
# Коды места для listings.match_basis (стабильные, не для показа).
LOC_CODES = {
    LOC_NONE: "none",
    LOC_GEO: "geo",
    LOC_ADDRESS: "address",
    LOC_FINGERPRINT: "fingerprint",
}

Outcome = Literal["match", "reject", "defer"]


@dataclass
class Verdict:
    object_id: int
    outcome: Outcome
    confidence: str | None = None
    reasons: list[str] = field(default_factory=list)
    photo_matches: int = 0
    location: int = LOC_NONE
    # Сколько совпавших фото было нужно (0 — фото нет у одной из сторон).
    photos_needed: int = 0
    # reject из-за жёсткого противоречия (а не из-за нехватки улик).
    conflict: bool = False
    # Сравнивать не по чему: у объявления нет комнат или площади. Существующую склейку
    # из-за этого не трогаем.
    insufficient: bool = False
    relist: bool = False

    @property
    def rank(self) -> int:
        return CONFIDENCE_RANK.get(self.confidence or "", 0) if self.outcome == "match" else 0

    @property
    def photo_ok(self) -> bool:
        return self.photos_needed > 0 and self.photo_matches >= self.photos_needed

    def summary(self) -> str:
        head = {
            "match": f"склеено ({self.confidence})",
            "reject": "не склеено",
            "defer": "отложено",
        }[self.outcome]
        return f"{head}: " + "; ".join(self.reasons)

    def basis(self) -> dict[str, Any]:
        """Основание склейки для listings.match_basis: место, фото, перевыкладывание."""
        return {
            "rule": "compare",
            "location": LOC_CODES[self.location],
            "photo_matches": self.photo_matches,
            "photos_needed": self.photos_needed,
            "relist": self.relist,
        }


def same_seller(a: ListingFeatures, b: ListingFeatures) -> bool:
    """Тот же продавец: тот же псевдоним на той же площадке или то же агентство (юрлицо)."""
    if a.source == b.source and a.seller_key and a.seller_key == b.seller_key:
        return True
    return bool(a.agency and a.agency == b.agency)


def coexisted(a: ListingFeatures, b: ListingFeatures) -> bool:
    """Объявления хотя бы раз были видны одновременно."""
    return max(a.first_seen, b.first_seen) <= min(a.last_seen, b.last_seen)


def hard_conflicts(l: ListingFeatures, p: ListingFeatures, cfg: MatchingConfig) -> list[str]:
    """Признаки, при которых два объявления заведомо про разные квартиры."""
    out = []
    if l.town and p.town and l.town != p.town:
        out.append(f"город {l.town} ≠ {p.town}")
    if l.rooms and p.rooms and l.rooms != p.rooms:
        out.append(f"комнат {l.rooms} ≠ {p.rooms} у {p.label}")
    if l.area and p.area and abs(l.area - p.area) > cfg.area_tolerance_m2 + 1e-9:
        out.append(
            f"площадь {l.area:g} vs {p.area:g} у {p.label} (допуск ±{cfg.area_tolerance_m2:g})"
        )
    if l.floor is not None and p.floor is not None and l.floor != p.floor:
        out.append(f"этаж {l.floor} ≠ {p.floor} у {p.label}")
    if l.floors_total and p.floors_total and l.floors_total != p.floors_total:
        out.append(f"этажность {l.floors_total} ≠ {p.floors_total} у {p.label}")
    street_eq = streets_match(l.street, p.street)
    if street_eq is False:
        out.append(f"улица «{l.street}» ≠ «{p.street}» у {p.label}")
    elif l.house and p.house and l.house != p.house:
        out.append(f"дом {l.house} ≠ {p.house} у {p.label}")
    dist = distance_m(l, p)
    if dist is not None and dist > cfg.geo_contradiction_m:
        out.append(f"между точками {dist:.0f} м у {p.label} (> {cfg.geo_contradiction_m:g} м)")
    return out


_hard_conflicts = hard_conflicts  # старое имя


def _seller_check(
    l: ListingFeatures, p: ListingFeatures, cfg: MatchingConfig, now: datetime
) -> tuple[Outcome, str] | None:
    """Тот же продавец на той же площадке: перевыкладывание или две разные квартиры."""
    if not (l.source == p.source and l.seller_key and l.seller_key == p.seller_key):
        return None
    newer, older = (
        (l, p) if (l.first_seen, l.listing_id) >= (p.first_seen, p.listing_id) else (p, l)
    )
    overlap = min(l.last_seen, p.last_seen) - max(l.first_seen, p.first_seen)
    if overlap > cfg.same_seller_overlap_grace:
        return (
            "reject",
            (
                f"тот же продавец держит {l.label} и {p.label} одновременно дольше "
                f"{cfg.same_seller_overlap_grace} — это разные квартиры"
            ),
        )
    if older.active and now - newer.first_seen <= cfg.same_seller_overlap_grace:
        return (
            "defer",
            (
                f"тот же продавец, старое объявление {older.label} ещё активно: "
                "ждём, снимет ли он его (перевыкладывание)"
            ),
        )
    if older.active:
        return (
            "reject",
            (
                f"тот же продавец не снял {older.label} за {cfg.same_seller_overlap_grace} — "
                "это разные квартиры"
            ),
        )
    return None


def _location(l: ListingFeatures, p: ListingFeatures, cfg: MatchingConfig) -> int:
    if not (l.rooms and p.rooms and l.area and p.area):
        return LOC_NONE  # без комнат и площади ни один уровень склейки не работает
    if l.rooms != p.rooms or abs(l.area - p.area) > cfg.area_tolerance_m2 + 1e-9:
        return LOC_NONE
    if streets_match(l.street, p.street) is True and l.house and l.house == p.house:
        if (
            l.floor is not None
            and l.floor == p.floor
            and l.floors_total
            and l.floors_total == p.floors_total
        ):
            return LOC_FINGERPRINT
        return LOC_ADDRESS
    dist = distance_m(l, p)
    if dist is not None and dist <= cfg.geo_radius_m:
        return LOC_GEO
    return LOC_NONE


def _relist_by_same_seller(l: ListingFeatures, p: ListingFeatures, cfg: MatchingConfig) -> bool:
    """`l` — новое объявление того же продавца после снятия `p` (в окне relisted)."""
    if not same_seller(l, p) or p.active or p.first_seen >= l.first_seen:
        return False
    gap = l.first_seen - p.last_seen
    return -cfg.same_seller_overlap_grace <= gap <= cfg.relisted_window


def photos_needed(n_listing: int, n_object: int, cfg: MatchingConfig) -> int:
    """Сколько совпавших фото нужно для улики «фото» (0 — сравнивать нечего)."""
    if not (n_listing and n_object):
        return 0
    share = math.ceil(cfg.phash_min_share * min(n_listing, n_object) - 1e-9)
    return max(cfg.phash_min_matches, share)


def compare(
    listing: ListingFeatures,
    object_id: int,
    others: list[ListingFeatures],
    cfg: MatchingConfig,
    now: datetime,
    *,
    weak: list[ListingFeatures] = (),
) -> Verdict:
    """Сравнить объявление с объектом, у которого надёжно привязаны объявления `others`.

    `weak` — объявления объекта со склейкой low: улик они не дают, но участвуют в правиле
    «без фото + одновременно на одной площадке».
    """
    v = Verdict(object_id=object_id, outcome="reject")
    others = [o for o in others if o.listing_id != listing.listing_id]
    weak = [o for o in weak if o.listing_id != listing.listing_id]
    if not others:
        v.reasons.append("у объекта нет надёжно привязанных объявлений для сравнения")
        v.insufficient = True
        return v
    if not (listing.rooms and listing.area):
        v.reasons.append("у объявления нет комнат или площади — сравнивать не по чему")
        v.insufficient = True
        return v

    conflicts = [c for o in others for c in hard_conflicts(listing, o, cfg)]
    seller = [r for o in others if (r := _seller_check(listing, o, cfg, now))]
    conflicts += [text for kind, text in seller if kind == "reject"]

    union: set[str] = set().union(*(o.phashes for o in others))
    richest = max(len(o.phashes) for o in others)
    v.photo_matches = count_photo_matches(listing.phashes, union, cfg.phash_max_distance)
    v.photos_needed = photos_needed(len(listing.phashes), richest, cfg)
    photo_ok = v.photo_ok
    if (
        not photo_ok
        and len(listing.phashes) >= cfg.photo_min_for_contradiction
        and len(union) >= cfg.photo_min_for_contradiction
    ):
        conflicts.append(
            f"фото противоречат: у объявления {len(listing.phashes)}, у объекта {len(union)}, "
            f"совпало {v.photo_matches} (нужно ≥ {v.photos_needed})"
        )
    if conflicts:
        v.reasons, v.conflict = conflicts, True
        return v
    defer = [text for kind, text in seller if kind == "defer"]
    if defer:
        v.outcome, v.reasons = "defer", defer
        return v

    v.location = max(_location(listing, o, cfg) for o in others)
    v.relist = relist = any(_relist_by_same_seller(listing, o, cfg) for o in others)
    v.reasons.append(f"место: {LOC_NAMES[v.location]}")
    if listing.phashes and union:
        v.reasons.append(
            f"фото: совпало {v.photo_matches} из нужных {v.photos_needed} "
            f"(Хэмминг ≤ {cfg.phash_max_distance})"
        )
    else:
        v.reasons.append("фото: нет хешей у одной из сторон")
    if listing.popular_phashes:
        v.reasons.append(f"типовых фото отброшено: {listing.popular_phashes}")
    if relist:
        v.reasons.append("тот же продавец перевыложил после снятия")

    floor_known = v.location == LOC_FINGERPRINT
    if (
        photo_ok
        and v.location >= LOC_GEO
        and (floor_known or v.photo_matches >= cfg.phash_high_min_matches_without_floor)
    ):
        confidence = "high"
    elif photo_ok or v.location == LOC_FINGERPRINT or (v.location == LOC_ADDRESS and relist):
        confidence = "medium"
    elif v.location in (LOC_ADDRESS, LOC_GEO):
        confidence = "low"
    else:
        v.reasons.insert(0, "нет совпадения ни по месту, ни по фото")
        return v

    if not photo_ok and any(
        o.source == listing.source and not same_seller(o, listing) and coexisted(listing, o)
        for o in [*others, *weak]
    ):
        v.reasons.insert(
            0,
            "без подтверждения фото, а объявления разных продавцов одновременно висят на "
            "одной площадке — скорее соседние квартиры (одинаковая планировка в другом подъезде)",
        )
        return v

    v.outcome, v.confidence = "match", confidence
    return v
