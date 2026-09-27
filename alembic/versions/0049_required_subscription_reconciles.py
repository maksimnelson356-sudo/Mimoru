"""durable required subscription reconciliation intents

Revision ID: 0049_required_subscription_reconciles
Revises: 0048_user_messages
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0049_required_subscription_reconciles"
down_revision = "0048_user_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "required_subscription_reconciles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_username", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_required_reconcile_dedupe_key"),
    )
    op.create_index(
        "ix_required_subscription_reconciles_group_id",
        "required_subscription_reconciles",
        ["group_id"],
    )
    op.create_index(
        "ix_required_subscription_reconciles_status",
        "required_subscription_reconciles",
        ["status"],
    )
    op.create_index(
        "ix_required_subscription_reconciles_next_attempt_at",
        "required_subscription_reconciles",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_required_subscription_reconciles_created_at",
        "required_subscription_reconciles",
        ["created_at"],
    )
    op.create_index(
        "ix_required_subscription_reconciles_updated_at",
        "required_subscription_reconciles",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_required_subscription_reconciles_updated_at",
        "required_subscription_reconciles",
    )
    op.drop_index(
        "ix_required_subscription_reconciles_created_at",
        "required_subscription_reconciles",
    )
    op.drop_index(
        "ix_required_subscription_reconciles_next_attempt_at",
        "required_subscription_reconciles",
    )
    op.drop_index(
        "ix_required_subscription_reconciles_status",
        "required_subscription_reconciles",
    )
    op.drop_index(
        "ix_required_subscription_reconciles_group_id",
        "required_subscription_reconciles",
    )
    op.drop_table("required_subscription_reconciles")
