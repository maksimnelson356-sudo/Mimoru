"""add the default reason for obscene content

Revision ID: 0053_default_reason_without_obscenity
Revises: 0052_warning_expiry_hours
"""

import sqlalchemy as sa

from alembic import op


revision = "0053_default_reason_without_obscenity"
down_revision = "0052_warning_expiry_hours"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the new built-in reason to existing groups without duplicating a
    # custom reason that already uses the same name.
    op.execute(
        sa.text(
            "INSERT INTO moderation_reasons "
            "(group_id, name, actions, active, sort_order) "
            "SELECT groups.id, :name, CAST(:actions AS json), TRUE, 55 "
            "FROM groups "
            "WHERE NOT EXISTS ("
            "    SELECT 1 FROM moderation_reasons "
            "    WHERE moderation_reasons.group_id = groups.id "
            "      AND moderation_reasons.name = :name"
            ")"
        ).bindparams(
            name="Без пошлостей",
            actions='["warn", "mute"]',
        )
    )


def downgrade() -> None:
    # Keep user data intact if an owner edited or reused the built-in reason
    # name after the migration. The next upgrade remains idempotent.
    pass
