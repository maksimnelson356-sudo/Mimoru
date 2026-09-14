from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Punishment
from app.db.rank_models import RankAssignment
from app.handlers import group_commands, member_center, reason_admin
from app.services.access import can_moderate, is_service_owner
from app.services.moderation import execute
from app.services.moderation_operations import (
    create_moderation_intent,
    drop_moderation_intent,
)
from app.services.moderation_reasons import normalize_actions
from app.services.ranks import ADMIN_RANKS, can_moderate_target
from app.services.repositories import active_warnings_count

router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}


async def _locked_active_group_by_id(
    session: AsyncSession, group_id: int
) -> Group | None:
    return await session.scalar(
        select(Group)
        .where(Group.id == group_id, Group.is_active.is_(True))
        .with_for_update()
    )


async def _locked_active_group_by_chat(
    session: AsyncSession, chat_id: int
) -> Group | None:
    return await session.scalar(
        select(Group)
        .where(Group.telegram_chat_id == chat_id, Group.is_active.is_(True))
        .with_for_update()
    )


async def _target_rank_state(
    session: AsyncSession,
    *,
    group_id: int,
    target_id: int,
) -> tuple[bool, bool]:
    assignment = await session.scalar(
        select(RankAssignment).where(
            RankAssignment.group_id == group_id,
            RankAssignment.user_telegram_id == target_id,
            RankAssignment.active.is_(True),
        )
    )
    if assignment is None or assignment.rank_code not in ADMIN_RANKS:
        return False, False
    return True, bool(assignment.telegram_admin_managed)


async def _has_active_mute(
    session: AsyncSession, *, group_id: int, target_id: int
) -> bool:
    existing = await session.scalar(
        select(Punishment.id)
        .where(
            Punishment.group_id == group_id,
            Punishment.user_telegram_id == target_id,
            Punishment.kind == "mute",
            Punishment.active.is_(True),
        )
        .limit(1)
    )
    return existing is not None


def _status_value(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value or "")


def _until_iso(member: object) -> str | None:
    value = getattr(member, "until_date", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    return None


async def _telegram_snapshot(bot: Bot, group: Group, target_id: int) -> dict | None:
    try:
        member = await bot.get_chat_member(group.telegram_chat_id, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    status = getattr(member, "status", None)
    muted = (
        status == ChatMemberStatus.RESTRICTED
        and getattr(member, "can_send_messages", True) is False
    )
    return {
        "pre_status": _status_value(status),
        "pre_banned": status == ChatMemberStatus.KICKED,
        "pre_muted": muted,
        "pre_until": _until_iso(member),
    }


async def _authorized_for_action(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target_id: int,
    action: str,
) -> bool:
    if not await can_moderate(bot, session, group, actor_id, action):
        return False
    allowed, _ = await can_moderate_target(session, group, actor_id, target_id)
    return allowed


async def _create_guard_intent(
    session: AsyncSession,
    *,
    group: Group,
    target_id: int,
    actor_id: int,
    action: str,
    source: str,
    reason: str,
    telegram_snapshot: dict,
    duration: int | None = None,
    warnings_limit: int | None = None,
    default_mute: int | None = None,
) -> int | None:
    now = datetime.now(UTC)
    admin_rank, managed_admin = await _target_rank_state(
        session,
        group_id=group.id,
        target_id=target_id,
    )
    payload: dict = {
        "chat_id": group.telegram_chat_id,
        "reason": reason,
        "duration": duration,
        "target_admin_rank": admin_rank,
        "target_managed_admin": managed_admin,
        **telegram_snapshot,
    }
    if action == "mute":
        seconds = int(duration or default_mute or group.settings.default_mute_seconds)
        payload["duration"] = seconds
        payload["ends_at"] = (now + timedelta(seconds=seconds)).isoformat()
        payload["had_active_mute"] = await _has_active_mute(
            session,
            group_id=group.id,
            target_id=target_id,
        )
    elif action == "ban" and duration:
        payload["ends_at"] = (now + timedelta(seconds=int(duration))).isoformat()
    elif action == "warn":
        limit = int(warnings_limit or group.settings.warnings_limit)
        mute_seconds = int(default_mute or group.settings.default_mute_seconds)
        current = await active_warnings_count(session, group.id, target_id)
        payload["warnings_before"] = current
        payload["warnings_limit"] = limit
        payload["default_mute"] = mute_seconds
        payload["had_active_mute"] = await _has_active_mute(
            session,
            group_id=group.id,
            target_id=target_id,
        )
        payload["expect_auto_mute"] = current + 1 >= limit and not admin_rank
        if payload["expect_auto_mute"]:
            payload["ends_at"] = (now + timedelta(seconds=mute_seconds)).isoformat()

    return await create_moderation_intent(
        session,
        group_id=group.id,
        target_id=target_id,
        actor_id=actor_id,
        action=action,
        source=source,
        payload=payload,
    )


async def _pending_message(message: Message) -> None:
    await message.reply(
        "Для этого участника уже выполняется действие модерации. Повторите позже."
    )


async def _snapshot_failed_message(message: Message) -> None:
    await message.reply(
        "Telegram не позволил проверить текущее состояние участника. Повторите действие позже."
    )


@router.callback_query(F.data.regexp(r"^modreason:[0-9a-f]{10}:\d+$"))
async def durable_reason_action(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
) -> None:
    _, token, raw_reason_id = callback.data.split(":")
    raw = await redis.get(f"mimoru:modpending:{token}")
    if not raw:
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return
    data = json.loads(raw)
    action = str(data.get("action") or "")
    if action not in {"ban", "mute", "warn"}:
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return
    if int(data.get("moderator_id", 0)) != callback.from_user.id:
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return

    origin = data.get("origin", "group")
    if origin == "group" and callback.message.chat.id != int(data["chat_id"]):
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return
    if origin == "panel" and callback.message.chat.type != "private":
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return

    group = await _locked_active_group_by_id(session, int(data.get("group_id", 0)))
    if group is None:
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return
    target_id = int(data["target_id"])
    if not await _authorized_for_action(
        bot, session, group, callback.from_user.id, target_id, action
    ):
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return
    reason = await reason_admin.get_reason(session, group.id, int(raw_reason_id))
    if (
        reason is None
        or not reason.active
        or action not in normalize_actions(reason.actions)
    ):
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return

    snapshot = await _telegram_snapshot(bot, group, target_id)
    if snapshot is None:
        await callback.answer(
            "Telegram не позволил проверить текущее состояние участника. Повторите позже.",
            show_alert=True,
        )
        return
    admin_rank, _ = await _target_rank_state(
        session, group_id=group.id, target_id=target_id
    )
    if (
        action in {"ban", "mute", "warn"}
        and snapshot["pre_status"]
        in {
            _status_value(ChatMemberStatus.CREATOR),
            _status_value(ChatMemberStatus.ADMINISTRATOR),
        }
        and not admin_rank
    ):
        await reason_admin.moderation_reason_selected(callback, bot, session, redis)
        return

    # Для бана — показать диалог подтверждения (вместо немедленного execute)
    if action == "ban":
        if callback.message is None:
            await callback.answer("Сообщение недоступно.", show_alert=True)
            return
        target_name = str(data.get("target_name", target_id))
        text = (
            "🛑 Подтверждение бана\n\n"
            f"👤 Нарушитель: {target_name}\n\n"
            "Выберите обычный бан или бан с очисткой сохранённых "
            "сообщений пользователя.\n\n"
            "⚠ Очистка необратима. Telegram удалит не все сообщения:\n"
            "• сообщения старше 48 часов остаются\n"
            "• сообщения пользователя, который уже был забанен ранее, "
            "могут остаться"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔴 Бан",
                        callback_data=f"modreason:confirm:{token}:{raw_reason_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔴🗑 Бан + очистка",
                        callback_data=f"modreason:clean:{token}:{raw_reason_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"modreason:cancel:{token}:{raw_reason_id}",
                    )
                ],
            ]
        )
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            import structlog

            structlog.get_logger().warning("modban_prompt_edit_failed", error=str(exc))
        await callback.answer()
        return

    intent_id = await _create_guard_intent(
        session,
        group=group,
        target_id=target_id,
        actor_id=callback.from_user.id,
        action=action,
        source="reason_callback",
        reason=reason.name,
        telegram_snapshot=snapshot,
        duration=data.get("duration"),
        warnings_limit=int(data.get("warnings_limit") or group.settings.warnings_limit),
        default_mute=int(
            data.get("default_mute") or group.settings.default_mute_seconds
        ),
    )
    if intent_id is None:
        await callback.answer(
            "Для этого участника уже выполняется действие модерации. Повторите позже.",
            show_alert=True,
        )
        return

    await reason_admin.moderation_reason_selected(callback, bot, session, redis)
    await drop_moderation_intent(session, intent_id)


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.reply_to_message,
    F.text.casefold() == "говори",
)
async def durable_reply_unmute(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is None or message.from_user is None:
        return
    group = await _locked_active_group_by_chat(session, message.chat.id)
    if group is None or not await _authorized_for_action(
        bot, session, group, message.from_user.id, target.id, "unmute"
    ):
        await group_commands.unmute_combined(message, bot, session)
        return
    snapshot = await _telegram_snapshot(bot, group, target.id)
    if snapshot is None:
        await _snapshot_failed_message(message)
        return
    intent_id = await _create_guard_intent(
        session,
        group=group,
        target_id=target.id,
        actor_id=message.from_user.id,
        action="unmute",
        source="reply",
        reason="",
        telegram_snapshot=snapshot,
    )
    if intent_id is None:
        await _pending_message(message)
        return
    await group_commands._do_unmute(message, bot, session, target_id=target.id)
    await drop_moderation_intent(session, intent_id)


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.text.casefold().startswith("разбан "),
)
async def durable_unban_by_username(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    if message.from_user is None:
        return
    group = await _locked_active_group_by_chat(session, message.chat.id)
    if group is None or not await can_moderate(
        bot, session, group, message.from_user.id, "unban"
    ):
        await group_commands.unban_combined(message, bot, session)
        return
    target_id, _ = await group_commands.resolve_target_user(
        session,
        message.chat.id,
        message,
        command_keyword="разбан",
    )
    if target_id is None:
        await group_commands.unban_combined(message, bot, session)
        return
    allowed, _ = await can_moderate_target(
        session, group, message.from_user.id, target_id
    )
    if not allowed:
        await group_commands.unban_combined(message, bot, session)
        return
    snapshot = await _telegram_snapshot(bot, group, target_id)
    if snapshot is None:
        await _snapshot_failed_message(message)
        return
    intent_id = await _create_guard_intent(
        session,
        group=group,
        target_id=target_id,
        actor_id=message.from_user.id,
        action="unban",
        source="reply_username",
        reason="",
        telegram_snapshot=snapshot,
    )
    if intent_id is None:
        await _pending_message(message)
        return
    await group_commands._do_unban(message, bot, session, target_id=target_id)
    await drop_moderation_intent(session, intent_id)


@router.callback_query(F.data.regexp(r"^member_action:\d+:-?\d+:(unmute|unban)$"))
async def durable_member_release(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
) -> None:
    _, raw_group, raw_user, action = callback.data.split(":")
    group = await _locked_active_group_by_id(session, int(raw_group))
    if group is None or (
        callback.from_user.id != group.owner_telegram_id
        and not is_service_owner(callback.from_user.id)
    ):
        await member_center.member_action(callback, bot, session)
        return
    target_id = int(raw_user)
    snapshot = await _telegram_snapshot(bot, group, target_id)
    if snapshot is None:
        await callback.answer(
            "Telegram не позволил проверить текущее состояние участника. Повторите позже.",
            show_alert=True,
        )
        return
    intent_id = await _create_guard_intent(
        session,
        group=group,
        target_id=target_id,
        actor_id=callback.from_user.id,
        action=action,
        source="member_panel",
        reason="Снято из панели",
        telegram_snapshot=snapshot,
    )
    if intent_id is None:
        await callback.answer(
            "Для этого участника уже выполняется действие модерации. Повторите позже.",
            show_alert=True,
        )
        return
    await member_center.member_action(callback, bot, session)
    await drop_moderation_intent(session, intent_id)


@router.callback_query(F.data.regexp(r"^modreason:confirm:[0-9a-f]{10}:\d+$"))
async def durable_ban_confirm(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
) -> None:
    await _durable_ban_execute(callback, bot, session, redis, cleanup=False)


@router.callback_query(F.data.regexp(r"^modreason:clean:[0-9a-f]{10}:\d+$"))
async def durable_ban_clean(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
) -> None:
    await _durable_ban_execute(callback, bot, session, redis, cleanup=True)


@router.callback_query(F.data.regexp(r"^modreason:cancel:[0-9a-f]{10}:\d+$"))
async def durable_ban_cancel(callback: CallbackQuery, redis: Redis) -> None:
    _, token, _ = callback.data.split(":")
    await redis.delete(f"mimoru:modpending:{token}")
    if callback.message is not None:
        try:
            await callback.message.delete()
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            import structlog

            structlog.get_logger().warning(
                "modban_cancel_delete_failed", error=str(exc)
            )
    await callback.answer("Отменено.")


async def _durable_ban_execute(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
    *,
    cleanup: bool,
) -> None:
    import json

    import structlog

    log = structlog.get_logger()

    _, _, token, raw_reason_id = callback.data.split(":")
    raw = await redis.get(f"mimoru:modpending:{token}")
    if not raw:
        await callback.answer("Действие уже выполнено или устарело.", show_alert=True)
        return
    data = json.loads(raw)

    if int(data.get("moderator_id", 0)) != callback.from_user.id:
        await callback.answer(
            "Только автор команды может подтвердить.", show_alert=True
        )
        return

    group = await _locked_active_group_by_id(session, int(data.get("group_id", 0)))
    if group is None:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return
    target_id = int(data["target_id"])
    if not await _authorized_for_action(
        bot, session, group, callback.from_user.id, target_id, "ban"
    ):
        await callback.answer("Право на действие больше недоступно.", show_alert=True)
        return

    snapshot = await _telegram_snapshot(bot, group, target_id)
    if snapshot is None:
        await callback.answer(
            "Telegram не позволил проверить состояние участника.",
            show_alert=True,
        )
        return

    deleted = await redis.delete(f"mimoru:modpending:{token}")
    if not deleted:
        await callback.answer("Действие уже выполнено.", show_alert=True)
        return

    reason = await reason_admin.get_reason(session, group.id, int(raw_reason_id))
    if reason is None or not reason.active:
        await callback.answer("Причина больше не активна.", show_alert=True)
        return

    intent_id = await _create_guard_intent(
        session,
        group=group,
        target_id=target_id,
        actor_id=callback.from_user.id,
        action="ban",
        source="reason_callback_confirm",
        reason=reason.name,
        telegram_snapshot=snapshot,
        duration=data.get("duration"),
        warnings_limit=int(data.get("warnings_limit") or group.settings.warnings_limit),
        default_mute=int(
            data.get("default_mute") or group.settings.default_mute_seconds
        ),
    )
    if intent_id is None:
        await callback.answer(
            "Для этого участника уже выполняется действие.", show_alert=True
        )
        return

    try:
        result = await execute(
            bot=bot,
            session=session,
            chat_id=group.telegram_chat_id,
            group_id=group.id,
            target_id=target_id,
            moderator_id=callback.from_user.id,
            action="ban",
            duration=data.get("duration"),
            reason=reason.name,
            warnings_limit=int(
                data.get("warnings_limit") or group.settings.warnings_limit
            ),
            default_mute=int(
                data.get("default_mute") or group.settings.default_mute_seconds
            ),
            target_name=str(data.get("target_name", target_id)),
            moderator_name=str(data.get("moderator_name", callback.from_user.id)),
            actor_role=data.get("actor_role", "admin"),
        )
        if result.commit:
            await session.commit()

        if cleanup:
            try:
                await bot.unban_chat_member(
                    chat_id=group.telegram_chat_id,
                    user_id=target_id,
                    only_if_banned=True,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                log.warning("modban_unban_failed", error=str(exc))
            try:
                await bot.ban_chat_member(
                    chat_id=group.telegram_chat_id,
                    user_id=target_id,
                    revoke_messages=True,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                log.warning("modban_clean_failed", error=str(exc))

        await drop_moderation_intent(session, intent_id)

        if result.public_notice:
            try:
                await bot.send_message(group.telegram_chat_id, str(result))
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                log.warning("modban_notify_failed", error=str(exc))

        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        await callback.answer(
            "Забанен." if result.success else (str(result) or "Не удалось забанить."),
            show_alert=not result.success,
        )
    except Exception as exc:
        log.exception("modban_execute_failed", error=str(exc))
        await callback.answer("Ошибка выполнения бана.", show_alert=True)
