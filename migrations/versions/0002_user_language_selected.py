"""Add language_selected flag to users for first-time /start language prompt.

Revision ID: 0002_user_language_selected
Revises: 0001_initial_schema
Create Date: 2026-11-19 02:25:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_user_language_selected"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("language_selected", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE users SET language_selected = TRUE")


def downgrade() -> None:
    op.drop_column("users", "language_selected")
