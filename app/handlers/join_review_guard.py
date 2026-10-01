from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.access import can_manage_group
from app.services.join_request_transitions import claim_join_review
from app.services.join_requests import join_request_status_label
from app.services.join_review_execution import execute_join_review
from app.services.repositories import get_or_create_group


router = Router(name=__name__)


async def _managed_group(message: Message, bot: Bot, session: AsyncSession) -> Group | None:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or message.from_user is None:
        return None
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply("Изменять эти настройки может владелец или администратор группы.")
        return None
    return group


async def _review_request_serialized(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    approve: bool,
) -> None:
    group = await _managed_group(message, bot, session)
    if group is None or message.from_user is None:
        return
    request_id = int((message.text or "").split()[-1])
    row = await claim_join_review(
        session,
        request_id=request_id,
        group_id=group.id,
        actor_id=message.from_user.id,
        approve=approve,
    )
    if row is None:
        await message.reply("Ожидающая заявка с таким ID не найдена или уже обрабатывается.")
        return

    result = await execute_join_review(
        session,
        bot,
        request_id=row.id,
        approve=approve,
    )
    if result.status == "completed":
        await message.reply(
            f"✅ Заявка #{row.id}: {join_request_status_label(result.request_status or '')}."
        )
    elif result.status == "stale_actor":
        await message.reply(
            "Права на группу изменились. Заявка возвращена в ожидание решения текущего владельца."
        )
    elif result.status == "telegram_error":
        await message.reply(
            f"Не удалось обработать заявку: {escape(result.error_text or 'ошибка Telegram')}"
        )
    else:
        await message.reply("Заявка не обработана: состояние группы или заявки изменилось.")


@router.message(F.text.regexp(r"(?i)^одобрить заявку \d+$"))
async def approve_request_serialized(message: Message, bot: Bot, session: AsyncSession) -> None:
    await _review_request_serialized(message, bot, session, approve=True)


@router.message(F.text.regexp(r"(?i)^отклонить заявку \d+$"))
async def decline_request_serialized(message: Message, bot: Bot, session: AsyncSession) -> None:
    await _review_request_serialized(message, bot, session, approve=False)
