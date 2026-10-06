from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RequiredAdListing(Base):
    """Public OP marketplace listing created by a managed group owner."""

    __tablename__ = "required_ad_listings"
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), unique=True, index=True)
    seller_owner_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    member_count_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    min_days: Mapped[int] = mapped_column(Integer, default=1)
    price_unit: Mapped[str] = mapped_column(String(16), default="day")
    price_text: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RequiredAdDealRequest(Base):
    """A buyer request sent to one specific OP listing and its owner."""

    __tablename__ = "required_ad_deal_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("required_ad_listings.id", ondelete="CASCADE"), index=True)
    buyer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    seller_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_resource: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class GlobalPostRequest(Base):
    """Network-wide ad post reviewed by a Mimoru service owner before payment."""

    __tablename__ = "global_post_requests"
    __table_args__ = (
        Index(
            "ix_global_post_requests_buyer_status",
            "buyer_telegram_id",
            "status",
        ),
        # One Telegram Stars charge must be recorded once. Partial, so requests
        # that have no charge yet stay unlimited.
        Index(
            "uq_global_post_requests_payment_charge_id_not_null",
            "payment_charge_id",
            unique=True,
            postgresql_where=text("payment_charge_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str | None] = mapped_column(String(512))
    button_text: Mapped[str | None] = mapped_column(String(64))
    button_url: Mapped[str | None] = mapped_column(String(2048))
    price_stars: Mapped[int] = mapped_column(Integer)
    payment_charge_id: Mapped[str | None] = mapped_column(String(255))
    reviewed_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GlobalPostDelivery(Base):
    __tablename__ = "global_post_deliveries"
    __table_args__ = (UniqueConstraint("request_id", "group_id", name="uq_global_post_request_group"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("global_post_requests.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="sent", index=True)
    error_text: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DirectRequiredRule(Base):
    """Owner-created OP rule configured directly from a managed group chat."""

    __tablename__ = "direct_required_rules"
    __table_args__ = (UniqueConstraint("group_id", "channel_username", name="uq_direct_required_group_channel"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    channel_username: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    limit_value: Mapped[int] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
