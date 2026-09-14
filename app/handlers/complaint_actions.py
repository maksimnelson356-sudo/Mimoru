from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Complaint, Group
from app.services.moderation import execute
from app.services.public_identity import public_user_token

log = structlog.get_logger(__name__)

router = Router(name=__name__)


async def _get_pending(
    session: AsyncSession,
    complaint_id: int,
) -> Complaint | None:
    complaint = await session.get(Complaint, complaint_id)
    if complaint is None or complaint.status != "pending":
        return None
    return complaint


async def _reject_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта жалоба уже обработана.", show_alert=True)


@router.callback_query(F.data.regexp(r"^complaint:ack:\d+$"))
async def complaint_ack(callback: CallbackQuery, session: AsyncSession) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
    group = await session.get(Group, complaint.group_id)
    if group is None:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return
    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "ack"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    try:
        await callback.bot.send_message(
            group.telegram_chat_id,
            f"✅ Жалоба обработана.\nМодератор: {actor}",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "complaint_notify_failed",
            complaint_id=cid,
            error=str(exc),
        )

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отмечено как проверено.")


@router.callback_query(F.data.regexp(r"^complaint:warn:\d+$"))
async def complaint_warn(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
    group = await session.get(Group, complaint.group_id)
    if group is None:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return

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
        await bot.send_message(
            group.telegram_chat_id,
            f"⚠ {target} получил предупреждение.\nМодератор: {actor}",
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
async def complaint_ban_prompt(callback: CallbackQuery, session: AsyncSession) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
    if callback.message is None:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return

    target = public_user_token(complaint.target_telegram_id)
    text = (
        "🛑 Подтверждение бана\n\n"
        f"👤 Нарушитель: {target}\n\n"
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
                    callback_data=f"complaint:ban:confirm:{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔴🗑 Бан + очистка",
                    callback_data=f"complaint:ban:clean:{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"complaint:ban:cancel:{cid}",
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        log.warning(
            "complaint_ban_prompt_edit_failed", complaint_id=cid, error=str(exc)
        )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^complaint:ban:confirm:\d+$"))
async def complaint_ban_confirm(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
    group = await session.get(Group, complaint.group_id)
    if group is None:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return

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

    complaint.status = "processed"
    complaint.reviewed_by_telegram_id = callback.from_user.id
    complaint.resolution = "ban"
    await session.commit()

    actor = public_user_token(callback.from_user.id)
    target = public_user_token(complaint.target_telegram_id)
    try:
        await bot.send_message(
            group.telegram_chat_id,
            f"🚫 {target} забанен.\nМодератор: {actor}",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("complaint_notify_failed", complaint_id=cid, error=str(exc))

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Пользователь забанен.")


@router.callback_query(F.data.regexp(r"^complaint:ban:clean:\d+$"))
async def complaint_ban_clean(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
    group = await session.get(Group, complaint.group_id)
    if group is None:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return

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

    # Всегда пробуем очистку.
    # ВАЖНО: Telegram не удаляет сообщения при повторном
    # ban_chat_member(revoke_messages=True), если пользователь УЖЕ забанен.
    # Поэтому сначала снимаем бан (только если он есть), потом баним
    # с revoke_messages=True — тогда очистка сработает.
    cleanup_ok = False
    cleanup_error = ""

    # Шаг 1: разбанить, если пользователь уже забанен
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

    # Шаг 2: забанить с очисткой
    try:
        await bot.ban_chat_member(
            chat_id=group.telegram_chat_id,
            user_id=complaint.target_telegram_id,
            revoke_messages=True,
        )
        cleanup_ok = True
    except TelegramBadRequest as exc:
        cleanup_error = str(exc)
        log.warning(
            "complaint_ban_clean_failed",
            complaint_id=cid,
            error=cleanup_error,
        )
    except TelegramForbiddenError as exc:
        cleanup_error = str(exc)
        log.warning(
            "complaint_ban_clean_forbidden",
            complaint_id=cid,
            error=cleanup_error,
        )

    # Реальная ошибка — только если и бан, и очистка провалились
    if not result.success and not cleanup_ok:
        await callback.answer(result or "Не удалось забанить.", show_alert=True)
        return

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
            f"Модератор: {actor}"
        )
    else:
        msg = (
            f"🚫 {target} забанен.\n"
            f"⚠ Очистка не удалась (сообщения старше 48 часов или "
            f"повторный бан).\nМодератор: {actor}"
        )
    try:
        await bot.send_message(group.telegram_chat_id, msg)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("complaint_notify_failed", complaint_id=cid, error=str(exc))

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
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
    callback: CallbackQuery, session: AsyncSession
) -> None:
    cid = int((callback.data or "").rsplit(":", 1)[1])
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
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
    complaint = await _get_pending(session, cid)
    if complaint is None:
        await _reject_stale(callback)
        return
    group = await session.get(Group, complaint.group_id)
    if group is None:
        await callback.answer("Группа больше не активна.", show_alert=True)
        return

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
        await bot.send_message(
            group.telegram_chat_id,
            f"🤐 {reporter} замучен на 1 минуту за флуд жалобами.\nМодератор: {actor}",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("complaint_notify_failed", complaint_id=cid, error=str(exc))

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отправитель наказан.")


@router.callback_query(F.data.regexp(r"^complaint:mute_reporter:cancel:\d+$"))
async def complaint_mute_reporter_cancel(callback: CallbackQuery) -> None:
    if callback.message is not None:
        try:
            await callback.message.delete()
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("complaint_mute_cancel_delete_failed", error=str(exc))
    await callback.answer("Отменено.")
