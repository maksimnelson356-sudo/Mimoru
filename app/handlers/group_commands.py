from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Complaint,
    ComplaintNotification,
    Group,
    GroupMember,
    User,
    Warning,
)
from app.db.rank_models import RankAssignment
from app.services.access import can_moderate
from app.services.action_panel import format_action_panel
from app.services.moderation import execute, log_action
from app.services.public_identity import public_user_token
from app.services.ranks import (
    CHAT_ADMIN,
    CHIEF_ADMIN,
    DEPUTY_OWNER,
    HELPER,
    can_moderate_target,
    get_assignment,
)
from app.utils.user_resolver import resolve_target_user

router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}
COMPLAINT_WORDS = {"жалоба", "доложить", "нарушитель"}
CLEAR_WARNING_WORDS = {
    "снять все предупреждения",
    "снять предупреждения",
    "обнулить предупреждения",
    "снять все преды",
}

log = structlog.get_logger(__name__)


async def _active_group(
    session: AsyncSession,
    chat_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    query = select(Group).where(
        Group.telegram_chat_id == chat_id,
        Group.is_active.is_(True),
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


def _target_from_reply(message: Message):
    if message.reply_to_message is None:
        return None
    return message.reply_to_message.from_user


def _message_link(group: Group, message_id: int) -> str | None:
    if message_id <= 0:
        return None
    chat_id = str(group.telegram_chat_id)
    if not chat_id.startswith("-100"):
        return None
    return f"https://t.me/c/{chat_id[4:]}/{message_id}"


async def _notify_complaint_recipients(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    reporter_id: int,
    reporter_name: str,
    target_id: int,
    target_name: str,
    message_id: int,
    message_text: str | None = None,
    complaint: Complaint | None = None,
) -> int:
    reporter_rank = await get_assignment(session, group.id, reporter_id)
    recipients: set[int] = set()
    if (
        reporter_rank is not None
        and reporter_rank.rank_code == HELPER
        and reporter_rank.helper_for_telegram_id
    ):
        recipients.add(reporter_rank.helper_for_telegram_id)
    else:
        if group.owner_telegram_id:
            recipients.add(group.owner_telegram_id)
        rows = (
            await session.scalars(
                select(RankAssignment.user_telegram_id).where(
                    RankAssignment.group_id == group.id,
                    RankAssignment.active.is_(True),
                    RankAssignment.rank_code.in_(
                        (DEPUTY_OWNER, CHIEF_ADMIN, CHAT_ADMIN)
                    ),
                )
            )
        ).all()
        recipients.update(int(value) for value in rows)

    recipients.discard(reporter_id)
    # Fallback: если после удаления репортера получателей не осталось,
    # вернуть владельца — иначе жалоба уйдёт в пустоту.
    if not recipients and group.owner_telegram_id:
        recipients.add(group.owner_telegram_id)

    delivered = 0
    fields = [
        ("👥", f"Чат: {group.title}"),
        ("🙋", f"Кто пожаловался: {public_user_token(reporter_id)}"),
        ("🎯", f"На кого: {public_user_token(target_id)}"),
        ("🔗", f"Объект: Сообщение №{message_id}"),
    ]
    if message_text:
        snippet = message_text.strip()
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."
        footer = f"⚠ Проверьте контекст перед выдачей наказания.\n> «{snippet}»"
    else:
        footer = "⚠ Проверьте контекст перед выдачей наказания."

    text = format_action_panel("complaint", fields=fields, footer=footer)
    link = _message_link(group, message_id)
    rows: list[list[InlineKeyboardButton]] = []

    # Кнопки действий (требуют complaint.id)
    if complaint is not None:
        cid = complaint.id
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Проверено", callback_data=f"complaint:ack:{cid}"
                ),
                InlineKeyboardButton(
                    text="⚠️ Выдать пред", callback_data=f"complaint:warn:{cid}"
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🚫 Забанить", callback_data=f"complaint:ban:{cid}"
                ),
                InlineKeyboardButton(
                    text="🤐 Наказать отправителя",
                    callback_data=f"complaint:mute_reporter:{cid}",
                ),
            ]
        )

    # Кнопка "Открыть сообщение" (если есть ссылка)
    if link:
        rows.append([InlineKeyboardButton(text="Открыть сообщение", url=link)])

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    for recipient in recipients:
        try:
            sent = await bot.send_message(recipient, text, reply_markup=keyboard)
            delivered += 1
            if complaint is not None:
                session.add(
                    ComplaintNotification(
                        complaint_id=complaint.id,
                        admin_telegram_id=recipient,
                        message_id=sent.message_id,
                    )
                )
        except (TelegramBadRequest, TelegramForbiddenError):
            continue

    if complaint is not None and delivered > 0:
        await session.commit()

    if delivered == 0:
        log.warning(
            "complaint_no_recipients",
            group_id=group.id,
            telegram_chat_id=group.telegram_chat_id,
            reporter_id=reporter_id,
            target_id=target_id,
            recipients_count=len(recipients),
        )

    return delivered


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.reply_to_message,
    F.text.casefold().in_(COMPLAINT_WORDS),
)
async def group_complaint(message: Message, bot: Bot, session: AsyncSession) -> None:
    target = _target_from_reply(message)
    if target is None or message.from_user is None:
        return
    if target.id == message.from_user.id:
        await message.reply("Нельзя отправить жалобу на самого себя.")
        return
    group = await _active_group(session, message.chat.id)
    if group is None:
        return

    existing = await session.scalar(
        select(Complaint.id).where(
            Complaint.group_id == group.id,
            Complaint.reporter_telegram_id == message.from_user.id,
            Complaint.message_id == message.reply_to_message.message_id,
            Complaint.status == "pending",
        )
    )
    if existing is not None:
        await message.reply("Эта жалоба уже отправлена и ожидает проверки.")
        return

    complaint = Complaint(
        group_id=group.id,
        reporter_telegram_id=message.from_user.id,
        target_telegram_id=target.id,
        message_id=message.reply_to_message.message_id,
        message_text=(
            message.reply_to_message.text or message.reply_to_message.caption or ""
        )[:4000]
        or None,
        status="pending",
    )
    session.add(complaint)
    await session.flush()

    delivered = await _notify_complaint_recipients(
        bot,
        session,
        group,
        message.from_user.id,
        message.from_user.full_name or str(message.from_user.id),
        target.id,
        target.full_name or str(target.id),
        message.reply_to_message.message_id,
        complaint.message_text,
        complaint,
    )
    await session.commit()
    if delivered:
        await message.reply(
            "✅ Жалоба принята. Администраторы группы получили уведомление."
        )
    else:
        await message.reply(
            "✅ Жалоба сохранена.\n\n"
            "⚠️ В группе нет администраторов Mimoru, кому можно передать жалобу.\n"
            "Владелец группы может назначить их в личке бота."
        )


async def _do_unmute(
    message: Message, bot: Bot, session: AsyncSession, *, target_id: int
) -> None:
    group = await _active_group(session, message.chat.id)
    if group is None:
        return
    if not await can_moderate(bot, session, group, message.from_user.id, "unmute"):
        await message.reply("У вас нет права размутить пользователя.")
        return
    notice = await execute(
        bot=bot,
        session=session,
        chat_id=message.chat.id,
        group_id=group.id,
        target_id=target_id,
        moderator_id=message.from_user.id,
        action="unmute",
        duration=None,
        reason="",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=public_user_token(target_id),
        moderator_name=public_user_token(message.from_user.id),
    )
    await session.commit()
    await message.reply(notice)


async def _do_unban(
    message: Message, bot: Bot, session: AsyncSession, *, target_id: int
) -> None:
    group = await _active_group(session, message.chat.id)
    if group is None:
        return
    if not await can_moderate(bot, session, group, message.from_user.id, "unban"):
        await message.reply("У вас нет права разбанить пользователя.")
        return
    notice = await execute(
        bot=bot,
        session=session,
        chat_id=message.chat.id,
        group_id=group.id,
        target_id=target_id,
        moderator_id=message.from_user.id,
        action="unban",
        duration=None,
        reason="",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=public_user_token(target_id),
        moderator_name=public_user_token(message.from_user.id),
    )
    await session.commit()
    await message.reply(notice)


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.text.casefold().in_({"говори", "размут", "размутить", "снять мут"}),
)
async def unmute_combined(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    target_id, _ = await resolve_target_user(
        session,
        message.chat.id,
        message,
        command_keyword="говори",
    )
    if target_id is None:
        if message.reply_to_message is None:
            await message.reply(
                "Укажите пользователя: ответьте на его сообщение или напишите "
                "<code>размут @username</code> / <code>размут 123456</code>."
            )
        return
    await _do_unmute(message, bot, session, target_id=target_id)


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.reply_to_message,
    F.text.casefold().in_(CLEAR_WARNING_WORDS),
)
async def clear_all_warnings(message: Message, bot: Bot, session: AsyncSession) -> None:
    target = _target_from_reply(message)
    if target is None or message.from_user is None:
        return
    group = await _active_group(session, message.chat.id, for_update=True)
    if group is None:
        return
    if not await can_moderate(bot, session, group, message.from_user.id, "unwarn"):
        await message.reply("У вас нет права снимать предупреждения.")
        return
    allowed, reason = await can_moderate_target(
        session, group, message.from_user.id, target.id
    )
    if not allowed:
        await message.reply(reason)
        return

    rows = list(
        (
            await session.scalars(
                select(Warning).where(
                    Warning.group_id == group.id,
                    Warning.user_telegram_id == target.id,
                    Warning.active.is_(True),
                )
            )
        ).all()
    )
    if not rows:
        await message.reply("У этого участника нет активных предупреждений.")
        return
    for row in rows:
        row.active = False
    log_action(
        session,
        group.id,
        message.from_user.id,
        target.id,
        "unwarn_all",
        "Сняты все предупреждения",
        {"count": len(rows)},
    )
    await session.commit()
    await message.reply(f"✅ Сняты все активные предупреждения: {len(rows)}.")


async def _resolve_group_user(
    session: AsyncSession, group_id: int, raw: str
) -> tuple[int | None, str]:
    value = raw.strip()
    if value.isdigit():
        target_id = int(value)
        known = await session.scalar(
            select(GroupMember.id).where(
                GroupMember.group_id == group_id,
                GroupMember.user_telegram_id == target_id,
            )
        )
        return (
            (target_id, public_user_token(target_id))
            if known is not None
            else (None, value)
        )
    if not value.startswith("@") or len(value) < 2:
        return None, value
    username = value[1:].casefold()
    row = await session.execute(
        select(User.telegram_id, User.username)
        .join(GroupMember, GroupMember.user_telegram_id == User.telegram_id)
        .where(
            GroupMember.group_id == group_id,
            func.lower(User.username) == username,
        )
        .limit(1)
    )
    found = row.first()
    if found is None:
        return None, value
    target_id = int(found.telegram_id)
    return target_id, public_user_token(target_id)


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.text.casefold().in_({"разбан", "разбанить", "снять бан"}),
)
async def unban_combined(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    group = await _active_group(session, message.chat.id)
    if group is None:
        return
    if not await can_moderate(bot, session, group, message.from_user.id, "unban"):
        await message.reply("У вас нет права разбанить пользователя.")
        return

    target_id, target_label = await resolve_target_user(
        session,
        message.chat.id,
        message,
        command_keyword="разбан",
    )
    if target_id is None:
        if message.reply_to_message is None:
            await message.reply(
                "Укажите пользователя: ответьте на его сообщение или напишите "
                "<code>разбан @username</code> / <code>разбан 123456</code>."
            )
        return

    notice = await execute(
        bot=bot,
        session=session,
        chat_id=message.chat.id,
        group_id=group.id,
        target_id=target_id,
        moderator_id=message.from_user.id,
        action="unban",
        duration=None,
        reason="",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=target_label,
        moderator_name=public_user_token(message.from_user.id),
    )
    await session.commit()
    await message.reply(notice)
