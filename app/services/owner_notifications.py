from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from app.db.models import Group
from app.db.session import SessionFactory


async def send_to_current_group_owner(
    bot: Bot,
    *,
    group_id: int,
    text: str,
) -> tuple[bool, int | None, str | None]:
    """Resolve the current owner under the ownership-transfer lock and deliver once."""
    async with SessionFactory() as session:
        group = await session.scalar(
            select(Group).where(Group.id == group_id).with_for_update()
        )
        if group is None or not group.is_active or group.owner_telegram_id is None:
            return False, None, "group_unavailable"

        owner_id = group.owner_telegram_id
        try:
            await bot.send_message(owner_id, text)
        except (TelegramAPIError, TimeoutError) as error:
            # Every Telegram-side failure must be reported to the caller instead of
            # escaping. The caller has already committed its pre-send claim, so an
            # escaping exception would strand that claim and permanently suppress the
            # daily report / subscription notice for the rest of its window.
            #
            # End the transaction explicitly so the ownership row lock is released
            # before the caller logs/continues its background iteration.
            await session.commit()
            return False, owner_id, str(error)

        await session.commit()
        return True, owner_id, None
