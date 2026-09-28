"""add run_id to budget halts

Revision ID: 1dd12b8e308b
Revises: 3ef3f8649043
Create Date: 2026-09-28 19:59:09.484509+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types are rendered fully qualified by autogenerate.
import polska.db.types  # noqa: F401

revision: str = "1dd12b8e308b"
down_revision: str | None = "3ef3f8649043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Named explicitly: autogenerate left this unnamed, which renders a
#: drop_constraint(None, ...) in downgrade() that fails as soon as anyone
#: actually runs it. Given a name here so downgrade works too, not just up.
_FK_NAME = "fk_budget_halts_run_id_runs"


def upgrade() -> None:
    with op.batch_alter_table("budget_halts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_budget_halts_run_id"), ["run_id"], unique=False)
        batch_op.create_foreign_key(_FK_NAME, "runs", ["run_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("budget_halts", schema=None) as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_budget_halts_run_id"))
        batch_op.drop_column("run_id")
