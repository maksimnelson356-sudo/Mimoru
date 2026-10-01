from __future__ import annotations

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.access import can_manage_group
from app.services.repositories import get_or_create_group


async def managed_group_for_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    denial_text: str,
    for_update: bool = False,
) -> Group | None:
    """Resolve a managed group and optionally serialize a mutation with ownership transfer."""
    if message.from_user is None:
        return None
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if for_update:
        # The Group may already be present in this Session's identity map from the
        # unlocked lookup above. Force the locking SELECT to overwrite that snapshot
        # with the current committed row before owner authorization.
        group = await session.scalar(
            select(Group).where(
                Group.id == group.id,
                Group.is_active.is_(True),
            ).with_for_update().execution_options(populate_existing=True)
        )
        if group is None:
            await message.reply("Группа больше не обслуживается.")
            return None
    if not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply(denial_text)
        return None
    return group
