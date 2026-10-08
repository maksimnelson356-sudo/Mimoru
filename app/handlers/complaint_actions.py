from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Complaint, ComplaintNotification, Group, UserMessage
from app.services.access import can_moderate
from app.services.message_ttl import (
    COMPLAINT_MESSAGE_TTL_SECONDS,
    send_group_notice,
)
from app.services.moderation import execute
from app.services.public_identity import public_user_token
from app.services.rank_access import get_actor_rank_with_access
from app.services.ranks import can_moderate_target

log = structlog.get_logger(__name__)

router = Router(name=__name__)


async def _clear_complaint_buttons(
    bot: Bot,
    session: AsyncSession,
    complaint_id: int,
) -> None:
    """Снимает inline-кнопки со всех уведомлений о жалобе."""
    notifications = (
        await session.scalars(
            select(ComplaintNotification).where(
                ComplaintNotification.complaint_id == complaint_id,
            )
        )
    ).all()
    for n in notifications:
        try:
            await bot.edit_message_reply_markup(
                chat_id=n.admin_telegram_id,
                message_id=n.message_id,
                reply_markup=None,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "complaint_notification_edit_failed",
                admin_telegram_id=n.admin_telegram_id,
                message_id=n.message_id,
                error=str(exc),
            )


async def _delete_user_messages(
    bot: Bot,
    session: AsyncSession,
    group_id: int,
    user_telegram_id: int,
    chat_id: int,
) -> int:
    """Удаляет сообщения пользователя через deleteMessages.
    Только сообщения < 47 часов (Telegram не даёт удалять старые).
    Возвращает количество удалённых.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=47)
    rows = (
        await session.scalars(
            select(UserMessage.message_id).where(
                UserMessage.group_id == group_id,
                UserMessage.user_telegram_id == user_telegram_id,
                UserMessage.created_at > cutoff,
            )
        )
    ).all()

    deleted = 0
    for i in range(0, len(rows), 100):
        batch = list(rows[i : i + 100])
        try:
            await bot.delete_messages(
                chat_id=chat_id,
                message_ids=batch,
            )
            deleted += len(batch)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "delete_messages_batch_failed",
                group_id=group_id,
                user_telegram_id=user_telegram_id,
                batch_size=len(batch),
                error=str(exc),
            )
    return deleted


async def _get_pending(
    session: AsyncSession,
    complaint_id: int,
    *,
    for_update: bool = False,
) -> Complaint | None:
    query = select(Complaint).where(
        Complaint.id == complaint_id,
        Complaint.status == "pending",
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def _reject_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта жалоба уже обработана.", show_alert=True)


_COMPLAINT_REVIEWER_CODES = frozenset(
    {"service_owner", "owner", "deputy_owner", "chief_admin", "chat_admin"}
)


async def _is_notification_recipient(
    session: AsyncSession,
    callback: CallbackQuery,
    complaint_id: int,
) -> bool:
    if callback.message is None or callback.message.chat.id != callback.from_user.id:
        return False
    notification_id = await session.scalar(
        select(ComplaintNotification.id).where(
            ComplaintNotification.complaint_id == complaint_id,
            ComplaintNotification.admin_telegram_id == callback.from_user.id,
            ComplaintNotification.message_id == callback.message.message_id,
        )
    )
    return notification_id is not None


async def _complaint_reviewer_allowed(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    user_id: int,
) -> bool:
    actor = await get_actor_rank_with_access(bot, session, group, user_id)
    return actor is not None and actor.code in _COMPLAINT_REVIEWER_CODES


async def _authorize_complaint_action(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    complaint: Complaint,
    user_id: int,
    action: str,
) -> bool:
    if not await _complaint_reviewer_allowed(bot, session, group, user_id):
        return False
    if not await can_moderate(bot, session, group, user_id, action):
        return False
    allowed, _ = await can_moderate_target(
        session,
        group,
        user_id,
        complaint.target_telegram_id,
    )
    return allowed


async def _load_complaint_context(
    bot: Bot,
    session: AsyncSession,
    callback: CallbackQuery,
    complaint_id: int,
    *,
    action: str | None,
    for_update: bool = False,
) -> tuple[Complaint, Group] | None:
    complaint = await _get_pending(session, complaint_id, for_update=for_update)
    if complaint is None:
        await _reject_stale(callback)
        return None
    group = await session.get(Group, complaint.group_id)
    if group is None or not group.is_active:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return None
    if not await _is_notification_recipient(session, callback, complaint_id):
        await callback.answer("Эта жалоба больше недоступна.", show_alert=True)
        return None
    if not await _complaint_reviewer_allowed(
        bot, session, group, callback.from_user.id
    ):
        await callback.answer("Нет доступа.", show_alert=True)
        return None
    if action is not None and not await _authorize_complaint_action(
        bot, session, group, complaint, callback.from_user.id, action
    ):
        await callback.answer("Нет доступа к этому действию.", show_alert=True)
        return None
    return complaint, group


@router.callback_query(F.data.regexp(r"^complaint:ack:\d+$"))
async def complaint_ack(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    context = await _load_complaint_context(
        bot,
        session,
        callback,
        cid,
        action=None,
        for_update=True,
    )
    if context is None:
        return
    complaint, group = context
    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "ack"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    try:
        await send_group_notice(
            bot,
            group.telegram_chat_id,
            f"✅ Жалоба обработана.\nМодератор: {actor}",
            delay_seconds=COMPLAINT_MESSAGE_TTL_SECONDS,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "complaint_notify_failed",
            complaint_id=cid,
            error=str(exc),
        )

    await _clear_complaint_buttons(bot, session, cid)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass  # уже снято хелпером
    await callback.answer("Отмечено как проверено.")


@router.callback_query(F.data.regexp(r"^complaint:warn:\d+$"))
async def complaint_warn(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    context = await _load_complaint_context(
        bot,
        session,
        callback,
        cid,
        action="warn",
        for_update=True,
    )
    if context is None:
        return
    complaint, group = context

    result = await execute(
        bot=bot,
        session=session,
        chat_id=group.telegram_chat_id,
        group_id=group.id,
        target_id=complaint.target_telegram_id,
        moderator_id=callback.from_user.id,
        action="warn",
        duration=None,
        reason="Жалоба: проверьте контекст",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=public_user_token(complaint.target_telegram_id),
        moderator_name=public_user_token(callback.from_user.id),
    )
    if result.commit:
        await session.commit()

    if not result.success:
        await callback.answer(result or "Не удалось выдать пред.", show_alert=True)
        return

    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "warn"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    target = public_user_token(complaint.target_telegram_id)
    try:
        await send_group_notice(
            bot,
            group.telegram_chat_id,
            f"⚠ {target} получил предупреждение.\nМодератор: {actor}",
            delay_seconds=COMPLAINT_MESSAGE_TTL_SECONDS,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "complaint_notify_failed",
            complaint_id=cid,
            error=str(exc),
        )

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Предупреждение выдано.")


@router.callback_query(F.data.regexp(r"^complaint:ban:\d+$"))
async def complaint_ban_prompt(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    """Кнопка «🚫 Забанить» из истории сообщений — бан сразу, без карточки."""
    cid = int((callback.data or "").rsplit(":", 1)[1])
    callback.data = f"complaint:ban:confirm:{cid}"
    await complaint_ban_confirm(callback, bot, session)


@router.callback_query(F.data.regexp(r"^complaint:ban:confirm:\d+$"))
async def complaint_ban_confirm(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    context = await _load_complaint_context(
        bot,
        session,
        callback,
        cid,
        action="ban",
        for_update=True,
    )
    if context is None:
        return
    complaint, group = context

    result = await execute(
        bot=bot,
        session=session,
        chat_id=group.telegram_chat_id,
        group_id=group.id,
        target_id=complaint.target_telegram_id,
        moderator_id=callback.from_user.id,
        action="ban",
        duration=None,
        reason="Жалоба: грубое нарушение",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=public_user_token(complaint.target_telegram_id),
        moderator_name=public_user_token(callback.from_user.id),
    )
    if result.commit:
        await session.commit()
    if not result.success:
        await callback.answer(result or "Не удалось забанить.", show_alert=True)
        return

    deleted = await _delete_user_messages(
        bot,
        session,
        group.id,
        complaint.target_telegram_id,
        group.telegram_chat_id,
    )
    log.info(
        "user_messages_deleted",
        complaint_id=cid,
        user_telegram_id=complaint.target_telegram_id,
        deleted=deleted,
    )

    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "ban"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    target = public_user_token(complaint.target_telegram_id)
    try:
        await send_group_notice(
            bot,
            group.telegram_chat_id,
            f"🚫 {target} забанен.\nМодератор: {actor}",
            delay_seconds=COMPLAINT_MESSAGE_TTL_SECONDS,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("complaint_notify_failed", complaint_id=cid, error=str(exc))

    await _clear_complaint_buttons(bot, session, cid)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass  # уже снято хелпером
    await callback.answer("Пользователь забанен.")


@router.callback_query(F.data.regexp(r"^complaint:ban:clean:\d+$"))
async def complaint_ban_clean(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    context = await _load_complaint_context(
        bot,
        session,
        callback,
        cid,
        action="ban",
        for_update=True,
    )
    if context is None:
        return
    complaint, group = context

    result = await execute(
        bot=bot,
        session=session,
        chat_id=group.telegram_chat_id,
        group_id=group.id,
        target_id=complaint.target_telegram_id,
        moderator_id=callback.from_user.id,
        action="ban",
        duration=None,
        reason="Жалоба: грубое нарушение (с очисткой)",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=public_user_token(complaint.target_telegram_id),
        moderator_name=public_user_token(callback.from_user.id),
    )
    if result.commit:
        await session.commit()
    if not result.success:
        await callback.answer(result or "Не удалось забанить.", show_alert=True)
        return

    deleted = await _delete_user_messages(
        bot,
        session,
        group.id,
        complaint.target_telegram_id,
        group.telegram_chat_id,
    )
    log.info(
        "user_messages_deleted",
        complaint_id=cid,
        user_telegram_id=complaint.target_telegram_id,
        deleted=deleted,
    )

    # После успешного execute() очистка безопасна: авторизация уже проверена,
    # а повторный ban выполняется только для уже разрешённого действия.
    cleanup_ok = False
    try:
        await bot.unban_chat_member(
            chat_id=group.telegram_chat_id,
            user_id=complaint.target_telegram_id,
            only_if_banned=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "complaint_ban_clean_unban_failed",
            complaint_id=cid,
            error=str(exc),
        )

    try:
        await bot.ban_chat_member(
            chat_id=group.telegram_chat_id,
            user_id=complaint.target_telegram_id,
            revoke_messages=True,
        )
        cleanup_ok = True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "complaint_ban_clean_failed",
            complaint_id=cid,
            error=str(exc),
        )

    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "ban_clean" if cleanup_ok else "ban"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    target = public_user_token(complaint.target_telegram_id)
    if cleanup_ok:
        msg = (
            f"🚫🗑 {target} забанен с очисткой сообщений.\n"
            f"⚠ Часть сообщений могла остаться (ограничения Telegram).\n"
            f"Модератор: {actor}."
        )
    else:
        msg = (
            f"🚫 {target} забанен.\n"
            f"⚠ Очистка не удалась (повторный бан).\n"
            f"Модератор: {actor}."
        )
    try:
        await send_group_notice(
            bot,
            group.telegram_chat_id,
            msg,
            delay_seconds=COMPLAINT_MESSAGE_TTL_SECONDS,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("complaint_notify_failed", complaint_id=cid, error=str(exc))

    await _clear_complaint_buttons(bot, session, cid)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass  # уже снято хелпером
    await callback.answer(
        "Забанен. Попытка очистки выполнена."
        if cleanup_ok
        else "Забанен. Очистка не удалась (ограничения Telegram)."
    )


@router.callback_query(F.data.regexp(r"^complaint:ban:cancel:\d+$"))
async def complaint_ban_cancel(callback: CallbackQuery) -> None:
    if callback.message is not None:
        try:
            await callback.message.delete()
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("complaint_ban_cancel_delete_failed", error=str(exc))
    await callback.answer("Отменено.")


@router.callback_query(F.data.regexp(r"^complaint:mute_reporter:\d+$"))
async def complaint_mute_reporter_prompt(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    context = await _load_complaint_context(
        bot,
        session,
        callback,
        cid,
        action="mute",
    )
    if context is None:
        return
    complaint, _ = context
    if callback.message is None:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return

    reporter = public_user_token(complaint.reporter_telegram_id)
    text = (
        "🤐 Наказать отправителя?\n\n"
        f"👤 Кому: {reporter}\n"
        "⏱ Длительность: 1 минута\n"
        "📝 Причина: Флуд жалобами\n\n"
        "Подтвердите действие."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤐 Замутить на 1 минуту",
                    callback_data=f"complaint:mute_reporter:confirm:{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"complaint:mute_reporter:cancel:{cid}",
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        log.warning(
            "complaint_mute_prompt_edit_failed", complaint_id=cid, error=str(exc)
        )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^complaint:mute_reporter:confirm:\d+$"))
async def complaint_mute_reporter_confirm(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    context = await _load_complaint_context(
        bot,
        session,
        callback,
        cid,
        action="mute",
        for_update=True,
    )
    if context is None:
        return
    complaint, group = context

    result = await execute(
        bot=bot,
        session=session,
        chat_id=group.telegram_chat_id,
        group_id=group.id,
        target_id=complaint.reporter_telegram_id,
        moderator_id=callback.from_user.id,
        action="mute",
        duration=60,
        reason="Не отвлекайте админов",
        warnings_limit=group.settings.warnings_limit,
        default_mute=group.settings.default_mute_seconds,
        target_name=public_user_token(complaint.reporter_telegram_id),
        moderator_name=public_user_token(callback.from_user.id),
    )
    if result.commit:
        await session.commit()
    if not result.success:
        await callback.answer(result or "Не удалось замутить.", show_alert=True)
        return

    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "mute_reporter"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    reporter = public_user_token(complaint.reporter_telegram_id)
    try:
        await send_group_notice(
            bot,
            group.telegram_chat_id,
            f"🤐 {reporter} заглушён на 1 минуту.\nМодератор: {actor}",
            delay_seconds=COMPLAINT_MESSAGE_TTL_SECONDS,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("complaint_notify_failed", complaint_id=cid, error=str(exc))

    await _clear_complaint_buttons(bot, session, cid)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass  # уже снято хелпером
    await callback.answer("Отправитель наказан.")


@router.callback_query(F.data.regexp(r"^complaint:mute_reporter:cancel:\d+$"))
async def complaint_mute_reporter_cancel(callback: CallbackQuery) -> None:
    if callback.message is not None:
        try:
            await callback.message.delete()
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("complaint_mute_cancel_delete_failed", error=str(exc))
    await callback.answer("Отменено.")
