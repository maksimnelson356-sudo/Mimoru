from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberUpdated, Message
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.ad_market_models import DirectRequiredRule
from app.db.models import Group, GroupMember, RequiredChannel, TrustedUser
from app.handlers.members import RESTRICTED, is_subscribed, required_channels, verification_keyboard
from app.services.owner_management import managed_group_for_message
from app.services.permissions import is_admin
from app.services.plans import plan_limit
from app.services.public_identity import public_user_token
from app.services.required_resources import normalize_public_telegram_resource

log = structlog.get_logger()


router = Router(name=__name__)


class ActivationError(Exception):
    """Raised when marketplace-driven activation cannot proceed."""


async def restrict_existing_unsubscribed_members(
    bot: Bot,
    redis: Redis,
    *,
    group_id: int,
    telegram_chat_id: int,
    channels: list[str],
) -> None:
    """Restrict existing group members who are not subscribed to required channels.

    Runs as a fire-and-forget background task after OP activation.
    Skips bots, admins/creators, trusted users, and already-subscribed members.
    """
    from app.db.session import SessionFactory

    settings = get_settings()
    async with SessionFactory() as session:
        members = list((await session.scalars(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.is_present.is_(True),
                GroupMember.is_deleted_account.is_(False),
            )
        )).all())

    restricted_count = 0
    for member in members:
        user_id = member.user_telegram_id
        try:
            async with SessionFactory() as session:
                trusted = await session.scalar(select(TrustedUser.id).where(
                    TrustedUser.group_id == group_id,
                    TrustedUser.user_telegram_id == user_id,
                ))
                if trusted is not None:
                    continue
            try:
                if await is_admin(bot, telegram_chat_id, user_id):
                    continue
            except (TelegramBadRequest, TelegramForbiddenError):
                continue
            subscribed, _error = await is_subscribed(bot, user_id, channels)
            if subscribed:
                continue
            try:
                await bot.restrict_chat_member(telegram_chat_id, user_id, permissions=RESTRICTED)
            except (TelegramBadRequest, TelegramForbiddenError):
                continue
            deadline = datetime.now(timezone.utc) + timedelta(seconds=settings.captcha_timeout_seconds)
            await redis.set(
                f"captcha:{telegram_chat_id}:{user_id}",
                str(int(deadline.timestamp())),
                ex=settings.captcha_timeout_seconds + 120,
            )
            text = f"{public_user_token(user_id)}, подтвердите подписку на обязательные каналы."
            await bot.send_message(
                telegram_chat_id,
                text,
                reply_markup=await verification_keyboard(telegram_chat_id, user_id, channels, bot=bot),
            )
            restricted_count += 1
            await asyncio.sleep(0.05)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
    if restricted_count:
        log.info(
            "existing_members_restricted_for_op",
            group_id=group_id,
            count=restricted_count,
        )


async def activate_deal_subscription(
    session: AsyncSession,
    *,
    group_id: int,
    channel: str,
    min_days: int,
    created_by: int,
    dedupe_key: str | None = None,
) -> None:
    """Create or update RequiredChannel + DirectRequiredRule for a marketplace deal.

    Raises ActivationError with a user-facing message on failure.
    """
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise ActivationError("Группа недоступна.")

    current = await session.scalar(select(RequiredChannel).where(
        RequiredChannel.group_id == group_id,
        RequiredChannel.channel_username == channel,
    ))
    if current is None:
        active_count = int(await session.scalar(
            select(func.count()).select_from(RequiredChannel).where(
                RequiredChannel.group_id == group_id,
                RequiredChannel.active.is_(True),
            )
        ) or 0)
        limit = plan_limit(group, "channels")
        if active_count >= limit:
            raise ActivationError(
                f"В группе уже включено максимально допустимое количество "
                f"обязательных подписок — {limit}."
            )
        current = RequiredChannel(group_id=group_id, channel_username=channel, active=True)
        session.add(current)
    else:
        current.active = True

    expires_at = datetime.now(timezone.utc) + timedelta(days=min_days)
    rule = await session.scalar(select(DirectRequiredRule).where(
        DirectRequiredRule.group_id == group_id,
        DirectRequiredRule.channel_username == channel,
    ))
    if rule is None:
        rule = DirectRequiredRule(
            group_id=group_id,
            channel_username=channel,
            mode="days",
            limit_value=min_days,
            used_count=0,
            expires_at=expires_at,
            active=True,
            created_by_telegram_id=created_by,
        )
        session.add(rule)
    else:
        rule.mode = "days"
        rule.limit_value = min_days
        rule.used_count = 0
        rule.expires_at = expires_at
        rule.active = True
        rule.created_by_telegram_id = created_by

    if dedupe_key is not None:
        from app.services.required_reconciles import enqueue_required_reconcile

        await enqueue_required_reconcile(
            session,
            group_id=group_id,
            telegram_chat_id=group.telegram_chat_id,
            channel_username=channel,
            dedupe_key=dedupe_key,
        )


def _parse_limit(raw: str) -> tuple[str, int] | None:
    value = " ".join(raw.casefold().strip().split())
    day_match = re.fullmatch(r"(\d+)\s*(?:д|дн|день|дня|дней)", value)
    if day_match:
        amount = int(day_match.group(1))
        return ("days", amount) if 1 <= amount <= 3650 else None
    member_match = re.fullmatch(r"(\d+)\s*(?:уч|участник|участника|участников)", value)
    if member_match:
        amount = int(member_match.group(1))
        return ("members", amount) if 1 <= amount <= 1_000_000 else None
    return None


async def _managed_group(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    return await managed_group_for_message(
        message,
        bot,
        session,
        denial_text="Изменять обязательную подписку может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text.regexp(r"(?i)^подключить\s+\S+\s+.+$"),
)
async def direct_required_connect(message: Message, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    if not message.from_user or not message.text:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        return
    channel = normalize_public_telegram_resource(parts[1])
    limit = _parse_limit(parts[2])
    if channel is None:
        await message.reply("Укажите публичный @username или ссылку вида https://t.me/username.")
        return
    if limit is None:
        await message.reply(
            "После ссылки укажите срок или количество участников. Например: "
            "подключить @channel 7 дней или подключить @channel 100 участников."
        )
        return
    group = await _managed_group(message, bot, session, for_update=True)
    if group is None:
        return

    current = await session.scalar(select(RequiredChannel).where(
        RequiredChannel.group_id == group.id,
        RequiredChannel.channel_username == channel,
    ))
    if current is None:
        active_count = int(await session.scalar(
            select(func.count()).select_from(RequiredChannel).where(
                RequiredChannel.group_id == group.id,
                RequiredChannel.active.is_(True),
            )
        ) or 0)
        if active_count >= plan_limit(group, "channels"):
            limit = plan_limit(group, "channels")
            await message.reply(f"В группе уже включено максимально допустимое количество обязательных подписок — {limit}.")
            return
        current = RequiredChannel(group_id=group.id, channel_username=channel, active=True)
        session.add(current)
    else:
        current.active = True

    mode, amount = limit
    rule = await session.scalar(select(DirectRequiredRule).where(
        DirectRequiredRule.group_id == group.id,
        DirectRequiredRule.channel_username == channel,
    ))
    expires_at = datetime.now(timezone.utc) + timedelta(days=amount) if mode == "days" else None
    if rule is None:
        rule = DirectRequiredRule(
            group_id=group.id,
            channel_username=channel,
            mode=mode,
            limit_value=amount,
            used_count=0,
            expires_at=expires_at,
            active=True,
            created_by_telegram_id=message.from_user.id,
        )
        session.add(rule)
    else:
        rule.mode = mode
        rule.limit_value = amount
        rule.used_count = 0
        rule.expires_at = expires_at
        rule.active = True
        rule.created_by_telegram_id = message.from_user.id
    await session.flush()
    if mode == "days":
        from app.services.required_reconciles import enqueue_required_reconcile

        await enqueue_required_reconcile(
            session,
            group_id=group.id,
            telegram_chat_id=group.telegram_chat_id,
            channel_username=channel,
            dedupe_key=f"direct:{rule.id}:{message.message_id}",
        )
    await session.commit()

    if mode == "days":
        await message.reply(
            f"✅ Обязательная подписка на {channel} включена на {amount} дн. "
            "Подтверждение от другого пользователя не требуется."
        )
    else:
        await message.reply(
            f"✅ Обязательная подписка на {channel} включена для следующих {amount} новых обычных участников группы. "
            "Администраторы и боты не учитываются."
        )


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text.regexp(r"(?i)^отключить\s+\S+$"),
)
async def direct_required_disconnect(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user or not message.text:
        return
    channel = normalize_public_telegram_resource(message.text.split(maxsplit=1)[1])
    if channel is None:
        await message.reply("Укажите публичный @username или ссылку вида https://t.me/username.")
        return
    group = await _managed_group(message, bot, session, for_update=True)
    if group is None:
        return
    item = await session.scalar(select(RequiredChannel).where(
        RequiredChannel.group_id == group.id,
        RequiredChannel.channel_username == channel,
    ))
    rule = await session.scalar(select(DirectRequiredRule).where(
        DirectRequiredRule.group_id == group.id,
        DirectRequiredRule.channel_username == channel,
    ))
    if item is None and rule is None:
        await message.reply("Такая обязательная подписка в этой группе не включена.")
        return
    if item is not None:
        item.active = False
    if rule is not None:
        rule.active = False
    await session.commit()
    await message.reply(f"✅ Обязательная подписка на {channel} отключена.")


@router.chat_member()
async def count_direct_required_members(event: ChatMemberUpdated, session: AsyncSession) -> None:
    if event.old_chat_member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return
    if event.new_chat_member.status in {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }:
        return
    if event.new_chat_member.user.is_bot:
        return
    group = await session.scalar(
        select(Group)
        .where(
            Group.telegram_chat_id == event.chat.id,
            Group.is_active.is_(True),
        )
        .with_for_update()
    )
    if group is None:
        return
    rules = list((await session.scalars(
        select(DirectRequiredRule)
        .where(
            DirectRequiredRule.group_id == group.id,
            DirectRequiredRule.active.is_(True),
            DirectRequiredRule.mode == "members",
        )
        .with_for_update()
    )).all())
    changed = False
    for rule in rules:
        rule.used_count += 1
        changed = True
        if rule.used_count >= rule.limit_value:
            rule.active = False
            channel = await session.scalar(select(RequiredChannel).where(
                RequiredChannel.group_id == group.id,
                RequiredChannel.channel_username == rule.channel_username,
            ))
            if channel is not None:
                channel.active = False
    if changed:
        await session.commit()
