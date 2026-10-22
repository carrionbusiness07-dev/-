"""events: dedup_key, notify, created_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 13:31:33.413610

События пишет склейка (minsk_realty.matching):
- dedup_key — детерминированный ключ события, уникальный в пределах объекта:
  повторная обработка не создаёт дублей;
- notify — False для событий из склейки с низкой уверенностью (в историю, но не в уведомления);
- created_at — когда событие заметили (at — когда оно произошло на рынке).

Колонки добавляются как NULL, заполняются для уже существующих строк и только потом
становятся NOT NULL — так миграция проходит и на непустой таблице.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dedup_key", sa.String(length=96), nullable=True))
        batch_op.add_column(
            sa.Column("notify", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))

    # Старые события (если есть) получают уникальный ключ по id и время создания = at.
    op.execute("UPDATE events SET dedup_key = 'legacy:' || id, created_at = at")

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.alter_column("dedup_key", existing_type=sa.String(length=96), nullable=False)
        batch_op.alter_column(
            "created_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch_op.create_index(batch_op.f("ix_events_created_at"), ["created_at"], unique=False)
        batch_op.create_unique_constraint(
            batch_op.f("uq_events_object_id_dedup_key"), ["object_id", "dedup_key"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_events_object_id_dedup_key"), type_="unique")
        batch_op.drop_index(batch_op.f("ix_events_created_at"))
        batch_op.drop_column("created_at")
        batch_op.drop_column("notify")
        batch_op.drop_column("dedup_key")
