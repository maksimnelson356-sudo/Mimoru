"""drop campaign spam feature, retune repeats default

Revision ID: 0054_drop_campaign_spam
Revises: 0053_default_reason_without_obscenity
"""

import sqlalchemy as sa

from alembic import op


revision = "0054_drop_campaign_spam"
down_revision = "0053_default_reason_without_obscenity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("group_settings", "campaign_spam_enabled")
    op.drop_column("group_settings", "campaign_spam_limit")
    op.drop_column("group_settings", "campaign_spam_window_seconds")
    op.drop_column("group_settings", "campaign_spam_mute_seconds")
    # New default: a single user is muted after 5 identical messages.
    op.alter_column("group_settings", "repeats_limit", server_default="5")
    op.execute(
        sa.text("UPDATE group_settings SET repeats_limit = 5 WHERE repeats_limit = 3")
    )


def downgrade() -> None:
    op.add_column("group_settings", sa.Column("campaign_spam_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("group_settings", sa.Column("campaign_spam_limit", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("group_settings", sa.Column("campaign_spam_window_seconds", sa.Integer(), nullable=False, server_default="120"))
    op.add_column("group_settings", sa.Column("campaign_spam_mute_seconds", sa.Integer(), nullable=False, server_default="3600"))
    op.alter_column("group_settings", "repeats_limit", server_default="3")
