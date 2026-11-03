"""listings: тип продавца «застройщик» (developer)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31 20:00:00

Realt помечает объявления застройщика (`realEstateDevUuid`). Раньше они записывались как
«собственник»; после следующего прохода сборщик сам перепишет им тип. Меняется только
CHECK-ограничение, данные не трогаются.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _seller_check(values: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{v}'" for v in values)
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_listings_seller_type"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_listings_seller_type"), f"seller_type IN ({allowed})"
        )


def upgrade() -> None:
    """Upgrade schema."""
    _seller_check(("owner", "agency", "developer", "unknown"))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE listings SET seller_type = 'unknown' WHERE seller_type = 'developer'")
    _seller_check(("owner", "agency", "unknown"))
