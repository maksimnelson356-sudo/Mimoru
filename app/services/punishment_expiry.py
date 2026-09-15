from __future__ import annotations

from datetime import datetime, timezone

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions
from redis.asyncio import Redis
from sqlalchemy import select

from app.db.models import Group, Punishment
from app.db.session import SessionFactory
from app.services.ui import is_chat_unavailable

UNMUTED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


async def expire_punishments(bot: Bot, redis: Redis) -> None:
    """Expire timed punishments without releasing a newer permission owner.

    Live moderation and CAPTCHA admission serialize on the Group row. The expiry
    worker uses the same boundary and rechecks both durable owners before any
    Telegram release.
    """
    log = structlog.get_logger()
    now = datetime.now(timezone.utc)

    async with SessionFactory() as session:
        candidates = list(
            (
                await session.execute(
                    select(Punishment.id, Punishment.group_id).where(
                        Punishment.active.is_(True),
                        Punishment.ends_at.is_not(None),
                        Punishment.ends_at <= now,
                    )
                )
            ).all()
        )

    for punishment_id, group_id in candidates:
        async with SessionFactory() as session:
            group = await session.scalar(
                select(Group).where(Group.id == group_id).with_for_update()
            )
            punishment = await session.scalar(
                select(Punishment)
                .where(Punishment.id == punishment_id)
                .with_for_update()
            )
            if (
                punishment is None
                or not punishment.active
                or punishment.ends_at is None
                or punishment.ends_at > now
            ):
                continue

            if group is None or not group.is_active:
                punishment.active = False
                await session.commit()
                continue

            punishment.active = False
            await session.flush()

            another_active = await session.scalar(
                select(Punishment.id)
                .where(
                    Punishment.group_id == punishment.group_id,
                    Punishment.user_telegram_id == punishment.user_telegram_id,
                    Punishment.kind == punishment.kind,
                    Punishment.active.is_(True),
                    Punishment.id != punishment.id,
                )
                .limit(1)
            )
            if another_active is not None:
                await session.commit()
                continue

            try:
                if punishment.kind == "mute":
                    captcha_key = (
                        f"captcha:{group.telegram_chat_id}:"
                        f"{punishment.user_telegram_id}"
                    )
                    if await redis.get(captcha_key) is not None:
                        await session.commit()
                        continue
                    await bot.restrict_chat_member(
                        group.telegram_chat_id,
                        punishment.user_telegram_id,
                        permissions=UNMUTED,
                    )
                elif punishment.kind == "ban":
                    await bot.unban_chat_member(
                        group.telegram_chat_id,
                        punishment.user_telegram_id,
                        only_if_banned=True,
                    )
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                await session.rollback()
                if is_chat_unavailable(error):
                    log.debug(
                        "punishment_expiry_failed",
                        punishment_id=punishment_id,
                        error=str(error),
                    )
                else:
                    log.warning(
                        "punishment_expiry_failed",
                        punishment_id=punishment_id,
                        error=str(error),
                    )
                continue

            await session.commit()
