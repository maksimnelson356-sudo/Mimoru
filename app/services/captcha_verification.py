from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.types import ChatPermissions
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Punishment
from app.services.captcha_state import (
    VERIFIED,
    delete_captcha_state,
    refresh_captcha_state_ttl,
)
from app.services.ui import is_chat_unavailable

UNRESTRICTED = ChatPermissions(
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

VERIFICATION_FINISHED = "finished"
VERIFICATION_MUTED = "muted"
VERIFICATION_RETRY = "retry"
VERIFICATION_UNAVAILABLE = "unavailable"


async def finalize_verified_captcha(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    *,
    key: str,
    chat_id: int,
    user_id: int,
) -> str:
    """Finish a VERIFIED CAPTCHA without overriding a concurrent moderation mute.

    Moderation serializes permission mutations on the Group row. Use the same
    boundary here and keep it through the Telegram unrestrict and Redis cleanup.
    The caller commits/rolls back the DB transaction to release the lock.
    """
    group = await session.scalar(
        select(Group).where(Group.telegram_chat_id == chat_id).with_for_update()
    )
    if group is None or not group.is_active:
        if await redis.get(key) == VERIFIED:
            await delete_captcha_state(redis, key, VERIFIED)
        return VERIFICATION_UNAVAILABLE

    if await redis.get(key) != VERIFIED:
        return VERIFICATION_FINISHED

    active_mute = await session.scalar(
        select(Punishment.id)
        .where(
            Punishment.group_id == group.id,
            Punishment.user_telegram_id == user_id,
            Punishment.kind == "mute",
            Punishment.active.is_(True),
        )
        .limit(1)
    )
    if active_mute is not None:
        await delete_captcha_state(redis, key, VERIFIED)
        return VERIFICATION_MUTED

    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=UNRESTRICTED)
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as error:
        logger = structlog.get_logger()
        if is_chat_unavailable(error):
            logger.debug(
                "captcha_verified_unrestrict_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(error),
            )
        else:
            logger.warning(
                "captcha_verified_unrestrict_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(error),
            )
        await refresh_captcha_state_ttl(redis, key, VERIFIED)
        return VERIFICATION_RETRY

    await delete_captcha_state(redis, key, VERIFIED)
    return VERIFICATION_FINISHED
