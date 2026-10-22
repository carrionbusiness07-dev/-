"""Кабинет «Хроника»: локальный веб-интерфейс владельца.

python -m minsk_realty.web            # http://localhost:8080
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from minsk_realty.analytics import (
    build_check_report,
    compute_baseline,
    compute_overview,
    fmt,
    source_health_summary,
)
from minsk_realty.db.session import database_url, make_engine, make_session_factory
from minsk_realty.web import views

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(fmt=fmt, sources=views.SOURCE_NAMES)
NB_THOUSANDS = re.compile(r"(?<=\d) (?=\d{3}\b)")
RANGE = re.compile(r"\$[\d\u00a0]+–[\d\u00a0]+")


def nb(text) -> Markup:
    """Неразрывные пробелы в числах и неразрывный диапазон «$78 500–80 000»:
    сумма не должна переноситься посередине."""
    safe = NB_THOUSANDS.sub("\u00a0", str(escape(text)))
    return Markup(RANGE.sub(lambda m: f'<span class="nowrap">{m.group(0)}</span>', safe))


templates.env.filters["nb"] = nb
templates.env.filters["money"] = lambda v, c="USD": nb(fmt.money(v, c))
templates.env.filters["pct"] = lambda v: views.pct(v) if v is not None else "—"
templates.env.filters["ppm"] = fmt.ppm
templates.env.filters["dshort"] = fmt.date_short
templates.env.filters["dwords"] = fmt.date_words
templates.env.filters["thousands"] = lambda v: (
    fmt.group_thousands(round(v)) if v is not None else "—"
)

app = FastAPI(title="Хроника", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@cache
def _factory():
    return make_session_factory(make_engine(database_url()))


def get_session() -> Iterator[Session]:
    with _factory()() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _snapshot(session: Session) -> views.Snapshot:
    return views.market_snapshot(session, database_url())


def _page(request: Request, name: str, snap: views.Snapshot, session: Session, **context):
    health = source_health_summary(session, snap.now)
    return templates.TemplateResponse(
        request,
        name,
        {"snap": snap, "health": health, "nav": name.split(".")[0], **context},
    )


@app.get("/", response_class=HTMLResponse)
def summary(
    request: Request,
    session: SessionDep,
    kind: str = Query("all"),
    days: int = Query(7, ge=1, le=60),
):
    snap = _snapshot(session)
    if kind not in dict(views.FILTERS):
        kind = "all"
    feed, counts = views.build_feed(snap, days=days, kind=kind)
    overview = compute_overview(snap.data, snap.now, top=5)
    return _page(
        request,
        "summary.html",
        snap,
        session,
        feed=feed,
        counts=counts,
        filters=views.FILTERS,
        kind=kind,
        days=days,
        overview=overview,
        series=views.city_series(snap),
    )


@app.get("/check")
def check(session: SessionDep, url: str = Query("")):
    snap = _snapshot(session)
    report = build_check_report(session, url.strip(), snap.now)
    if report.status == "ok" and report.object_id:
        return RedirectResponse(f"/objects/{report.object_id}", status_code=303)
    if report.status == "ok" and report.listing:
        return RedirectResponse(f"/listings/{report.listing.listing_id}", status_code=303)
    reason = "unsupported" if report.status == "unsupported_url" else "not_found"
    return RedirectResponse(f"/objects?missing={reason}", status_code=303)


@app.get("/objects", response_class=HTMLResponse)
def objects(
    request: Request,
    session: SessionDep,
    sort: str = Query("days"),
    district: str = Query(""),
    missing: str = Query(""),
):
    snap = _snapshot(session)
    rows = views.object_rows(snap, district=district or None)
    rows = views.sort_rows(rows, sort)
    districts = sorted({r.district_raw for r in views.object_rows(snap) if r.district_raw})
    return _page(
        request,
        "objects.html",
        snap,
        session,
        rows=rows,
        sort=sort,
        district=district,
        districts=districts,
        missing=missing,
    )


@app.get("/objects/{object_id}", response_class=HTMLResponse)
def object_page(request: Request, session: SessionDep, object_id: int):
    snap = _snapshot(session)
    listings = snap.data.object_listings(object_id)
    if not listings:
        return RedirectResponse("/objects?missing=not_found", status_code=303)
    current = max(listings, key=lambda r: (r.active, r.last_seen, r.id))
    return _report_page(request, session, snap, f"listing:{current.id}")


@app.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(request: Request, session: SessionDep, listing_id: int):
    snap = _snapshot(session)
    return _report_page(request, session, snap, f"listing:{listing_id}")


def _report_page(request: Request, session: Session, snap: views.Snapshot, ref: str):
    baseline = compute_baseline(snap.data, snap.now)
    report = build_check_report(session, ref, snap.now, baseline=baseline)
    if report.status != "ok":
        return RedirectResponse("/objects?missing=not_found", status_code=303)
    return _page(
        request,
        "object.html",
        snap,
        session,
        report=report,
        timeline=views.timeline(snap, report),
        history=views.history_rows(report),
        nav_hint="objects",
    )


@app.get("/market", response_class=HTMLResponse)
def market(request: Request, session: SessionDep):
    snap = _snapshot(session)
    overview = compute_overview(snap.data, snap.now, top=10)
    return _page(
        request, "market.html", snap, session, overview=overview, series=views.city_series(snap, 12)
    )


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request, session: SessionDep):
    snap = _snapshot(session)
    runs = views.recent_runs(session, snap.now)
    return _page(request, "sources.html", snap, session, runs=runs)
