"""Схема БД. Изменения схемы — только вместе с миграцией Alembic (migrations/)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

SOURCES = ("kufar", "realt", "onliner", "domovita")
SELLER_TYPES = ("owner", "agency", "unknown")
CURRENCIES = ("USD", "BYN", "EUR", "RUB")
CONFIDENCE = ("high", "medium", "low")
LISTING_STATUSES = ("active", "inactive")
EVENT_TYPES = ("new", "price_drop", "price_up", "relisted", "cross_listed", "removed", "returned")
RUN_STATUSES = ("running", "ok", "failed", "blocked")
DELIVERY_STATUSES = ("pending", "sent", "failed")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Время всегда в UTC и всегда с часовым поясом — и в PostgreSQL, и в SQLite.

    SQLite не хранит часовой пояс: без этого типа даты читаются «наивными»,
    и сравнение с aware-датами либо падает, либо сравнивает настенное время.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: передавайте время с часовым поясом")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# jsonb в PostgreSQL: сравнение, GROUP BY и индексы; обычный JSON в SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _enum(values: tuple[str, ...], name: str) -> Enum:
    # native_enum=False: обычная строка + CHECK — одинаково в SQLite и PostgreSQL, проще миграции.
    return Enum(*values, name=name, native_enum=False, create_constraint=True, length=16)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class RealtyObject(Base):
    """Физическая квартира. К ней Emma (matching) привязывает объявления с разных площадок.

    Поля объекта — по основному (первому) объявлению, дополненные и обновлённые по надёжным
    объявлениям (`matching.matcher.refresh_object`). `street_tail` — последнее слово
    нормализованной улицы: ключ отбора кандидатов склейки в БД (индекс вместе с комнатами
    и площадью).
    """

    __tablename__ = "objects"
    __table_args__ = (
        Index("ix_objects_street_tail_rooms_area", "street_tail", "rooms", "area_total"),
        Index("ix_objects_rooms_area", "rooms", "area_total"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str | None] = mapped_column(String(128), index=True)
    street_norm: Mapped[str | None] = mapped_column(String(128))
    street_tail: Mapped[str | None] = mapped_column(String(64))
    house: Mapped[str | None] = mapped_column(String(16))
    rooms: Mapped[int | None]
    area_total: Mapped[float | None] = mapped_column(Float)
    floor: Mapped[int | None]
    floors_total: Mapped[int | None]
    town: Mapped[str | None] = mapped_column(String(64))
    district: Mapped[str | None] = mapped_column(String(64))
    microdistrict: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    listings: Mapped[list[Listing]] = relationship(back_populates="object")


class Listing(Base):
    """Одно объявление на одной площадке."""

    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(_enum(SOURCES, "source"))
    external_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))

    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"), index=True)
    match_confidence: Mapped[str | None] = mapped_column(_enum(CONFIDENCE, "match_confidence"))
    # Основание склейки (пишет matching): {"rule": "founder"} у основного объявления объекта,
    # иначе {"rule": "compare", "location": "fingerprint"|"address"|"geo"|"none",
    # "photo_matches": n, "photos_needed": k, "relist": bool}. NULL — склейка до 0003.
    match_basis: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    seller_type: Mapped[str] = mapped_column(_enum(SELLER_TYPES, "seller_type"), default="unknown")
    agency_name: Mapped[str | None] = mapped_column(String(256))
    seller_key: Mapped[str | None] = mapped_column(String(32), index=True)

    status: Mapped[str] = mapped_column(_enum(LISTING_STATUSES, "listing_status"), default="active")
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    posted_at_source: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at_source: Mapped[datetime | None] = mapped_column(UTCDateTime())

    object: Mapped[RealtyObject | None] = relationship(back_populates="listings")
    snapshots: Mapped[list[Snapshot]] = relationship(
        back_populates="listing", order_by="Snapshot.seen_at"
    )


class Snapshot(Base):
    """Состояние объявления. Новая строка пишется только когда значимые поля изменились;
    факт «объявление ещё висит» отражает `listings.last_seen`."""

    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshots_listing_seen", "listing_id", "seen_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    content_hash: Mapped[str] = mapped_column(String(32))

    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_currency: Mapped[str | None] = mapped_column(_enum(CURRENCIES, "currency"))
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    rooms: Mapped[int | None]
    area_total: Mapped[float | None] = mapped_column(Float)
    floor: Mapped[int | None]
    floors_total: Mapped[int | None]
    street: Mapped[str | None] = mapped_column(String(128))
    house: Mapped[str | None] = mapped_column(String(16))
    address_raw: Mapped[str | None] = mapped_column(String(256))
    district: Mapped[str | None] = mapped_column(String(64))
    town: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    listing: Mapped[Listing] = relationship(back_populates="snapshots")


class PhotoHash(Base):
    """Перцептивный хеш фото. Самих фото в БД нет, ссылок в этой таблице тоже.

    Первые 6 ссылок на фото хранятся в `snapshots.extra["photo_urls"]` — публичные ссылки
    на фото объявления, без персональных данных; по ним шаг сборщика считает хеши.
    """

    __tablename__ = "photo_hashes"
    __table_args__ = (UniqueConstraint("listing_id", "phash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    phash: Mapped[str] = mapped_column(String(16), index=True)


class Event(Base):
    """Лента изменений объекта — основа отчёта /check и уведомлений.

    События пишет Emma (minsk_realty.matching). `dedup_key` делает генерацию идемпотентной:
    повторная обработка не создаёт дублей. `at` — когда это произошло на рынке,
    `created_at` — когда мы это заметили (по нему удобно рассылать новое).

    - `trusted` — событие из надёжной склейки (high/medium): из таких строится история
      объекта в отчёте. False — события цены объявления, склеенного с low.
    - `notify` — можно отправить пользователю: trusted, не отозвано и свежее (произошло
      не раньше `MatchingConfig.notify_max_age` до того, как мы его заметили). Старые события
      при первом запуске или поздней склейке идут в историю без уведомления.
    - `retracted_at` — событие отозвано: объявление, на котором оно основано, отцепили от
      объекта. В историю и уведомления такие события не идут. Рассылка должна брать только
      `notify AND retracted_at IS NULL`.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_object_at", "object_id", "at"),
        UniqueConstraint("object_id", "dedup_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"))
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    type: Mapped[str] = mapped_column(_enum(EVENT_TYPES, "event_type"))
    at: Mapped[datetime] = mapped_column(UTCDateTime())
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    dedup_key: Mapped[str] = mapped_column(String(96))
    notify: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    trusted: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    retracted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class User(Base):
    """Пользователь бота. Храним только Telegram id — без имени и username."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Watch(Base):
    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    object_id: Mapped[int] = mapped_column(
        ForeignKey("objects.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Check(Base):
    """Запрос /check: нужен для метрик пилота и будущей оплаты."""

    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(512))
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Notification(Base):
    """Событие, отправленное (или зарезервированное к отправке) пользователю ботом.

    Строка пишется и коммитится ДО отправки (status="pending"), после — "sent" или "failed".
    Уникальность (user_id, event_id) гарантирует, что одно событие не уйдёт дважды даже после
    перезапуска; "pending", оставшийся от упавшего процесса, повторно не отправляется
    (лучше потерять уведомление, чем прислать его дважды). По `created_at` бот соблюдает
    «не чаще раза в час по одному объекту».
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id"),
        Index("ix_notifications_user_object_created", "user_id", "object_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(
        _enum(DELIVERY_STATUSES, "delivery_status"), default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ChannelPost(Base):
    """Публикация в канал (недельная сводка): одна на период, без повторов после перезапуска."""

    __tablename__ = "channel_posts"
    __table_args__ = (UniqueConstraint("kind", "period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # "weekly"
    period: Mapped[str] = mapped_column(String(16))  # "2026-W43"
    status: Mapped[str] = mapped_column(
        _enum(DELIVERY_STATUSES, "delivery_status"), default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SourceRun(Base):
    """Один проход сборщика по площадке — для алертов о здоровье источников."""

    __tablename__ = "source_runs"
    __table_args__ = (Index("ix_source_runs_source_started", "source", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(_enum(SOURCES, "source"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(_enum(RUN_STATUSES, "run_status"), default="running")
    # True только если сборщик дошёл до последней страницы выдачи без обрыва.
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    listings_seen: Mapped[int] = mapped_column(Integer, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(1024))
