from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery
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
async def complaint_ban(
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
        log.warning(
            "complaint_notify_failed",
            complaint_id=cid,
            error=str(exc),
        )

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Пользователь забанен.")


@router.callback_query(F.data.regexp(r"^complaint:mute_reporter:\d+$"))
async def complaint_mute_reporter(
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
        log.warning(
            "complaint_notify_failed",
            complaint_id=cid,
            error=str(exc),
        )

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отправитель наказан.")
