"""Разовая разведка для поиска в боте: тексты и справочники Kufar/Realt. Сеть, одна страница на подкоманду.

    python scripts/search_recon.py kufar-text | kufar-labels | realt-labels

Свободный текст объявлений не печатается и не сохраняется: только длины и признак обрыва.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from statistics import median

from minsk_realty.sources import kufar, realt
from minsk_realty.sources.base import PoliteClient

LABEL_KEYS = ("bathroom", "balcony", "flat_windows_side", "possible_exchange")
REALT_KEYS = ("balconyType", "wallMaterial", "heating")


def kufar_page() -> list[dict]:
    params = {**kufar.SEARCH_PARAMS, "size": "20"}
    return PoliteClient().get(kufar.SEARCH_URL, params=params).json().get("ads") or []


def kufar_text() -> None:
    rows = []
    for ad in kufar_page():
        body, short = ad.get("body") or "", ad.get("body_short") or ""
        cut = lambda s: s.rstrip().endswith(("…", "..."))  # noqa: E731
        rows.append((len(body), len(short), cut(body), cut(short)))
        print(f"body={len(body)} body_short={len(short)} body_cut={cut(body)} short_cut={cut(short)}")
    if rows:
        print(
            f"n={len(rows)} body median={median(r[0] for r in rows)} max={max(r[0] for r in rows)}"
            f" short median={median(r[1] for r in rows)} max={max(r[1] for r in rows)}"
            f" body_cut={sum(r[2] for r in rows)} short_cut={sum(r[3] for r in rows)}"
        )


def kufar_labels(ads: list[dict] | None = None) -> None:
    seen: dict[str, dict] = defaultdict(dict)
    for ad in ads if ads is not None else kufar_page():
        for p in ad.get("ad_parameters") or []:
            if p.get("p") in LABEL_KEYS:
                v, vl = p.get("v"), p.get("vl")
                # Списковые параметры (окна): v и vl идут в одном порядке, раскладываем по парам.
                pairs = zip(v, vl) if isinstance(v, list) and isinstance(vl, list) else [(v, vl)]
                seen[p["p"]].update(pairs)
    for key in LABEL_KEYS:
        print(key, "->", dict(seen.get(key, {})) or "не встретилось на странице")


def _find(node, key, path=""):
    """Ищет списки вариантов {label, value} под ключом key в произвольной глубине."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, list):
                yield path + "/" + k, v
            else:
                yield from _find(v, key, path + "/" + k)
    elif isinstance(node, list):
        for i, v in enumerate(node[:50]):
            yield from _find(v, key, f"{path}[{i}]")


def realt_labels() -> None:
    html = PoliteClient().get(realt.LIST_URL, params={"page": 1}).text
    props = realt.extract_page_props(html)
    for key in REALT_KEYS:
        found = [(p, v) for p, v in _find(props, key) if v and isinstance(v[0], dict) and "label" in v[0]]
        if not found:
            codes = sorted({repr(o.get(key)) for o in props.get("objects") or []})
            print(key, "-> справочника в данных нет; коды в объявлениях:", ", ".join(codes))
            continue
        for path, options in found[:1]:
            print(key, path, "->", {o.get("value"): o.get("label") for o in options})


if __name__ == "__main__":
    cmds = {"kufar-text": kufar_text, "kufar-labels": kufar_labels, "realt-labels": realt_labels}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"usage: search_recon.py {'|'.join(cmds)}")
    cmds[sys.argv[1]]()
