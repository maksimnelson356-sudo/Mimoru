from __future__ import annotations

from datetime import datetime, timezone

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameMessage
from app.games.locks import advisory_xact_lock
from app.services.ui import is_chat_unavailable

log = structlog.get_logger()
_GAME_PHASE_MESSAGE_LOCK_NAMESPACE = 4_676_945
_GAME_MESSAGE_RETIRE_LOCK_NAMESPACE = 4_676_947


async def register_game_message(
    session: AsyncSession,
    *,
    game_id: int,
    chat_id: int,
    message_id: int,
    kind: str = "temporary",
) -> GameMessage:
    await session.execute(
        insert(GameMessage)
        .values(
            game_id=game_id,
            chat_id=chat_id,
            message_id=message_id,
            kind=kind,
            active=True,
        )
        .on_conflict_do_update(
            index_elements=["game_id", "message_id"],
            set_={
                "chat_id": chat_id,
                "kind": kind,
                "active": True,
                "retired_at": None,
            },
        )
    )
    await session.commit()
    record = await session.scalar(
        select(GameMessage).where(
            GameMessage.game_id == game_id,
            GameMessage.message_id == message_id,
        )
    )
    if record is None:
        raise RuntimeError("failed to register game message")
    return record


async def retire_game_message(
    bot: Bot,
    session: AsyncSession,
    *,
    record: GameMessage,
    replacement_text: str | None = None,
) -> None:
    await advisory_xact_lock(
        session,
        namespace=_GAME_MESSAGE_RETIRE_LOCK_NAMESPACE,
        key=record.id,
    )
    current = await session.scalar(
        select(GameMessage).where(GameMessage.id == record.id).with_for_update()
    )
    if current is None or not current.active:
        await session.commit()
        return
    try:
        if replacement_text is not None:
            await bot.edit_message_text(
                chat_id=current.chat_id,
                message_id=current.message_id,
                text=replacement_text,
                reply_markup=None,
            )
        else:
            await bot.delete_message(current.chat_id, current.message_id)
    except TelegramBadRequest as error:
        error_text = str(error).casefold()
        if not any(
            marker in error_text
            for marker in (
                "message to delete not found",
                "message can't be deleted",
                "message is not modified",
                "message to edit not found",
            )
        ):
            log.info(
                "game_message_retire_failed",
                game_id=current.game_id,
                message_id=current.message_id,
                error=str(error),
            )
    except TelegramForbiddenError as error:
        log.info(
            "game_message_retire_forbidden",
            game_id=current.game_id,
            message_id=current.message_id,
            error=str(error),
        )
    current.active = False
    current.retired_at = datetime.now(timezone.utc)
    await session.commit()


async def retire_active_messages(
    bot: Bot,
    session: AsyncSession,
    *,
    game_id: int,
    kind: str | None = None,
    replacement_text: str | None = None,
) -> int:
    query = select(GameMessage).where(
        GameMessage.game_id == game_id,
        GameMessage.active.is_(True),
    )
    if kind is not None:
        query = query.where(GameMessage.kind == kind)
    records = list((await session.scalars(query.order_by(GameMessage.id))).all())
    retired = 0
    for record in records:
        was_active = record.active
        await retire_game_message(
            bot,
            session,
            record=record,
            replacement_text=replacement_text,
        )
        await session.refresh(record)
        if was_active and not record.active:
            retired += 1
    return retired


async def upsert_phase_message(
    bot: Bot,
    session: AsyncSession,
    *,
    game_id: int,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    kind: str = "phase",
) -> Message | None:
    await advisory_xact_lock(
        session,
        namespace=_GAME_PHASE_MESSAGE_LOCK_NAMESPACE,
        key=game_id,
    )
    try:
        existing = await session.scalar(
            select(GameMessage)
            .where(
                GameMessage.game_id == game_id,
                GameMessage.kind == kind,
                GameMessage.active.is_(True),
            )
            .order_by(GameMessage.id.desc())
            .limit(1)
        )
        if existing is not None:
            try:
                await bot.edit_message_text(
                    chat_id=existing.chat_id,
                    message_id=existing.message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                await session.commit()
                return None
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).casefold():
                    await session.commit()
                    return None
                existing.active = False
                existing.retired_at = datetime.now(timezone.utc)
            except TelegramForbiddenError as error:
                log.info(
                    "game_phase_message_edit_forbidden",
                    game_id=game_id,
                    message_id=existing.message_id,
                    error=str(error),
                )
                await session.commit()
                return None

        try:
            message = await bot.send_message(chat_id, text, reply_markup=reply_markup)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            if is_chat_unavailable(error):
                log.debug(
                    "game_phase_message_send_failed", game_id=game_id, error=str(error)
                )
            else:
                log.warning(
                    "game_phase_message_send_failed", game_id=game_id, error=str(error)
                )
            await session.commit()
            return None
        await register_game_message(
            session,
            game_id=game_id,
            chat_id=chat_id,
            message_id=message.message_id,
            kind=kind,
        )
        return message
    except Exception:
        await session.rollback()
        raise
