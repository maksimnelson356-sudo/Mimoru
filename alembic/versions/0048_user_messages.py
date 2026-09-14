"""user messages

Revision ID: 0048_user_messages
Revises: 0047_complaint_notifications
Create Date: 2026-09-14
"""

import sqlalchemy as sa

from alembic import op

revision = "0048_user_messages"
down_revision = "0047_complaint_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_user_messages_group_id",
        "user_messages",
        ["group_id"],
    )
    op.create_index(
        "ix_user_messages_user_telegram_id",
        "user_messages",
        ["user_telegram_id"],
    )
    op.create_index(
        "ix_user_messages_created_at",
        "user_messages",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_messages_created_at", "user_messages")
    op.drop_index("ix_user_messages_user_telegram_id", "user_messages")
    op.drop_index("ix_user_messages_group_id", "user_messages")
    op.drop_table("user_messages")
