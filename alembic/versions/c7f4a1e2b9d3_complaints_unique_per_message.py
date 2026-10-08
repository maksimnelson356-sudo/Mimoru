"""complaints unique per message

Revision ID: c7f4a1e2b9d3
Revises: b7c1d4e9a206
Create Date: 2026-10-08

Вводит запрет на более одной жалобы на одно сообщение: сообщение
идентифицируется парой (group_id, message_id), потому что message_id
уникален внутри чата.

Перед созданием ограничения из таблицы удаляются дубликаты, оставляя
самую раннюю жалобу (минимальный id) на каждое сообщение. Без этой
чистки CREATE UNIQUE INDEX упал бы на существующих данных.
Каскад ON DELETE CASCADE автоматически убирает связанные
complaint_notifications. Записи в moderation_log не трогаются —
это исторический журнал действий.
"""

import sqlalchemy as sa

from alembic import op

revision = "c7f4a1e2b9d3"
down_revision = "b7c1d4e9a206"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM complaints
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM complaints
                GROUP BY group_id, message_id
            )
            """
        )
    )
    op.create_unique_constraint(
        "uq_complaints_group_message",
        "complaints",
        ["group_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_complaints_group_message",
        "complaints",
        type_="unique",
    )
    # Удалённые дубликаты не восстанавливаются: операция необратима.