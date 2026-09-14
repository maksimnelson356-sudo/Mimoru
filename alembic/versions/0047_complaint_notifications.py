"""complaint notifications

Revision ID: 0047_complaint_notifications
Revises: 0046_game_engine_core
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0047_complaint_notifications"
down_revision = "0046_game_engine_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "complaint_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "complaint_id",
            sa.Integer(),
            sa.ForeignKey("complaints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_complaint_notifications_complaint_id",
        "complaint_notifications",
        ["complaint_id"],
    )
    op.create_index(
        "ix_complaint_notifications_admin_telegram_id",
        "complaint_notifications",
        ["admin_telegram_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_complaint_notifications_admin_telegram_id")
    op.drop_index("ix_complaint_notifications_complaint_id")
    op.drop_table("complaint_notifications")
