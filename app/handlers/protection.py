from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AllowedLink, AllowedSenderChat, AutoResponse, DailyStat, ForbiddenWord, Group, ModerationLog, NewMemberRecord, Punishment, TrustedUser, Warning
from app.services.campaign_spam import build_campaign_signature
from app.services.content import contains_blocked_link
from app.services.edit_protection import should_recheck_edit
from app.services.mentions import count_mentions_and_hashtags
from app.services.message_ttl import answer_group_notice
from app.services.permissions import is_admin
from app.services.quarantine import is_quarantine_active
from app.services.slow_mode import slow_mode_key
from app.services.repositories import active_warnings_count
from app.services.ui import automatic_action_notice
from app.utils.duration import human_duration

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
router.edited_message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
MUTED = ChatPermissions(can_send_messages=False)

VIOLATION_LABELS = {
    "slow_mode": "слишком частые сообщения",
    "quarantine_forward": "пересылки запрещены во время карантина",
    "quarantine_media": "медиа запрещены во время карантина",
    "quarantine_link": "ссылки запрещены во время карантина",
    "voice": "голосовые сообщения запрещены",
    "sticker": "стикеры запрещены",
    "forward": "пересылки запрещены",
    "repeat": "повторяющиеся сообщения",
    "caps": "слишком много заглавных букв",
    "link": "ссылки в этой группе запрещены",
    "word": "запрещённое слово",
}


def campaign_media_ids(message: Message) -> list[str]:
    ids: list[str] = []
    if message.photo:
        ids.append(message.photo[-1].file_unique_id)
    for item in (
        message.video, message.document, message.audio, message.voice,
        message.video_note, message.sticker, message.animation,
    ):
        if item is not None:
            ids.append(item.file_unique_id)
    return ids


async def apply_campaign_mute(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    group: Group,
    signature: str,
    participants: int,
    *,
    edited: bool = False,
) -> None:
    if not message.from_user:
        return
    seconds = group.settings.campaign_spam_mute_seconds
    ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning(
            "automatic_violation_delete_failed",
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            error=str(error),
        )
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            message.from_user.id,
            permissions=MUTED,
            until_date=int(ends_at.timestamp()),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning("campaign_spam_mute_failed", error=str(error))
        return
    await mark_message(
        session, group.id, message.from_user.id, deleted=True, count_message=not edited
    )
    session.add(Punishment(
        group_id=group.id,
        user_telegram_id=message.from_user.id,
        moderator_telegram_id=0,
        kind="mute",
        reason="Координированный массовый спам",
        ends_at=ends_at,
    ))
    session.add(ModerationLog(
        group_id=group.id,
        actor_telegram_id=0,
        target_telegram_id=message.from_user.id,
        action="campaign_spam_mute",
        reason="Одинаковый контент от нескольких пользователей",
        metadata_json={
            "duration_seconds": seconds,
            "message_id": message.message_id,
            "signature": signature,
            "participants": participants,
            "edited": edited,
        },
    ))
    if not edited:
        await answer_group_notice(
            message,
            automatic_action_notice(
                action="mute",
                target=message.from_user.full_name,
                reason="координированный массовый спам",
                duration_seconds=seconds,
            )
        )


async def mark_message(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    deleted: bool = False,
    count_message: bool = True,
) -> None:
    date = datetime.now(timezone.utc).date().isoformat()
    row = await session.scalar(
        select(DailyStat).where(
            DailyStat.group_id == group_id,
            DailyStat.user_telegram_id == user_id,
            DailyStat.date == date,
        )
    )
    if row is None:
        row = DailyStat(group_id=group_id, user_telegram_id=user_id, date=date)
        session.add(row)
    if count_message:
        row.messages_count += 1
    if deleted:
        row.deleted_count += 1


async def delete_violation(
    message: Message,
    session: AsyncSession,
    group: Group,
    reason: str,
    *,
    edited: bool = False,
    notify: bool = True,
) -> bool:
    if not message.from_user:
        return False
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning(
            "violation_delete_failed",
            chat_id=message.chat.id,
            message_id=message.message_id,
            error=str(error),
        )
        return False
    await mark_message(
        session,
        group.id,
        message.from_user.id,
        deleted=True,
        count_message=not edited,
    )
    label = VIOLATION_LABELS.get(reason, reason.replace("_", " "))
    session.add(
        ModerationLog(
            group_id=group.id,
            actor_telegram_id=0,
            target_telegram_id=message.from_user.id,
            action=f"filter_{reason}",
            reason=label,
            metadata_json={"message_id": message.message_id, "edited": edited},
        )
    )
    if notify and not edited:
        await answer_group_notice(
            message,
            automatic_action_notice(
                action="delete",
                target=message.from_user.full_name,
                reason=label,
            )
        )
    return True


async def apply_mention_mute(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    group: Group,
    mentions: int,
    hashtags: int,
    *,
    edited: bool = False,
) -> None:
    if not message.from_user:
        return
    seconds = group.settings.mention_mute_seconds
    ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning(
            "automatic_violation_delete_failed",
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            error=str(error),
        )
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            message.from_user.id,
            permissions=MUTED,
            until_date=int(ends_at.timestamp()),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning("mention_filter_mute_failed", error=str(error))
        return
    await mark_message(
        session, group.id, message.from_user.id, deleted=True, count_message=not edited
    )
    session.add(Punishment(
        group_id=group.id,
        user_telegram_id=message.from_user.id,
        moderator_telegram_id=0,
        kind="mute",
        reason="Массовые упоминания или хэштеги",
        ends_at=ends_at,
    ))
    session.add(ModerationLog(
        group_id=group.id,
        actor_telegram_id=0,
        target_telegram_id=message.from_user.id,
        action="mention_filter_mute",
        reason="Превышен лимит упоминаний или хэштегов",
        metadata_json={
            "duration_seconds": seconds,
            "message_id": message.message_id,
            "mentions": mentions,
            "hashtags": hashtags,
            "edited": edited,
        },
    ))
    if not edited:
        await answer_group_notice(
            message,
            automatic_action_notice(
                action="mute",
                target=message.from_user.full_name,
                reason="массовые упоминания или хэштеги",
                duration_seconds=seconds,
            )
        )


def message_marker_counts(message: Message, text: str) -> tuple[int, int]:
    mentions, hashtags = count_mentions_and_hashtags(text)
    entity_mentions = 0
    entity_hashtags = 0
    for entity in (message.entities or message.caption_entities or []):
        entity_type = str(entity.type)
        if entity_type in {"mention", "text_mention"}:
            entity_mentions += 1
        elif entity_type == "hashtag":
            entity_hashtags += 1
    return max(mentions, entity_mentions), max(hashtags, entity_hashtags)


async def apply_antiflood_mute(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    group: Group,
) -> None:
    if not message.from_user:
        return
    seconds = group.settings.antiflood_mute_seconds
    ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning(
            "automatic_violation_delete_failed",
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            error=str(error),
        )
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            message.from_user.id,
            permissions=MUTED,
            until_date=int(ends_at.timestamp()),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        structlog.get_logger().warning("antiflood_mute_failed", error=str(error))
        return
    await mark_message(session, group.id, message.from_user.id, deleted=True)
    session.add(
        Punishment(
            group_id=group.id,
            user_telegram_id=message.from_user.id,
            moderator_telegram_id=0,
            kind="mute",
            reason="Автоматический антифлуд",
            ends_at=ends_at,
        )
    )
    session.add(
        ModerationLog(
            group_id=group.id,
            actor_telegram_id=0,
            target_telegram_id=message.from_user.id,
            action="antiflood_mute",
            reason="Превышен лимит сообщений",
            metadata_json={"duration_seconds": seconds, "message_id": message.message_id},
        )
    )
    await answer_group_notice(
        message,
        automatic_action_notice(
            action="mute",
            target=message.from_user.full_name,
            reason="флуд",
            duration_seconds=seconds,
        )
    )


async def protect_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
    *,
    edited: bool = False,
) -> None:
    group = await session.scalar(select(Group).where(Group.telegram_chat_id == message.chat.id))
    if group is None or not group.is_active:
        return
    if message.sender_chat and group.settings.sender_chat_filter_enabled:
        is_own_group = message.sender_chat.id == message.chat.id
        allowed = await session.scalar(select(AllowedSenderChat.id).where(
            AllowedSenderChat.group_id == group.id,
            AllowedSenderChat.sender_chat_id == message.sender_chat.id,
        ))
        if not (is_own_group and group.settings.allow_group_sender_identity) and allowed is None:
            try:
                await message.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                return
            session.add(ModerationLog(group_id=group.id, actor_telegram_id=0, target_telegram_id=None, action="sender_chat_block", reason="Сообщение от неразрешённого канала", metadata_json={"sender_chat_id": message.sender_chat.id, "title": message.sender_chat.title, "message_id": message.message_id, "edited": edited}))
            await session.commit()
            return
    if not message.from_user or message.from_user.is_bot:
        return
    if await is_admin(bot, message.chat.id, message.from_user.id):
        if not edited:
            await mark_message(session, group.id, message.from_user.id)
            await session.commit()
        return
    trusted = await session.scalar(
        select(TrustedUser.id).where(
            TrustedUser.group_id == group.id,
            TrustedUser.user_telegram_id == message.from_user.id,
        )
    )
    if trusted is not None:
        if not edited:
            await mark_message(session, group.id, message.from_user.id)
            await session.commit()
        return

    settings = group.settings
    if edited:
        if not settings.edit_protection_enabled:
            return
        if not should_recheck_edit(
            message.date,
            message.edit_date,
            settings.edit_protection_window_seconds,
        ):
            return
    text = message.text or message.caption or ""

    if text and settings.mention_filter_enabled:
        mentions, hashtags = message_marker_counts(message, text)
        if mentions > settings.mention_limit or hashtags > settings.hashtag_limit:
            await apply_mention_mute(
                message, bot, session, group, mentions, hashtags, edited=edited
            )
            await session.commit()
            return

    if settings.campaign_spam_enabled:
        signature = build_campaign_signature(text, campaign_media_ids(message))
        if signature:
            key = f"campaign:{message.chat.id}:{signature}"
            await redis.sadd(key, str(message.from_user.id))
            await redis.expire(key, settings.campaign_spam_window_seconds)
            participants = await redis.scard(key)
            if participants >= settings.campaign_spam_limit:
                await apply_campaign_mute(
                    message, bot, session, group, signature, participants, edited=edited
                )
                await session.commit()
                return

    if not edited and settings.slow_mode_enabled:
        key = slow_mode_key(message.chat.id, message.from_user.id)
        accepted = await redis.set(key, "1", ex=settings.slow_mode_seconds, nx=True)
        if not accepted:
            await delete_violation(message, session, group, "slow_mode", edited=edited)
            await session.commit()
            return

    if settings.newcomer_quarantine_enabled:
        newcomer = await session.scalar(select(NewMemberRecord).where(
            NewMemberRecord.group_id == group.id,
            NewMemberRecord.user_telegram_id == message.from_user.id,
        ))
        if newcomer and is_quarantine_active(newcomer.joined_at, settings.newcomer_quarantine_seconds):
            media_present = any((
                message.photo, message.video, message.document, message.audio,
                message.voice, message.video_note, message.sticker, message.animation,
            ))
            if settings.newcomer_quarantine_block_forwards and message.forward_origin:
                await delete_violation(message, session, group, "quarantine_forward", edited=edited)
                await session.commit()
                return
            if settings.newcomer_quarantine_block_media and media_present:
                await delete_violation(message, session, group, "quarantine_media", edited=edited)
                await session.commit()
                return
            if settings.newcomer_quarantine_block_links and text and contains_blocked_link(text, set()):
                await delete_violation(message, session, group, "quarantine_link", edited=edited)
                await session.commit()
                return

    if message.voice and not settings.voices_allowed:
        await delete_violation(message, session, group, "voice", edited=edited)
        await session.commit()
        return
    if message.sticker and not settings.stickers_allowed:
        await delete_violation(message, session, group, "sticker", edited=edited)
        await session.commit()
        return
    if message.forward_origin and not settings.forwards_allowed:
        await delete_violation(message, session, group, "forward", edited=edited)
        await session.commit()
        return

    if not edited and settings.antiflood_enabled:
        key = f"flood:{message.chat.id}:{message.from_user.id}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, settings.antiflood_window_seconds)
        if count > settings.antiflood_limit:
            await apply_antiflood_mute(message, bot, session, group)
            await session.commit()
            return

    if not edited and text and settings.repeats_enabled:
        normalized = " ".join(text.casefold().split())
        key = f"repeat:{message.chat.id}:{message.from_user.id}:{normalized[:80]}"
        repeats = await redis.incr(key)
        if repeats == 1:
            await redis.expire(key, 60)
        if repeats > settings.repeats_limit:
            await delete_violation(message, session, group, "repeat", edited=edited)
            await session.commit()
            return

    if text and settings.caps_enabled and len(text) >= settings.caps_min_length:
        letters = [char for char in text if char.isalpha()]
        if letters and sum(char.isupper() for char in letters) * 100 / len(letters) >= settings.caps_percent:
            await delete_violation(message, session, group, "caps", edited=edited)
            await session.commit()
            return

    if text and not settings.links_enabled:
        allowed = set(
            (await session.scalars(select(AllowedLink.domain).where(AllowedLink.group_id == group.id))).all()
        )
        if contains_blocked_link(text, allowed):
            deleted = await delete_violation(message, session, group, "link", edited=edited, notify=False)
            if deleted and not edited:
                session.add(Warning(
                    group_id=group.id,
                    user_telegram_id=message.from_user.id,
                    moderator_telegram_id=0,
                    reason="Запрещённая ссылка",
                ))
                await session.flush()
                warning_count = await active_warnings_count(
                    session, group.id, message.from_user.id
                )
                session.add(ModerationLog(
                    group_id=group.id,
                    actor_telegram_id=0,
                    target_telegram_id=message.from_user.id,
                    action="auto_warn_link",
                    reason="Запрещённая ссылка",
                    metadata_json={"active_count": warning_count},
                ))
                if warning_count >= settings.warnings_limit:
                    seconds = settings.default_mute_seconds
                    ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                    try:
                        await bot.restrict_chat_member(
                            message.chat.id,
                            message.from_user.id,
                            permissions=MUTED,
                            until_date=int(ends_at.timestamp()),
                        )
                        session.add(Punishment(
                            group_id=group.id,
                            user_telegram_id=message.from_user.id,
                            moderator_telegram_id=0,
                            kind="mute",
                            reason="Лимит предупреждений",
                            ends_at=ends_at,
                        ))
                        await answer_group_notice(
                            message,
                            automatic_action_notice(
                                action="mute",
                                target=message.from_user.full_name,
                                reason="достигнут лимит предупреждений",
                                duration_seconds=seconds,
                            )
                        )
                    except (TelegramBadRequest, TelegramForbiddenError) as error:
                        structlog.get_logger().warning(
                            "link_auto_mute_failed", error=str(error)
                        )
                else:
                    await message.answer(
                        automatic_action_notice(
                            action="warn",
                            target=message.from_user.full_name,
                            reason="запрещённая ссылка",
                            warning_count=warning_count,
                            warning_limit=settings.warnings_limit,
                        )
                    )
            await session.commit()
            return

    if text:
        words = (await session.scalars(select(ForbiddenWord.word).where(ForbiddenWord.group_id == group.id))).all()
        lowered = text.casefold()
        if any(word.casefold() in lowered for word in words):
            await delete_violation(message, session, group, "word", edited=edited)
            await session.commit()
            return

        if edited:
            await session.commit()
            return

        responses = (await session.scalars(
            select(AutoResponse).where(AutoResponse.group_id == group.id, AutoResponse.active.is_(True))
        )).all()
        for item in responses:
            trigger = item.trigger.casefold()
            matched = lowered == trigger if item.match_type == "exact" else trigger in lowered
            if not matched:
                continue
            cooldown = f"response:{item.id}:{message.from_user.id}"
            if not await redis.get(cooldown):
                await redis.set(cooldown, "1", ex=60)
                await message.reply(item.response_text)
            break

    if not edited:
        await mark_message(session, group.id, message.from_user.id)
    await session.commit()


@router.message()
async def protect(message: Message, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    await protect_message(message, bot, session, redis, edited=False)


@router.edited_message()
async def protect_edited(message: Message, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    await protect_message(message, bot, session, redis, edited=True)
