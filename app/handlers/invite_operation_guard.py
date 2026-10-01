from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.invite_operation_models import InviteOperation
from app.db.models import Group, InviteCampaign
from app.services.access import can_manage_group
from app.services.invite_execution import execute_invite_creation, execute_invite_revocation
from app.services.join_request_transitions import (
    REVOKE_IN_PROGRESS,
    REVOKE_UNCERTAIN,
    begin_invite_creation,
    begin_invite_revocation,
)
from app.services.join_requests import parse_invite_command
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


@router.message(F.text.regexp(r"(?i)^создать ссылку(?:-заявку| заявку)? .+"))
async def create_invite_serialized(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed_group(message, bot, session)
    parsed = parse_invite_command(message.text or "")
    if group is None or parsed is None or message.from_user is None:
        return

    operation = await begin_invite_creation(
        session,
        group=group,
        actor_id=message.from_user.id,
        name=parsed.name,
        creates_join_request=parsed.creates_join_request,
    )
    if operation is None:
        await message.reply(
            "Ссылка с таким названием уже существует либо предыдущая попытка создания требует проверки."
        )
        return

    result = await execute_invite_creation(session, bot, operation_id=operation.id)
    if result.status == "completed":
        mode = "с заявкой" if parsed.creates_join_request else "обычная"
        await message.reply(f"✅ Создана {mode} ссылка «{escape(parsed.name)}»:\n{result.invite_link}")
    elif result.status == "stale_actor":
        await message.reply("Права на группу изменились. Создание ссылки отменено.")
    elif result.status == "compensated":
        await message.reply("Не удалось сохранить новую ссылку. Telegram-ссылка была отозвана.")
    elif result.status == "uncertain":
        await message.reply(
            "Не удалось надёжно сохранить или отозвать новую ссылку; операция помечена как неопределённая."
        )
    elif result.status == "telegram_error":
        await message.reply(f"Не удалось создать ссылку: {escape(result.error_text or 'ошибка Telegram')}")
    else:
        await message.reply("Создание ссылки не выполнено: состояние группы или операции изменилось.")


@router.message(F.text.regexp(r"(?i)^отключить ссылку \d+$"))
async def disable_invite_serialized(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed_group(message, bot, session)
    if group is None or message.from_user is None:
        return
    campaign_id = int((message.text or "").split()[-1])
    campaign = await session.scalar(
        select(InviteCampaign).where(
            InviteCampaign.id == campaign_id,
            InviteCampaign.group_id == group.id,
            InviteCampaign.active.is_(True),
        )
    )
    if campaign is None:
        await message.reply("Активная ссылка с таким ID не найдена.")
        return

    operation = await session.scalar(
        select(InviteOperation)
        .where(
            InviteOperation.campaign_id == campaign.id,
            InviteOperation.operation == "revoke",
            InviteOperation.status == REVOKE_UNCERTAIN,
        )
        .with_for_update()
    )
    if operation is not None:
        operation.status = REVOKE_IN_PROGRESS
        operation.actor_telegram_id = message.from_user.id
        operation.error_text = None
        await session.commit()
    else:
        operation = await begin_invite_revocation(
            session,
            campaign=campaign,
            actor_id=message.from_user.id,
        )
    if operation is None:
        await message.reply("Отзыв этой ссылки уже выполняется.")
        return

    result = await execute_invite_revocation(session, bot, operation_id=operation.id)
    if result.status == "completed":
        await message.reply(f"✅ Ссылка #{campaign.id} отключена.")
    elif result.status == "stale_actor":
        await message.reply("Права на группу изменились. Отзыв ссылки отменён.")
    elif result.status == "uncertain":
        await message.reply(
            "Telegram не подтвердил отзыв ссылки. Mimoru сохранила операцию как неопределённую; повторите команду позже."
        )
    else:
        await message.reply("Отзыв ссылки не выполнен: состояние группы или операции изменилось.")
