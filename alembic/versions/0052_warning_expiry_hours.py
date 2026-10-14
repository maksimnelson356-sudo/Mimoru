"""warning expiry window in hours

Days rounded every warning up to the next calendar midnight boundary: a
warning issued at 23:50 lived almost a full day longer than a 24h window, and
"через сутки" could not be expressed at all — the shortest selectable option
was 1 day, stored as an integer count of days.

The window moves to hours so 24h is exact. Existing values are converted with
warning_expire_days * 24, which keeps every owner's chosen duration: 0 stays
0 (never expire), 7 becomes 168, 30 becomes 720, 90 becomes 2160.

Revision ID: 0052_warning_expiry_hours
Revises: 0051_warning_expiry_one_day
Create Date: 2026-10-14
"""

import sqlalchemy as sa

from alembic import op

revision = "0052_warning_expiry_hours"
down_revision = "0051_warning_expiry_one_day"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_settings",
        sa.Column(
            "warning_expire_hours",
            sa.Integer(),
            nullable=False,
            server_default="24",
        ),
    )
    # Данные переносятся до удаления старой колонки: иначе значение выбора
    # владельца потерялось бы. 0 остаётся 0 — это «не удалять».
    op.execute(
        sa.text(
            "UPDATE group_settings SET warning_expire_hours = warning_expire_days * 24"
        )
    )
    op.drop_column("group_settings", "warning_expire_days")


def downgrade() -> None:
    op.add_column(
        "group_settings",
        sa.Column(
            "warning_expire_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    # Обратное преобразование округляет вниз: окно в часах короче суток при
    # обратном пересчёте всё равно не представимо в днях, а завышать срок
    # наказания молча нельзя.
    op.execute(
        sa.text(
            "UPDATE group_settings "
            "SET warning_expire_days = warning_expire_hours / 24"
        )
    )
    op.drop_column("group_settings", "warning_expire_hours")