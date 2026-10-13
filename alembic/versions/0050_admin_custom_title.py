"""admin custom title

Revision ID: 0050_admin_custom_title
Revises: c7f4a1e2b9d3
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0050_admin_custom_title"
down_revision = "c7f4a1e2b9d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rank_assignments",
        sa.Column("custom_title", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rank_assignments", "custom_title")