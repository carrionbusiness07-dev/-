"""BotService: всё, что бот делает с БД, — синхронно, по сессии на вызов.

Хендлеры aiogram зовут эти методы через `asyncio.to_thread` и только отправляют результат.
Методы возвращают готовый HTML-текст и клавиатуру (`Reply`), поэтому их удобно проверять
тестами без Telegram.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.orm import Session, sessionmaker

from minsk_realty.analytics import (
    CheckReport,
    MarketBaseline,
    WeeklySummary,
    build_check_report,
    compute_baseline,
    load_market,
    market_baseline,
)
from minsk_realty.analytics.search import (
    SearchItem,
    SearchQuery,
    Sort,
    build_index,
    parse_budget,
    summary_line,
)
from minsk_realty.analytics.search import search as find_flats
from minsk_realty.bot import keyboards, links, notify, search, texts, weekly
from minsk_realty.bot.config import BotConfig
from minsk_realty.bot.watchlist import (
    WatchResult,
    add_watch,
    current_listing,
    flat_name,
    forget_user,
    get_or_create_user,
    has_checked_listing,
    has_seen_object,
    is_watching,
    last_search,
    remove_watch,
    save_search,
    watch_items,
    watchable_object,
)
from minsk_realty.db.models import Check, Listing
from minsk_realty.db.repository import data_version, find_listing

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Venue:
    """Точка с подписью для `sendVenue` (Telegram показывает заголовок и адрес под картой)."""

    lat: float
    lon: float
    title: str
    address: str


@dataclass(frozen=True)
class Reply:
    text: str
    keyboard: InlineKeyboardMarkup | None = None
    venue: Venue | None = None  # точка дома, отправляется после отчёта


def reply_venue(report: CheckReport) -> Venue | None:
    """Точка дома с подписью; у неточной (центр района у Realt/Domovita) подпись это говорит."""
    loc = report.location
    if loc is None:
        return None
    street = report.flat.street
    if loc.approx:
        return Venue(
            loc.lat, loc.lon, f"≈ {street or 'Минск'} (примерно)", "точка площадки неточная"
        )
    district = report.flat.district
    return Venue(loc.lat, loc.lon, street or "Квартира", f"{district} р-н" if district else "Минск")


class BaselineCache:
    """Медианы рынка для /check: считаются по всей БД, поэтому кэш на `ttl` (час)."""

    def __init__(self, cfg: BotConfig) -> None:
        self.ttl = cfg.baseline_ttl
        self._value: MarketBaseline | None = None
        self._at: datetime | None = None
        self._lock = threading.Lock()

    def get(self, session: Session, now: datetime) -> MarketBaseline:
        with self._lock:
            if self._value is None or self._at is None or now - self._at >= self.ttl:
                self._value = market_baseline(session, now)
                self._at = now
            return self._value


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="search-index", daemon=True).start()


class IndexCache:
    """Индекс поиска. На живом рынке он строится около минуты, поэтому при смене
    `data_version` (закончился проход сборщика) пересобирается в фоне, по одной сборке за раз,
    а запросы до готовности получают прежний индекс. `None` — первого индекса ещё нет.

    `run` запускает сборку: по умолчанию в фоновом потоке, в тестах — сразу или по команде."""

    def __init__(
        self, factory: sessionmaker, run: Callable[[Callable[[], None]], None] = _in_thread
    ) -> None:
        self.factory, self._run = factory, run
        self._index: tuple[SearchItem, ...] | None = None
        self._version: tuple | None = None
        self._building = False
        self._lock = threading.Lock()

    def get(self, session: Session, now: datetime) -> tuple[SearchItem, ...] | None:
        version = data_version(session)
        with self._lock:
            start = version != self._version and not self._building
            self._building = self._building or start
        if start:
            self._run(lambda: self._rebuild(version, now))
        return self._index

    def _rebuild(self, version: tuple, now: datetime) -> None:
        try:
            with self.factory() as session:
                data = load_market(session)
                index = build_index(data, compute_baseline(data, now), now)
            with self._lock:
                self._index, self._version = index, version
        except Exception as exc:  # noqa: BLE001 — остаётся прежний индекс, следующий запрос повторит
            log.error("индекс поиска: сбой сборки, остаётся прежний: %s", type(exc).__name__)
        finally:
            with self._lock:
                self._building = False


@dataclass(frozen=True)
class Card:
    """Карточка квартиры в выдаче: одно сообщение с превью ссылки площадки."""

    text: str
    keyboard: InlineKeyboardMarkup | None
    preview_url: str | None


@dataclass(frozen=True)
class SearchPage:
    summary: str | None  # первое сообщение выдачи
    cards: list[Card]
    more: InlineKeyboardMarkup | None  # под последним сообщением: «Ещё 5» или «Найти квартиру»


class BotService:
    def __init__(
        self,
        factory: sessionmaker,
        cfg: BotConfig,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.factory, self.cfg, self.clock = factory, cfg, clock
        self.baseline = BaselineCache(cfg)
        self.index = IndexCache(factory)
        # Черновики анкеты поиска; после перезапуска их нет — кнопки отвечают «устарела».
        self.drafts: dict[int, search.SearchDraft] = {}

    # --- /check -----------------------------------------------------------------------------

    def check_text(self, telegram_id: int, text: str | None) -> Reply:
        """Сообщение пользователя (ссылка или текст со ссылкой) → отчёт или подсказка."""
        found = links.find_links(text)
        if not found:
            return Reply(texts.no_link_text())
        link = links.first_supported(found)
        if link is None:
            self._log_unsupported(telegram_id, found)
            return Reply(texts.unsupported_text(found))
        several = sum(1 for item in found if item.supported) > 1
        return self.check_link(
            telegram_id, link, note=texts.several_links_note() if several else ""
        )

    def check_link(self, telegram_id: int, link: links.Link, *, note: str = "") -> Reply:
        with self.factory() as session:
            now = self.clock()
            report = build_check_report(
                session, link.url, now, baseline=self.baseline.get(session, now)
            )
            reply = self._report_reply(session, telegram_id, report, note)
            self._log_check(session, telegram_id, report, link.clean, now)
            session.commit()
            return reply

    def report_for_object(self, telegram_id: int, object_id: int) -> Reply:
        """Свежий отчёт по квартире из списка или уведомления."""
        with self.factory() as session:
            now = self.clock()
            if not has_seen_object(session, telegram_id, object_id):
                return Reply(texts.stale_button_text())
            current = current_listing(load_market(session, object_ids=[object_id]), object_id)
            if current is None:
                return Reply(texts.esc("Этой квартиры больше нет в базе."))
            report = build_check_report(
                session, f"listing:{current.id}", now, baseline=self.baseline.get(session, now)
            )
            reply = self._report_reply(session, telegram_id, report)
            self._log_check(session, telegram_id, report, current.url, now)
            session.commit()
            return reply

    def warm_up(self) -> None:
        """Посчитать медианы заранее, чтобы первый /check после старта был быстрым,
        и запустить первую сборку индекса поиска (в фоне)."""
        with self.factory() as session:
            now = self.clock()
            self.baseline.get(session, now)
            self.index.get(session, now)

    def _report_reply(
        self, session: Session, telegram_id: int, report: CheckReport, note: str = ""
    ) -> Reply:
        if report.status != "ok" or report.listing is None:
            return Reply(texts.report_text(report, can_watch=False, prefix=note))
        listing = session.get(Listing, report.listing.listing_id)
        object_id = watchable_object(listing)
        can_watch = report.scope == "object" and object_id is not None
        markup = keyboards.report_keyboard(
            listing_id=report.listing.listing_id,
            url=report.listing.url,
            object_id=object_id,
            can_watch=can_watch,
            watching=can_watch and is_watching(session, telegram_id, object_id),
            suggest_watch=report.suggest_watch,
        )
        text = texts.report_text(report, can_watch=can_watch, prefix=note)
        return Reply(text, markup, reply_venue(report))

    def _log_check(
        self, session: Session, telegram_id: int, report: CheckReport, url: str, now: datetime
    ) -> None:
        if report.status == "unsupported_url":
            return
        user = get_or_create_user(session, telegram_id, now)
        listing_id = report.listing.listing_id if report.listing else None
        session.add(Check(user_id=user.id, url=url[:512], listing_id=listing_id, created_at=now))

    def _log_unsupported(self, telegram_id: int, found: list[links.Link]) -> None:
        """Ссылки на Onliner/Domovita пишем в checks: это спрос на следующие площадки.
        Прочие чужие ссылки не храним вовсе."""
        coming = next((item for item in found if item.coming_soon), None)
        if coming is None:
            return
        with self.factory() as session:
            now = self.clock()
            user = get_or_create_user(session, telegram_id, now)
            session.add(Check(user_id=user.id, url=coming.clean, listing_id=None, created_at=now))
            session.commit()

    # --- Поиск ------------------------------------------------------------------------------

    def search_start(self, telegram_id: int) -> Reply:
        """Новая анкета (старый черновик забывается); первой — «Повторить: <запрос>»."""
        with self.factory() as session:
            last = last_search(session, telegram_id)
        draft = self.drafts[telegram_id] = search.SearchDraft()
        return Reply(*search.render(draft, SearchQuery.from_json(last) if last else None))

    def search_button(self, telegram_id: int, cb: search.SearchCallback | None) -> Reply:
        """Кнопка шага анкеты (не «go»/«rep»/«new»): следующий вид того же сообщения."""
        draft = self.drafts.get(telegram_id)
        if cb is None or draft is None:
            return Reply(texts.search_stale_text(), search.new_search_keyboard())
        search.apply(draft, cb)
        return Reply(*search.render(draft))

    def search_text(self, telegram_id: int, text: str | None) -> Reply | None:
        """Сумма на шаге «своя сумма». None — текст не для анкеты: его обрабатывает обычный путь
        (ссылка → отчёт, иначе подсказка), черновик остаётся как был."""
        draft = self.drafts.get(telegram_id)
        if draft is None or draft.step != "budget_custom":
            return None
        budget = parse_budget(text or "")
        if budget is None:
            return None
        search.set_budget(draft, *budget)
        return Reply(*search.render(draft))

    def search_bind(self, telegram_id: int, message_id: int) -> None:
        """Запомнить сообщение анкеты, чтобы править его после текста."""
        draft = self.drafts.get(telegram_id)
        if draft is not None:
            draft.message_id = message_id

    def search_go(self, telegram_id: int, *, repeat: bool = False) -> SearchPage:
        """«Показать квартиры» (или «Повторить»): запомнить запрос и показать первую страницу."""
        with self.factory() as session:
            if repeat:
                last = last_search(session, telegram_id)
                if last is None:
                    return self._stale_page()
                query = SearchQuery.from_json(last)
                self.drafts[telegram_id] = search.SearchDraft(query, step="more")
            draft = self.drafts.get(telegram_id)
            if draft is None:
                return self._stale_page()
            save_search(session, telegram_id, draft.query.to_json())
            session.commit()
        return self.search_page(telegram_id, "best", 0)

    def search_page(self, telegram_id: int, sort: Sort, offset: int) -> SearchPage:
        """Страница выдачи по запросу черновика. Карточки и «Ещё 5» — следующий шаг."""
        draft = self.drafts.get(telegram_id)
        if draft is None:
            return self._stale_page()
        with self.factory() as session:
            index = self.index.get(session, self.clock())
        if index is None:
            return SearchPage(texts.search_preparing_text(), [], None)
        result = find_flats(index, draft.query, sort)
        return SearchPage(texts.esc(summary_line(result, sort)), [], None)

    def _stale_page(self) -> SearchPage:
        return SearchPage(texts.search_stale_text(), [], search.new_search_keyboard())

    # --- Список -----------------------------------------------------------------------------

    def watch_listing(self, telegram_id: int, listing_id: int) -> tuple[Reply, WatchResult]:
        """Кнопка «Следить» под отчётом."""
        with self.factory() as session:
            if not has_checked_listing(session, telegram_id, listing_id):
                result = WatchResult("denied", limit=self.cfg.watch_limit)
                return Reply(texts.watch_result_text(result)), result
            listing = session.get(Listing, listing_id)
            result = add_watch(session, telegram_id, listing, self.clock(), self.cfg.watch_limit)
            name = self._name(session, result.object_id)
            session.commit()
            return Reply(texts.watch_result_text(result, name)), result

    def watch_text(self, telegram_id: int, text: str | None) -> Reply:
        """/watch <ссылка>."""
        link = links.first_supported(links.find_links(text))
        if link is None:
            found = links.find_links(text)
            return Reply(texts.unsupported_text(found) if found else texts.watch_usage_text())
        with self.factory() as session:
            now = self.clock()
            listing = self._listing(session, link)
            result = add_watch(session, telegram_id, listing, now, self.cfg.watch_limit)
            name = self._name(session, result.object_id)
            if listing is not None:  # ссылку прислал сам пользователь — как /check
                user = get_or_create_user(session, telegram_id, now)
                session.add(
                    Check(user_id=user.id, url=link.clean, listing_id=listing.id, created_at=now)
                )
            session.commit()
            markup = None
            if result.status in ("added", "already") and listing is not None:
                markup = keyboards.report_keyboard(
                    listing_id=listing.id,
                    url=listing.url,
                    object_id=result.object_id,
                    can_watch=True,
                    watching=True,
                    suggest_watch=False,
                )
            return Reply(texts.watch_result_text(result, name), markup)

    def unwatch(self, telegram_id: int, object_id: int) -> bool:
        with self.factory() as session:
            ok = remove_watch(session, telegram_id, object_id)
            session.commit()
            return ok

    def unwatch_text(self, telegram_id: int, text: str | None) -> Reply:
        """/unwatch <ссылка>; без ссылки — список с кнопками."""
        link = links.first_supported(links.find_links(text))
        if link is None:
            return self.watchlist(telegram_id) if not text else Reply(texts.unwatch_usage_text())
        with self.factory() as session:
            listing = self._listing(session, link)
            ok = bool(listing and listing.object_id) and remove_watch(
                session, telegram_id, listing.object_id
            )
            session.commit()
            return Reply(texts.unwatched_text(ok))

    def report_markup(self, telegram_id: int, listing_id: int) -> InlineKeyboardMarkup | None:
        """Клавиатура отчёта после «Следить»/«Не следить» (без пересборки отчёта)."""
        with self.factory() as session:
            listing = session.get(Listing, listing_id)
            if listing is None:
                return None
            object_id = watchable_object(listing)
            return keyboards.report_keyboard(
                listing_id=listing.id,
                url=listing.url,
                object_id=object_id,
                can_watch=object_id is not None,
                watching=is_watching(session, telegram_id, object_id),
                suggest_watch=False,
            )

    def watchlist(self, telegram_id: int) -> Reply:
        with self.factory() as session:
            items = watch_items(session, telegram_id, self.clock())
            return Reply(
                texts.watchlist_text(items, self.cfg.watch_limit), keyboards.list_keyboard(items)
            )

    def forget(self, telegram_id: int) -> Reply:
        with self.factory() as session:
            ok = forget_user(session, telegram_id)
            session.commit()
            return Reply(texts.forgotten_text(ok))

    def _listing(self, session: Session, link: links.Link) -> Listing | None:
        return find_listing(session, link.source, link.external_id)

    def _name(self, session: Session, object_id: int | None):
        if object_id is None:
            return None
        return flat_name(load_market(session, object_ids=[object_id]), object_id)

    # --- Уведомления ------------------------------------------------------------------------

    def prepare_notifications(self) -> list[notify.Outgoing]:
        """Отобрать и зарезервировать сообщения (коммит до отправки)."""
        with self.factory() as session:
            now = self.clock()
            outgoing = notify.due_notifications(session, now, self.cfg)
            notify.reserve(session, outgoing, now)
            session.commit()
            return outgoing

    def finish_notifications(self, results: list[tuple[notify.Outgoing, notify.Outcome]]) -> None:
        if not results:
            return
        with self.factory() as session:
            notify.finish(session, results, self.clock())
            session.commit()

    def recover(self) -> int:
        with self.factory() as session:
            n = notify.recover_pending(session)
            session.commit()
            return n

    # --- Недельная сводка -------------------------------------------------------------------

    def weekly_summary(self) -> tuple[WeeklySummary, datetime | None]:
        """Сводка за прошлую неделю и когда её уже публиковали (для предпросмотра)."""
        with self.factory() as session:
            summary = weekly.last_week_summary(session, self.clock())
            post = weekly.find_post(session, weekly.period_key(summary))
            published = post.sent_at if post is not None and post.status == "sent" else None
            return summary, published

    def weekly_preview(self) -> str:
        summary, published = self.weekly_summary()
        return texts.weekly_preview_text(summary, published_at=published)

    def reserve_weekly(self, *, force: bool = False, scheduled: bool = False) -> weekly.Reservation:
        """Занять публикацию сводки за прошлую неделю (запись — до отправки).

        По расписанию — только в окне публикации и только если за этот период ещё не было
        ни одной попытки (неудачную повторяет админ командой, а не бот каждые 10 минут)."""
        with self.factory() as session:
            now = self.clock()
            if scheduled and weekly.publish_window(now, self.cfg) is None:
                return weekly.Reservation("skipped", "")
            summary = weekly.last_week_summary(session, now)
            period = weekly.period_key(summary)
            if scheduled and weekly.find_post(session, period) is not None:
                return weekly.Reservation("skipped", period)
            result = weekly.reserve_post(session, period, now, force=force)
            session.commit()
            if result.status != "reserved":
                return result
            return weekly.Reservation("reserved", period, text=texts.weekly_text(summary))

    def recover_weekly(self) -> list[str]:
        with self.factory() as session:
            periods = weekly.recover_posts(session)
            session.commit()
            return periods

    def finish_weekly(self, period: str, ok: bool) -> None:
        with self.factory() as session:
            weekly.finish_post(session, period, ok, self.clock())
            session.commit()
