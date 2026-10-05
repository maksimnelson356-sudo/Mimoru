"""add_composite_indexes_for_hot_queries"""
from alembic import op
import sqlalchemy as sa

revision = '0c6bfdcd804f'
down_revision = 'aae25103371f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # rank_assignments: WHERE group_id=? AND user_telegram_id=? AND active=True
    op.create_index(
        "ix_rank_assignments_group_user_active",
        "rank_assignments",
        ["group_id", "user_telegram_id", "active"],
        unique=False,
    )

    # punishments: WHERE group_id=? AND user_telegram_id=? AND kind=? AND active=True
    op.create_index(
        "ix_punishments_group_user_kind_active",
        "punishments",
        ["group_id", "user_telegram_id", "kind", "active"],
        unique=False,
    )

    # warnings: WHERE group_id=? AND user_telegram_id=? AND active=True ORDER BY created_at DESC
    op.create_index(
        "ix_warnings_group_user_active_created",
        "warnings",
        ["group_id", "user_telegram_id", "active", "created_at"],
        unique=False,
    )

    # group_members: WHERE group_id=? AND is_present=True AND is_deleted_account=False
    op.create_index(
        "ix_group_members_group_present_not_deleted",
        "group_members",
        ["group_id", "is_present", "is_deleted_account"],
        unique=False,
    )

    # global_post_requests: WHERE buyer_telegram_id=? AND status IN ('paid','completed')
    op.create_index(
        "ix_global_post_requests_buyer_status",
        "global_post_requests",
        ["buyer_telegram_id", "status"],
        unique=False,
    )

    # moderation_logs: WHERE group_id=? AND target_telegram_id=? ORDER BY created_at DESC LIMIT N
    op.create_index(
        "ix_moderation_logs_group_target_created",
        "moderation_logs",
        ["group_id", "target_telegram_id", "created_at"],
        unique=False,
    )

    # payments: additional index for refund recovery queries
    op.create_index(
        "ix_payments_status_user_created",
        "payments",
        ["status", "user_telegram_id", "created_at"],
        unique=False,
    )

    # daily_stats: common aggregation queries
    op.create_index(
        "ix_daily_stats_group_date",
        "daily_stats",
        ["group_id", "date"],
        unique=False,
    )

    # complaints: common lookup by group + reporter/target
    op.create_index(
        "ix_complaints_group_reporter_created",
        "complaints",
        ["group_id", "reporter_telegram_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_complaints_group_target_created",
        "complaints",
        ["group_id", "target_telegram_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_complaints_group_target_created", table_name="complaints")
    op.drop_index("ix_complaints_group_reporter_created", table_name="complaints")
    op.drop_index("ix_daily_stats_group_date", table_name="daily_stats")
    op.drop_index("ix_payments_status_user_created", table_name="payments")
    op.drop_index("ix_moderation_logs_group_target_created", table_name="moderation_logs")
    op.drop_index("ix_global_post_requests_buyer_status", table_name="global_post_requests")
    op.drop_index("ix_group_members_group_present_not_deleted", table_name="group_members")
    op.drop_index("ix_warnings_group_user_active_created", table_name="warnings")
    op.drop_index("ix_punishments_group_user_kind_active", table_name="punishments")
    op.drop_index("ix_rank_assignments_group_user_active", table_name="rank_assignments")
