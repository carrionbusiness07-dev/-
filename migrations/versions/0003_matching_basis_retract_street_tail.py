"""matching: основание склейки, отзыв событий, ключ улицы для отбора кандидатов

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22 18:00:00

- listings.match_basis — почему объявление в объекте (место, число совпавших фото,
  перевыкладывание; у основного объявления {"rule": "founder"}). NULL у старых склеек.
- events.trusted — событие из надёжной склейки (история отчёта). До 0003 эту роль играл
  notify (False только у событий цены из склеек low), поэтому trusted := notify.
- events.retracted_at — событие отозвано (объявление отцепили от объекта). Строку не
  удаляем: событие могло уже уйти пользователю.
- objects.street_tail — последнее слово нормализованной улицы: отбор кандидатов склейки по
  индексу (street_tail, rooms, area_total) вместо чтения всех объектов с той же площадью.
  street_norm хранится как «<тип> <название>», поэтому ключ — последнее слово строки.
- индекс (rooms, area_total) — кандидаты без улицы (по координатам или по фото).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("match_basis", json_type, nullable=True))

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("trusted", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.add_column(sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE events SET trusted = notify")

    with op.batch_alter_table("objects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("street_tail", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_objects_street_tail_rooms_area",
            ["street_tail", "rooms", "area_total"],
            unique=False,
        )
        batch_op.create_index("ix_objects_rooms_area", ["rooms", "area_total"], unique=False)

    # Ключ улицы для существующих объектов: последнее слово street_norm.
    objects = sa.table(
        "objects",
        sa.column("id", sa.Integer),
        sa.column("street_norm", sa.String),
        sa.column("street_tail", sa.String),
    )
    if not context.is_offline_mode():
        conn = op.get_bind()
        rows = conn.execute(
            sa.select(objects.c.id, objects.c.street_norm).where(objects.c.street_norm.is_not(None))
        ).all()
        for object_id, street_norm in rows:
            words = street_norm.split()
            if words:
                conn.execute(
                    objects.update()
                    .where(objects.c.id == object_id)
                    .values(street_tail=words[-1][:64])
                )
    else:
        # Офлайн-SQL (PostgreSQL): то же одним запросом.
        op.execute(
            "UPDATE objects SET street_tail = left(regexp_replace(street_norm, '^.*\\s', ''), 64) "
            "WHERE street_norm IS NOT NULL"
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("objects", schema=None) as batch_op:
        batch_op.drop_index("ix_objects_rooms_area")
        batch_op.drop_index("ix_objects_street_tail_rooms_area")
        batch_op.drop_column("street_tail")

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("retracted_at")
        batch_op.drop_column("trusted")

    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_column("match_basis")
