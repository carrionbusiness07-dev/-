"""bot: последний поиск квартир и показанные в поиске объекты

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31 10:00:00

- saved_searches — последний поиск пользователя (user_id, params, updated_at): параметры
  фильтра в json, чтобы повторить поиск и показать следующую страницу.
- search_shown — какие объекты показаны пользователю в поиске (user_id, object_id,
  shown_at). По этой таблице кнопка «Подробно» понимает, что объект пользователь уже видел.

Оба ряда уходят вместе с пользователем (ON DELETE CASCADE). Имён, телефонов и текста
запросов не храним. Новые таблицы, существующие данные не трогаются.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "saved_searches",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "params", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_saved_searches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_saved_searches")),
    )
    op.create_table(
        "search_shown",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("shown_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["objects.id"],
            name=op.f("fk_search_shown_object_id_objects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_search_shown_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "object_id", name=op.f("pk_search_shown")),
    )
    with op.batch_alter_table("search_shown", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_search_shown_object_id"), ["object_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("search_shown", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_search_shown_object_id"))

    op.drop_table("search_shown")
    op.drop_table("saved_searches")
