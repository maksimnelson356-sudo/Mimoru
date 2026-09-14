from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128))
    service_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    owner_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    plan_code: Mapped[str] = mapped_column(String(32), default="trial")
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    settings: Mapped["GroupSettings"] = relationship(
        back_populates="group", cascade="all, delete-orphan", lazy="selectin"
    )


class GroupSettings(Base):
    __tablename__ = "group_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), unique=True
    )
    antiflood_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    links_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    welcome_text: Mapped[str] = mapped_column(Text, default="Добро пожаловать, {имя}!")
    warnings_limit: Mapped[int] = mapped_column(Integer, default=3)
    default_mute_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    antiflood_limit: Mapped[int] = mapped_column(Integer, default=6)
    antiflood_window_seconds: Mapped[int] = mapped_column(Integer, default=10)
    antiflood_mute_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    repeats_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    repeats_limit: Mapped[int] = mapped_column(Integer, default=3)
    caps_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    caps_percent: Mapped[int] = mapped_column(Integer, default=70)
    caps_min_length: Mapped[int] = mapped_column(Integer, default=15)
    voices_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    stickers_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    forwards_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    rules_text: Mapped[str] = mapped_column(Text, default="Правила пока не настроены.")
    reports_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    report_hour_utc: Mapped[int] = mapped_column(Integer, default=8)
    timezone_name: Mapped[str] = mapped_column(String(64), default="Europe/Warsaw")
    last_report_date: Mapped[str | None] = mapped_column(String(10))
    anti_raid_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    anti_raid_limit: Mapped[int] = mapped_column(Integer, default=10)
    anti_raid_window_seconds: Mapped[int] = mapped_column(Integer, default=60)
    warning_expire_days: Mapped[int] = mapped_column(Integer, default=30)
    lockdown_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    lockdown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lockdown_previous_permissions: Mapped[dict | None] = mapped_column(JSON)
    audit_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    audit_topic_id: Mapped[int | None] = mapped_column(Integer)
    night_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    night_mode_start: Mapped[str] = mapped_column(String(5), default="23:00")
    night_mode_end: Mapped[str] = mapped_column(String(5), default="07:00")
    night_mode_active: Mapped[bool] = mapped_column(Boolean, default=False)
    night_mode_previous_permissions: Mapped[dict | None] = mapped_column(JSON)
    join_requests_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    join_requests_auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    newcomer_quarantine_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    newcomer_quarantine_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    newcomer_quarantine_block_links: Mapped[bool] = mapped_column(Boolean, default=True)
    newcomer_quarantine_block_media: Mapped[bool] = mapped_column(Boolean, default=True)
    newcomer_quarantine_block_forwards: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    slow_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    slow_mode_seconds: Mapped[int] = mapped_column(Integer, default=10)
    campaign_spam_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    campaign_spam_limit: Mapped[int] = mapped_column(Integer, default=3)
    campaign_spam_window_seconds: Mapped[int] = mapped_column(Integer, default=120)
    campaign_spam_mute_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    edit_protection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    edit_protection_window_seconds: Mapped[int] = mapped_column(Integer, default=172800)
    mention_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mention_limit: Mapped[int] = mapped_column(Integer, default=5)
    hashtag_limit: Mapped[int] = mapped_column(Integer, default=10)
    mention_mute_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    sender_chat_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_group_sender_identity: Mapped[bool] = mapped_column(Boolean, default=True)
    moderation_reasons_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_cleanup_schedule: Mapped[str] = mapped_column(String(16), default="off")
    deleted_cleanup_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    group: Mapped[Group] = relationship(back_populates="settings")


class AutomationLog(Base):
    __tablename__ = "automation_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    rule_code: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "user_telegram_id", name="uq_group_members_group_user"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    is_present: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_deleted_account: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    reputation_override: Mapped[int | None] = mapped_column(Integer)
    trust_status: Mapped[str | None] = mapped_column(String(32), index=True)


class MemberTag(Base):
    __tablename__ = "member_tags"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_member_tags_group_name"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(48))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemberTagAssignment(Base):
    __tablename__ = "member_tag_assignments"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "user_telegram_id", "tag_id", name="uq_member_tag_assignment"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("member_tags.id", ondelete="CASCADE"), index=True
    )
    assigned_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserProfileHistory(Base):
    __tablename__ = "user_profile_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Warning(Base):
    __tablename__ = "warnings"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_telegram_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text, default="Не указана")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Punishment(Base):
    __tablename__ = "punishments"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_telegram_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="Не указана")
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModerationLog(Base):
    __tablename__ = "moderation_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    delivery_error: Mapped[str | None] = mapped_column(Text)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)


class ModerationReason(Base):
    __tablename__ = "moderation_reasons"
    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_moderation_reasons_group_name"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    actions: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ForbiddenWord(Base):
    __tablename__ = "forbidden_words"
    __table_args__ = (UniqueConstraint("group_id", "word"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    word: Mapped[str] = mapped_column(String(255))


class AllowedLink(Base):
    __tablename__ = "allowed_links"
    __table_args__ = (UniqueConstraint("group_id", "domain"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(253))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TrustedUser(Base):
    __tablename__ = "trusted_users"
    __table_args__ = (UniqueConstraint("group_id", "user_telegram_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    added_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AllowedSenderChat(Base):
    __tablename__ = "allowed_sender_chats"
    __table_args__ = (UniqueConstraint("group_id", "sender_chat_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    sender_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    added_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RequiredChannel(Base):
    __tablename__ = "required_channels"
    __table_args__ = (UniqueConstraint("group_id", "channel_username"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    channel_username: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class GroupModerator(Base):
    __tablename__ = "group_moderators"
    __table_args__ = (UniqueConstraint("group_id", "user_telegram_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(32), default="moderator")
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    assigned_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AutoResponse(Base):
    __tablename__ = "auto_responses"
    __table_args__ = (UniqueConstraint("group_id", "trigger"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(255))
    response_text: Mapped[str] = mapped_column(Text)
    match_type: Mapped[str] = mapped_column(String(32), default="contains")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Complaint(Base):
    __tablename__ = "complaints"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    reporter_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reviewed_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    resolution: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ComplaintNotification(Base):
    __tablename__ = "complaint_notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    complaint_id: Mapped[int] = mapped_column(
        ForeignKey("complaints.id", ondelete="CASCADE"),
        index=True,
    )
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class UserMessage(Base):
    __tablename__ = "user_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"),
        index=True,
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("group_id", "user_telegram_id", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    messages_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0)


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="telegram_stars")
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="XTR")
    plan_code: Mapped[str] = mapped_column(String(32))
    duration_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModeratorNote(Base):
    __tablename__ = "moderator_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ScheduledMessage(Base):
    __tablename__ = "scheduled_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    creator_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    sent_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recurrence: Mapped[str] = mapped_column(String(32), default="once")
    recurrence_weekday: Mapped[int | None] = mapped_column(Integer)
    recurrence_time: Mapped[str | None] = mapped_column(String(5))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    plan_code: Mapped[str] = mapped_column(String(32), default="standard")
    bonus_days: Mapped[int] = mapped_column(Integer, default=7)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    current_uses: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PromoCodeUse(Base):
    __tablename__ = "promo_code_uses"
    __table_args__ = (UniqueConstraint("promo_code_id", "user_telegram_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NewMemberRecord(Base):
    __tablename__ = "new_member_records"
    __table_args__ = (UniqueConstraint("group_id", "user_telegram_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    source: Mapped[str] = mapped_column(String(32), default="join")


class InviteCampaign(Base):
    __tablename__ = "invite_campaigns"
    __table_args__ = (UniqueConstraint("group_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    invite_link: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    creates_join_request: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    joined_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JoinRequestRecord(Base):
    __tablename__ = "join_request_records"
    __table_args__ = (UniqueConstraint("group_id", "user_telegram_id", "requested_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("invite_campaigns.id", ondelete="SET NULL"), index=True
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    bio: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)


class AdPlacement(Base):
    __tablename__ = "ad_placements"
    __table_args__ = (UniqueConstraint("group_id", name="uq_ad_placements_group"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    price_stars: Mapped[int] = mapped_column(Integer, default=100)
    format_text: Mapped[str] = mapped_column(
        String(255), default="1 публикация на 24 часа"
    )
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    duration_hours: Mapped[int] = mapped_column(Integer, default=24)
    member_count_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    avg_daily_messages_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    avg_daily_active_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    stats_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdOrder(Base):
    __tablename__ = "ad_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    placement_id: Mapped[int] = mapped_column(
        ForeignKey("ad_placements.id", ondelete="CASCADE"), index=True
    )
    buyer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    seller_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ad_text: Mapped[str] = mapped_column(Text)
    desired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    price_stars: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    published_message_id: Mapped[int | None] = mapped_column(BigInteger)
    payment_charge_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationEvent(Base):
    __tablename__ = "operation_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    target_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class GroupConfigSnapshot(Base):
    __tablename__ = "group_config_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), default="Резервная копия")
    payload: Mapped[dict] = mapped_column(JSON)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class GroupSubscriptionEvent(Base):
    __tablename__ = "group_subscription_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
