"""add_partial_unique_indexes_and_status_check"""
from alembic import op
import sqlalchemy as sa

revision = 'aae25103371f'
down_revision = '0049_required_subscription_reconciles'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial unique index on payments.provider_payment_id (only for non-NULL values)
    op.create_index(
        "uq_payments_provider_payment_id_not_null",
        "payments",
        ["provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )

    # Partial unique index on global_post_requests.payment_charge_id (only for non-NULL values)
    op.create_index(
        "uq_global_post_requests_payment_charge_id_not_null",
        "global_post_requests",
        ["payment_charge_id"],
        unique=True,
        postgresql_where=sa.text("payment_charge_id IS NOT NULL"),
    )

    # CHECK constraint on payments.status
    op.create_check_constraint(
        "ck_payments_status",
        "payments",
        "status IN ('pending', 'paid', 'refunded', 'refund_pending')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_payments_status", "payments", type_="check")
    op.drop_index("uq_global_post_requests_payment_charge_id_not_null", table_name="global_post_requests")
    op.drop_index("uq_payments_provider_payment_id_not_null", table_name="payments")
