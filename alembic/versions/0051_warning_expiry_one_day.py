"""default warning expiry to one day

Warnings accumulated for 30 days by default and the settings menu offered no
shorter option, so a user warned twice a month apart could reach the mute
threshold without any recent offence. The storage unit is days, so a 24h
window is 1.

Groups that were left on the previous 30-day default are moved to 1 day.
An explicit owner choice (any other value, including 0 = never expire) is
preserved untouched.

Revision ID: 0051_warning_expiry_one_day
Revises: 0050_admin_custom_title
Create Date: 2026-10-14
"""

import sqlalchemy as sa

from alembic import op

revision = "0051_warning_expiry_one_day"
down_revision = "0050_admin_custom_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE group_settings SET warning_expire_days = 1 "
            "WHERE warning_expire_days = 30"
        )
    )


def downgrade() -> None:
    # Only the untouched previous default is restored; an owner-chosen value
    # cannot be reconstructed and must not be overwritten.
    op.execute(
        sa.text(
            "UPDATE group_settings SET warning_expire_days = 30 "
            "WHERE warning_expire_days = 1"
        )
    )