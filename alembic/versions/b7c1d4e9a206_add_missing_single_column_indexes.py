"""add_missing_single_column_indexes

The ORM declares `index=True` on these columns, but no migration ever created
the index, so the database never had them. `scripts/check_live_schema.py` reads
the live catalog and reports every one of them as missing. They were left as
warnings rather than errors because a missing index costs query performance
rather than correctness, which is why the drift survived unnoticed this long.
"""
from alembic import op

revision = 'b7c1d4e9a206'
down_revision = '0c6bfdcd804f'
branch_labels = None
depends_on = None

# (table, column) pairs the ORM marks index=True.
MISSING_INDEXES = [
    ("complaints", "created_at"),
    ("complaints", "reporter_telegram_id"),
    ("complaints", "target_telegram_id"),
    ("daily_stats", "date"),
    ("daily_stats", "group_id"),
    ("daily_stats", "user_telegram_id"),
    ("forbidden_words", "group_id"),
    ("game_sessions", "creator_telegram_id"),
    ("moderation_logs", "delivered_at"),
    ("moderator_notes", "author_telegram_id"),
    ("punishments", "group_id"),
    ("punishments", "user_telegram_id"),
    ("required_channels", "group_id"),
    ("scheduled_messages", "creator_telegram_id"),
    ("support_tickets", "user_telegram_id"),
    ("warnings", "group_id"),
    ("warnings", "user_telegram_id"),
]


def upgrade() -> None:
    for table, column in MISSING_INDEXES:
        op.create_index(f"ix_{table}_{column}", table, [column], unique=False)


def downgrade() -> None:
    for table, column in reversed(MISSING_INDEXES):
        op.drop_index(f"ix_{table}_{column}", table_name=table)