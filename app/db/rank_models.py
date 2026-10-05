from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RankAssignment(Base):
    __tablename__ = "rank_assignments"
    __table_args__ = (UniqueConstraint("group_id", "user_telegram_id", name="uq_rank_assignment_group_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    rank_code: Mapped[str] = mapped_column(String(32), index=True)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    assigned_by_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    helper_for_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # telegram = the role is mirrored to Telegram administrator rights;
    # bot_only = the person manages the group only through Mimoru.
    access_mode: Mapped[str] = mapped_column(String(16), server_default="bot_only", index=True)
    telegram_admin_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    restore_after_mute: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GroupRankPolicy(Base):
    __tablename__ = "group_rank_policies"
    __table_args__ = (UniqueConstraint("group_id", "rank_code", name="uq_group_rank_policy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    rank_code: Mapped[str] = mapped_column(String(32), index=True)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RankAssignmentEvent(Base):
    __tablename__ = "rank_assignment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    old_rank_code: Mapped[str | None] = mapped_column(String(32))
    new_rank_code: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
