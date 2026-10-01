from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.group_disconnect_models import GroupDisconnectIntent
from app.db.models import Group
from app.db.session import SessionFactory
from app.services.access import is_service_owner
from app.services.rank_departure import deactivate_all_ranks


ABSENT_STATUSES = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
SYSTEM_DISCONNECT_ACTOR_ID = 0


async def _persist_disconnect_intent(
    session: AsyncSession,
    group: Group,
    actor_telegram_id: int,
) -> GroupDisconnectIntent:
    intent = await session.get(GroupDisconnectIntent, group.id)
    if intent is None:
        intent = GroupDisconnectIntent(
            group_id=group.id,
            actor_telegram_id=actor_telegram_id,
            status="pending",
            error_text=None,
        )
        session.add(intent)
    else:
        intent.actor_telegram_id = actor_telegram_id
        intent.status = "pending"
        intent.error_text = None
    await session.commit()
    return intent


async def request_group_disconnect(
    session: AsyncSession,
    group: Group,
    actor_telegram_id: int,
) -> GroupDisconnectIntent:
    """Persist owner-authorized disconnect intent before any Telegram side effect."""
    return await _persist_disconnect_intent(session, group, actor_telegram_id)


async def request_system_group_disconnect(
    session: AsyncSession,
    group: Group,
) -> GroupDisconnectIntent:
    """Persist a Telegram-membership-driven disconnect independent of group ownership."""
    return await _persist_disconnect_intent(session, group, SYSTEM_DISCONNECT_ACTOR_ID)


async def _bot_membership_status(bot: Bot, chat_id: int) -> ChatMemberStatus | None:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    return member.status


async def _bot_is_absent(bot: Bot, chat_id: int) -> bool | None:
    status = await _bot_membership_status(bot, chat_id)
    return None if status is None else status in ABSENT_STATUSES


async def _finalize_disconnect(
    session: AsyncSession,
    group: Group,
    intent: GroupDisconnectIntent,
) -> bool:
    """Finalize while retaining the caller's Group row ownership lock."""
    group.is_active = False
    # Staff access must leave with the group: a later reconnect must not restore
    # the old team automatically — owners re-appoint people on purpose.
    await deactivate_all_ranks(session, group)
    await session.delete(intent)
    await session.commit()
    return True


async def attempt_group_disconnect(bot: Bot, group_id: int) -> bool:
    """Attempt one currently authorized or system-observed disconnect under Group lock."""
    log = structlog.get_logger()
    async with SessionFactory() as session:
        group = await session.scalar(
            select(Group).where(Group.id == group_id).with_for_update()
        )
        intent = await session.scalar(
            select(GroupDisconnectIntent)
            .where(GroupDisconnectIntent.group_id == group_id)
            .with_for_update()
        )
        if intent is None:
            return False
        if group is None:
            await session.delete(intent)
            await session.commit()
            return True

        chat_id = group.telegram_chat_id
        if intent.actor_telegram_id == SYSTEM_DISCONNECT_ACTOR_ID:
            # A system intent is created only after Telegram reports that Mimoru
            # lost administrator rights. Re-check before replaying the leave: the
            # group may have been repaired/reconnected while the process was down.
            status = await _bot_membership_status(bot, chat_id)
            if status == ChatMemberStatus.ADMINISTRATOR:
                await session.delete(intent)
                await session.commit()
                log.info("group_disconnect_system_intent_stale", group_id=group.id)
                return False
            if status in ABSENT_STATUSES:
                return await _finalize_disconnect(session, group, intent)
            if status is None:
                intent.status = "pending"
                intent.error_text = "cannot verify bot membership before system disconnect"
                await session.commit()
                return False
        elif (
            intent.actor_telegram_id != group.owner_telegram_id
            and not is_service_owner(intent.actor_telegram_id)
        ):
            await session.delete(intent)
            await session.commit()
            log.info(
                "group_disconnect_stale_actor_dropped",
                group_id=group.id,
                actor_id=intent.actor_telegram_id,
                owner_id=group.owner_telegram_id,
            )
            return False

        intent.status = "leaving"
        intent.error_text = None
        await session.flush()

        try:
            await bot.leave_chat(chat_id)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            absent = await _bot_is_absent(bot, chat_id)
            if absent is True:
                return await _finalize_disconnect(session, group, intent)
            intent.status = "pending"
            intent.error_text = str(error)[:1000]
            await session.commit()
            log.warning(
                "group_disconnect_leave_retryable",
                group_id=group_id,
                chat_id=chat_id,
                error=str(error),
            )
            return False

        return await _finalize_disconnect(session, group, intent)


async def recover_group_disconnects(bot: Bot) -> None:
    """Retry durable owner/system disconnect intents under current state checks."""
    async with SessionFactory() as session:
        ids = list((await session.scalars(
            select(GroupDisconnectIntent.group_id)
            .where(GroupDisconnectIntent.status.in_(["pending", "leaving"]))
            .order_by(GroupDisconnectIntent.group_id)
            .limit(100)
        )).all())
    for group_id in ids:
        try:
            await attempt_group_disconnect(bot, group_id)
        except Exception:
            structlog.get_logger().exception(
                "group_disconnect_recovery_failed",
                group_id=group_id,
            )
