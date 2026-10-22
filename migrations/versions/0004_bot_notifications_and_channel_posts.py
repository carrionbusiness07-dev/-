"""bot: журнал уведомлений и публикаций в канал

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22 15:39:44

- notifications — какие события какому пользователю бот отправил. Строка пишется до
  отправки (pending) и после неё становится sent/failed. Уникальность (user_id, event_id)
  не даёт отправить одно событие дважды, в том числе после перезапуска бота. По created_at
  бот соблюдает «не больше одного уведомления в час по одному объекту».
- channel_posts — публикации в канал (недельная сводка): одна на период (kind, period).

Новые таблицы, существующие данные не трогаются.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _status() -> sa.Enum:
    return sa.Enum(
        "pending",
        "sent",
        "failed",
        name="delivery_status",
        native_enum=False,
        create_constraint=True,
        length=16,
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "channel_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("status", _status(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_posts")),
        sa.UniqueConstraint("kind", "period", name=op.f("uq_channel_posts_kind_period")),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", _status(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_notifications_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["objects.id"],
            name=op.f("fk_notifications_object_id_objects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("user_id", "event_id", name=op.f("uq_notifications_user_id_event_id")),
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_event_id"), ["event_id"], unique=False)
        batch_op.create_index(
            "ix_notifications_user_object_created",
            ["user_id", "object_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index("ix_notifications_user_object_created")
        batch_op.drop_index(batch_op.f("ix_notifications_event_id"))

    op.drop_table("notifications")
    op.drop_table("channel_posts")
