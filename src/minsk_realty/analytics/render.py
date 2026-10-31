"""Тексты для бота и канала. Только форматирование: все цифры — из структур отчёта.

`icons=False` убирает эмодзи (веб-кабинет, логи).
"""

from __future__ import annotations

from minsk_realty.analytics.data import FlatDetails
from minsk_realty.analytics.facts import facts_line
from minsk_realty.analytics.fmt import (
    CONFIDENCE_NAMES,
    SELLER_NAMES,
    SOURCE_NAMES,
    date_full,
    date_short,
    date_words,
    days_text,
    district_label,
    money,
    num,
    objects_text,
    pct,
    plural,
    ppm,
    rooms_label,
    times_text,
)
from minsk_realty.analytics.hypothesis import HypothesisReport
from minsk_realty.analytics.medians import MedianCell
from minsk_realty.analytics.report import CheckReport, OtherListing
from minsk_realty.analytics.weekly import WeeklySummary
from minsk_realty.sources import NOT_COLLECTED_NOTE, names_text, poll_text

_ICONS = {
    "flat": "🏠",
    "time": "⏳",
    "relist": "↻",
    "history": "📉",
    "cross": "🔁",
    "median": "📊",
    "info": "ℹ️",
    "maybe": "❓",
    "top": "🔻",
    "good": "✅",
    "warn": "⚠️",
    "meh": "🟡",
    "place": "📍",
    "house": "🏢",
}

_VERDICT_ICON = {
    "suspicious": "warn",
    "below": "good",
    "market": "good",
    "above": "meh",
    "unknown": "info",
}
FOOTER = "Цены объявлений, а не сделок. Это рекомендация, а не гарантия."
SUPPORTED_TEXT = names_text("или")


def _icon(name: str, icons: bool) -> str:
    return f"{_ICONS[name]} " if icons else ""


def _source(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def _price_with_usd(amount, currency, usd) -> str:
    text = money(amount, currency)
    if currency != "USD" and usd is not None:
        text += f" (≈ {money(usd, 'USD')})"
    return text


def render_check_text(report: CheckReport, *, icons: bool = True, max_history: int = 8) -> str:
    obs = (
        f"Наблюдаем рынок с {date_full(report.observation_start)}"
        if report.observation_start
        else "Наблюдение за рынком только начинается"
    )
    if report.status == "unsupported_url":
        return (
            "Не понимаю эту ссылку. Пришлите ссылку на объявление о продаже квартиры "
            f"с {SUPPORTED_TEXT}."
        )
    if report.status == "not_found":
        source = report.source or ""
        note = NOT_COLLECTED_NOTE.get(source)
        return (
            f"{_icon('info', icons)}Этого объявления пока нет в нашей базе. Сборщик обходит "
            f"{_source(source)} {poll_text(source)}, и свежие объявления появляются "
            "с задержкой. Попробуйте позже.\n" + (f"{note}\n" if note else "") + obs
        )

    housing = (
        [report.flat.housing[:1].upper() + report.flat.housing[1:]]
        if report.flat and report.flat.housing
        else []
    )
    top = [_header(report, icons), *housing, _now_line(report)]
    blocks = [
        _assessment_lines(report, icons),
        _details_lines(report, icons),
        _place_lines(report, icons),
        [*_comparison_lines(report, icons), *_house_lines(report, icons)],
    ]
    if report.data_level == "first_time":
        blocks.append(
            [
                f"{_icon('info', icons)}Эту квартиру мы видим впервые. Истории пока нет.",
                "Нажмите «Следить» — сообщу, если цена изменится или объявление снимут.",
            ]
        )
    else:
        blocks += [
            _exposure_lines(report, icons),
            _history_lines(report, icons, max_history),
            _others_lines(report, icons) if report.others else [],
            _possible_lines(report, icons) if report.possible_object else [],
        ]
    tail = [FOOTER, _match_line(report)]
    if report.data_level == "short":
        tail.append(
            f"{_icon('info', icons)}Квартира уже продавалась, когда мы начали наблюдать: "
            "её настоящая история длиннее нашей."
        )
    tail.append(obs)
    return "\n\n".join("\n".join(b) for b in (top, *blocks, tail) if b)


def _header(report: CheckReport, icons: bool) -> str:
    f = report.flat
    parts = []
    if f is not None:
        if f.rooms:
            parts.append(f"{f.rooms}-комн.")
        if f.area:
            parts.append(f"{f.area:g} м²")
        if f.floor is not None and f.floors_total:
            parts.append(f"{f.floor}/{f.floors_total} эт.")
        if f.street:
            parts.append(f.street)
        if f.district:
            parts.append(district_label(f.district))
    return _icon("flat", icons) + (", ".join(parts) or "Квартира")


def _seller(seller_type: str) -> str:
    return SELLER_NAMES.get(seller_type, seller_type)


def _now_line(report: CheckReport) -> str:
    listing = report.listing
    price = listing.price
    who = f"{_source(listing.source)}, {_seller(listing.seller_type)}"
    if price is None:
        return f"Цена не указана — {who}"
    extra = []
    if price.currency != "USD" and price.usd is not None:
        extra.append(f"≈ {money(price.usd, 'USD')}")
    if price.usd_per_m2 is not None:
        extra.append(ppm(price.usd_per_m2))
    text = money(price.amount, price.currency) + (f" ({', '.join(extra)})" if extra else "")
    if listing.status != "active":
        return f"Последняя цена: {text} — {who}, объявление снято"
    return f"Сейчас: {text} — {who}"


def _exposure_lines(report: CheckReport, icons: bool) -> list[str]:
    e = report.exposure
    if e is None:
        return [f"{_icon('time', icons)}Срок на рынке посчитать не из чего"]
    how = "опубликована" if e.start_basis == "posted_at_source" else "впервые замечена"
    if e.at_least:
        head = f"На рынке: как минимум {days_text(e.days)}"
        if e.start_basis == "posted_at_source":
            detail = f"{how} {date_words(e.start)} на {_source(e.start_source)}, по дате площадки"
        else:
            since = date_words(e.observed_since) if e.observed_since else date_words(e.start)
            detail = (
                f"уже продавалась, когда мы начали следить за {_source(e.start_source)} {since}"
            )
    else:
        head = f"На рынке: {days_text(e.days)}"
        detail = f"{how} {date_words(e.start)} на {_source(e.start_source)}"
    lines = [f"{_icon('time', icons)}{head} ({detail})"]
    relists = e.relist_count + e.returned_new_listing
    if relists:
        lines.append(f"   {_icon('relist', icons)}Перевыкладывалась {times_text(relists)}")
    if e.site_days is not None and e.state == "active" and e.days - e.site_days >= 7:
        lines.append(
            f"   На сайте объявлению {days_text(e.site_days)}, на самом деле квартира продаётся "
            f"{'как минимум ' if e.at_least else ''}{days_text(e.days)}"
        )
    if e.state == "removed":
        lines.append(f"   Снята со всех площадок {date_short(e.end)}")
    elif e.state == "hidden":
        lines.append(
            f"   Сейчас объявлений нет (последний раз видели {date_short(e.end)}); "
            "если не вернётся за 3 дня, считаем снятой"
        )
    s = report.sellers
    if s is not None and s.count > 1:
        kinds = []
        if s.agencies:
            kinds.append(f"{s.agencies} {plural(s.agencies, 'агентство', 'агентства', 'агентств')}")
        if s.owners:
            kinds.append(
                f"{s.owners} {plural(s.owners, 'собственник', 'собственника', 'собственников')}"
            )
        lines.append(
            f"   Продавцов за историю: {s.count}" + (f" ({', '.join(kinds)})" if kinds else "")
        )
    return lines


def _history_lines(report: CheckReport, icons: bool, limit: int) -> list[str]:
    points = list(report.history)
    if not points:
        return [f"{_icon('history', icons)}Цена в объявлении не указана"]
    if len(points) == 1:
        p = points[0]
        price = _price_with_usd(p.amount, p.currency, p.usd)
        return [
            f"{_icon('history', icons)}Цена не менялась: {price} с {date_short(p.at)} "
            + f"({_source(p.source)})"
        ]
    lines = [f"{_icon('history', icons)}История цены:"]
    hidden = 0
    if len(points) > limit:
        hidden = len(points) - limit
        points = points[:1] + points[-(limit - 1) :]
    multi = len({p.listing_id for p in report.history}) > 1
    for i, p in enumerate(points):
        note = ""
        if p.kind == "new_listing":
            note = " (новое объявление, тот же объект)"
        elif p.kind == "parallel":
            note = " (ещё одно объявление этой квартиры)"
        elif p.kind == "currency":
            note = " (сменили валюту)"
        elif p.kind == "change" and p.change_pct is not None:
            note = f" ({pct(p.change_pct)})"
        if p.kind == "new_listing" and p.change_pct:
            note = f" (новое объявление, тот же объект, {pct(p.change_pct)})"
        source = _source(p.source)
        if (
            multi
            and p.seller_type in ("owner", "agency")
            and p.kind in ("first", "new_listing", "parallel")
        ):
            source += f", {_seller(p.seller_type)}"
        lines.append(
            f"   {date_short(p.at)}  {_price_with_usd(p.amount, p.currency, p.usd)}  {source}{note}"
        )
        if hidden and i == 0:
            lines.append(
                f"   … ещё {hidden} {plural(hidden, 'изменение', 'изменения', 'изменений')}"
            )
    moves = report.moves
    if moves and moves.total_change_pct and len({p.currency for p in report.history}) == 1:
        lines.append(f"   Всего: {pct(moves.total_change_pct)} от первой цены")
    return lines


def _other_line(o: OtherListing) -> str:
    who = SELLER_NAMES.get(o.seller_type, o.seller_type)
    if o.other_seller and o.seller_type == "agency":
        who = "другое агентство"
    elif not o.other_seller:
        who = "тот же продавец"
    price = (
        _price_with_usd(o.amount, o.currency, o.usd) if o.amount is not None else "цена не указана"
    )
    text = f"   {_source(o.source)} — {price} ({who})"
    if o.confidence == "low":
        return text + " — возможно, та же квартира, склейка не подтверждена"
    if o.cheaper_by is not None and o.cheaper_by > 0:
        text += f"  ← дешевле на {money(o.cheaper_by, o.cheaper_currency)}"
    elif o.cheaper_by is not None and o.cheaper_by < 0:
        text += f"  дороже на {money(-o.cheaper_by, o.cheaper_currency)}"
    return text


def _others_lines(report: CheckReport, icons: bool) -> list[str]:
    same_source = any(o.source == report.listing.source for o in report.others)
    head = (
        "Другие объявления этой квартиры сейчас:" if same_source else "Сейчас на других площадках:"
    )
    return [f"{_icon('cross', icons)}{head}", *(_other_line(o) for o in report.others)]


def _possible_lines(report: CheckReport, icons: bool) -> list[str]:
    p = report.possible_object
    e = p.exposure
    where = ", ".join(_source(s) for s in p.sources)
    facts = []
    if e is not None:
        facts.append(f"там она на рынке {'как минимум ' if e.at_least else ''}{days_text(e.days)}")
    if p.drops:
        facts.append(f"{p.drops} {plural(p.drops, 'снижение', 'снижения', 'снижений')} цены")
    since = f" с {date_short(e.start)}" if e is not None else ""
    lines = [
        f"{_icon('maybe', icons)}Возможно, это та же квартира, что продаётся на {where}{since}"
        + (f": {', '.join(facts)}." if facts else ".")
    ]
    why = f" ({p.basis})" if p.basis else ""
    lines.append(
        f"   Склейка не подтверждена{why}, поэтому выше — история только этого объявления."
    )
    return lines


def _assessment_lines(report: CheckReport, icons: bool) -> list[str]:
    a = report.assessment
    return [_icon(_VERDICT_ICON[a.kind], icons) + a.verdict] if a else []


_BATHROOM = {"два": "два санузла"}
_BALCONY = {"есть": "балкон", "нет": "без балкона", "два": "два балкона"}


def details_line(d: FlatDetails | None) -> str | None:
    """«Кухня 9 м² · санузел раздельный · лоджия · кирпичный дом · потолки от 2,7»."""
    if d is None:
        return None
    parts = [
        f"кухня {num(d.kitchen_m2)} м²" if d.kitchen_m2 else None,
        f"жилая {num(d.living_m2)} м²" if d.living_m2 else None,
        _BATHROOM.get(d.bathroom, f"санузел {d.bathroom}") if d.bathroom else None,
        _BALCONY.get(d.balcony, d.balcony) if d.balcony else None,
        f"{d.walls} дом" if d.walls else None,
        # Kufar отдаёт только нижнюю границу высоты потолков («от 2.7 м»)
        f"потолки от {num(d.ceiling_m)}" if d.ceiling_m else None,
        f"окна: {d.windows_side}" if d.windows_side else None,
        "обмен возможен" if d.exchange and "exchange" not in d.facts else None,
    ]
    text = " · ".join(p for p in parts if p)
    return text[:1].upper() + text[1:] if text else None


def _facts_text(d: FlatDetails | None, icons: bool) -> str | None:
    """Все факты из описания (без ограничения в 5); icons=False — без эмодзи."""
    text = facts_line(d.facts, limit=None) if d else None
    if text and not icons:
        text = text.replace("⚠️ ", "").replace("📝 ", "")
    return text


def _details_lines(report: CheckReport, icons: bool) -> list[str]:
    d = report.flat.details if report.flat else None
    return [t for t in (details_line(d), _facts_text(d, icons)) if t]


def _place_lines(report: CheckReport, icons: bool) -> list[str]:
    loc, f = report.location, report.flat
    if loc is None:  # координат нет: ни метро, ни «Дом …»
        return []
    if loc.station is None:
        text = "Метро дальше 3 км"
    elif loc.approx or loc.walk_min is None:
        text = f"Метро «{loc.station.name}» — ближайшая станция"
    else:
        metres = max(50, int(loc.station_m / 50 + 0.5) * 50)  # half-up, не «0 м»
        text = f"Метро «{loc.station.name}» — ~{loc.walk_min} мин пешком ({metres} м)"
    floors = f.floors_total if f is not None else None
    if f is not None and f.year:
        floors_text = f", {floors} {plural(floors, 'этаж', 'этажа', 'этажей')}" if floors else ""
        text += f". Дом {f.year} г.{floors_text}"
    elif floors:
        text += f". {floors}-этажный дом"
    return [_icon("place", icons) + text]


_ROOM_WORDS = {
    1: ("однушка", "однушки", "однушек"),
    2: ("двушка", "двушки", "двушек"),
    3: ("трёшка", "трёшки", "трёшек"),
    4: ("квартира 4+ комн.", "квартиры 4+ комн.", "квартир 4+ комн."),
}
_EPOCH_PHRASES = {
    "pre1970": "в домах до 1970 г.",
    "1970_1990": "в домах 1970–90-х",
    "1991_2010": "в домах 1991–2010 гг.",
    "post2010": "в домах после 2010 г.",
    "building": "в строящихся домах",
}
_FINISH_PHRASES = {"none": " без отделки", "done": " с отделкой"}


def _comparison_lines(report: CheckReport, icons: bool) -> list[str]:
    c = report.comparison
    if c is None:
        return []
    district = c.step == "district"
    ref = ("медианы района" if c.district else "медианы по Минску") if district else "похожих"
    if abs(c.diff_pct) < 1:
        diff = f"на уровне {ref}"
    else:
        diff = f"на {pct(c.diff_pct, signed=False)} {'ниже' if c.diff_pct < 0 else 'выше'} {ref}"
    if district:
        where = district_label(c.district) if c.district else "по Минску"
        detail = f"{where}, {rooms_label(c.rooms)}: медиана {ppm(c.median)}, {objects_text(c.n)}"
    else:
        words = _ROOM_WORDS.get(min(c.rooms or 0, 4), ("квартира", "квартиры", "квартир"))
        age = _EPOCH_PHRASES.get(c.epoch, "без учёта возраста дома")
        radius = f"{c.radius_km:g}".replace(".", ",")
        what = plural(c.n, *words) + _FINISH_PHRASES.get(c.finish, "")
        detail = f"{c.n} {what} {age} в радиусе {radius} км, медиана {ppm(c.median)}"
    return [f"{_icon('median', icons)}Цена: {ppm(c.usd_per_m2)} — {diff}", f"   ({detail})"]


def _house_lines(report: CheckReport, icons: bool) -> list[str]:
    prices = report.same_house
    if not prices:
        return []
    n = len(prices)
    if n > 3:
        what = f"от {ppm(prices[0]).removesuffix('/м²')} до {ppm(prices[-1])}"
    else:
        shown = [ppm(p).removesuffix("/м²") for p in prices]
        what = (shown[0] if n == 1 else f"{', '.join(shown[:-1])} и {shown[-1]}") + "/м²"
    verb = plural(n, "продаётся", "продаются", "продаются")
    flats = plural(n, "квартира", "квартиры", "квартир")
    return [f"{_icon('house', icons)}Ещё в этом доме {verb} {n} {flats}: {what}"]


def _house_body(report: CheckReport) -> str | None:
    """Строка про соседей по дому без вводных слов: заголовок карточки «Ещё в этом доме» их несёт."""
    line = next(iter(_house_lines(report, False)), None)
    if line is None:
        return None
    body = line.removeprefix("Ещё в этом доме ")
    return body[:1].upper() + body[1:]


def web_blocks(report: CheckReport) -> dict:
    """Те же блоки оценки, что в тексте бота, но чистой строкой для страницы кабинета."""
    a = report.assessment
    place = _place_lines(report, False)
    details = report.flat.details if report.flat else None
    facts = _facts_text(details, True)
    return {
        "details": details_line(details),
        "facts": facts.splitlines() if facts else [],
        "verdict": a.verdict if a else None,
        "place": place[0] if place else None,
        "comparison": [line.strip() for line in _comparison_lines(report, False)],
        "house": _house_body(report),
    }


def _match_line(report: CheckReport) -> str:
    m = report.match
    if m is None or m.level is None:
        return "Склейка этого объявления ещё не выполнена — показываем только его историю"
    if m.single:
        text = "Уверенность склейки: других объявлений этой квартиры мы не нашли"
        if m.low_listings:
            text += " (есть похожие, но склейка не подтверждена)"
        return text
    level = CONFIDENCE_NAMES.get(m.level, m.level)
    return f"Уверенность склейки: {level}" + (f" ({m.basis})" if m.basis else "")


# --- Недельная сводка ---------------------------------------------------------------------


def render_weekly_text(summary: WeeklySummary, *, icons: bool = True) -> str:
    s, c = summary, summary.counts
    lines = [f"{_icon('median', icons)}Квартиры Минска на продажу, неделя {s.week}"]
    lines.append("• Медиана: " + _city_median(s.median, s.median_change_pct))
    if c.dropped_objects:
        step = f", медианный шаг {pct(c.drop_step_median_pct)}" if c.drop_step_median_pct else ""
        lines.append(f"• Подешевели: {objects_text(c.dropped_objects)}{step}")
    else:
        lines.append("• Подешевели: снижений за неделю не было")
    exp = s.exposure_median
    if exp is not None and exp.enough:
        note = ""
        if s.exposure_truncated_share and s.exposure_truncated_share >= 0.5:
            note = " (как минимум: большую часть квартир мы застали уже в продаже)"
        lines.append(f"• Медианный срок экспозиции: {days_text(round(exp.median))}{note}")
    if c.relisted_share is not None:
        lines.append(f"• Перевыложено «как новые»: {c.relisted_share:.0%} новых объявлений")
    if s.top_drops:
        lines.append(f"{_icon('top', icons)}Топ-{len(s.top_drops)} снижения недели:")
        for i, d in enumerate(s.top_drops, 1):
            what = ", ".join(
                x
                for x in (
                    rooms_label(d.rooms) if d.rooms else None,
                    d.street,
                    district_label(d.district) if d.district else None,
                )
                if x
            )
            lines.append(
                f"   {i}. {what or 'Квартира'}: {money(d.old_amount, d.currency)} → "
                f"{money(d.new_amount, d.currency)} ({pct(d.pct)}) {d.url}"
            )
    if s.observation_start:
        lines.append(
            f"Наблюдаем рынок с {date_full(s.observation_start)}. Цены объявлений, не сделок."
        )
    return "\n".join(lines)


def _city_median(cell: MedianCell | None, change: float | None) -> str:
    if cell is None or not cell.enough:
        n = cell.n if cell else 0
        return f"мало данных ({objects_text(n)})"
    text = ppm(cell.median)
    if change is not None:
        text += f" ({pct(change)} за неделю)"
    return text


# --- Гипотеза ------------------------------------------------------------------------------

DECISIONS = {
    "continue": "ПРОДОЛЖАЕМ: критерии раздела 9 выполнены",
    "stop": (
        "ОСТАНАВЛИВАЕМ продукт (инструмент остаётся личным): "
        "перевыкладываний и расхождений почти нет"
    ),
    "extend": "ПРОДЛЕВАЕМ: цифры пограничные, наблюдаем до 12 недель и решаем по тем же порогам",
    "too_early": "РАНО СУДИТЬ: данных для решения пока мало (метрики предварительные)",
}


def _share(share) -> str:
    value = share.share
    return f"{share.count} из {share.total}" + (f" ({value:.1%})" if value is not None else "")


def render_hypothesis_text(r: HypothesisReport) -> str:
    obs = date_full(r.observation_start) if r.observation_start else "—"
    sources = ", ".join(f"{_source(s)} {n}" for s, n in r.listings_by_source) or "нет"
    rows = [
        ("Перевыкладывались (relisted / вернулись под новым ID)", r.relisted),
        ("На нескольких площадках / у нескольких продавцов (cross_listed)", r.cross_listed),
        ("Перевыкладывались ИЛИ cross_listed", r.relisted_or_cross),
        ("  — среди объектов, которые видим ≥ 14 дней", r.relisted_or_cross_mature),
        ("Есть снижения цены (видели сами)", r.with_drops),
        ("  — среди объектов, которые видим ≥ 14 дней", r.with_drops_mature),
        ("Снижение показывает площадка (в т. ч. до 23.10)", r.with_drops_platform),
        ("Есть снижения: сами ИЛИ со слов площадки", r.with_drops_any),
        (f"Новые объекты (после исходного среза): {r.new_objects}", None),
        ("  — перевыкладывались ИЛИ cross_listed", r.relisted_or_cross_new),
        ("  — есть снижения цены", r.with_drops_new),
        ("Отчёт показывает то, чего нет на сайте", r.with_insight),
        ("Возвращались после снятия", r.returned),
        ("Сейчас сняты", r.removed),
        ("Объявления со склейкой low (среди привязанных)", r.low_listings),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [
        f"Проверка гипотезы на {date_full(r.at)}",
        (
            f"Наблюдаем с {obs} ({days_text(r.observation_days)}); объектов {r.objects}, "
            f"из них видим ≥ 14 дней: {r.mature_objects}; объявлений: {sources}"
        ),
        "",
        "| Метрика | Значение |",
        "|---|---|",
        *(
            f"| {label.ljust(width)} | {_share(share) if share is not None else ''} |"
            for label, share in rows
        ),
        "",
        "Срок экспозиции (дней, для снятых — до снятия):",
    ]
    e = r.exposure
    if e.n:
        lines.append(
            f"   p25 {e.p25:.0f} · медиана {e.p50:.0f} · p75 {e.p75:.0f} · p90 {e.p90:.0f}; "
            f"«как минимум» у {e.at_least_share:.0%}"
        )
        lines += [f"   {b.label:>10}: {b.count} ({b.share:.0%})" for b in e.buckets]
    else:
        lines.append("   нет объектов")
    lines += ["", "Критерии (docs/01-idea.md, раздел 9):"]
    for c in r.criteria:
        value = "нет данных" if c.value is None else f"{c.value:.1%}"
        mark = "да " if c.passed else "нет"
        lines.append(f"   [{mark}] {c.label}: {value} (порог {c.threshold:.0%})")
    lines += ["", f"Рекомендация: {DECISIONS[r.decision]}"]
    lines += [f"   • {reason}" for reason in r.reasons]
    if r.sample_object_ids:
        ids = ", ".join(str(i) for i in r.sample_object_ids)
        lines.append(f"Случайные объекты для ручной проверки /check: {ids}")
    return "\n".join(lines)
