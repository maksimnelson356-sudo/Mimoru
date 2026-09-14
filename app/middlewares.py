from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyStat, Group, UserMessage
from app.db.rank_models import RankAssignment
from app.db.session import SessionFactory
from app.keyboards.home import cancel_input_menu
from app.services.deleted_accounts import track_group_member
from app.services.ranks import UNTOUCHABLE
from app.services.repositories import GroupNotConnectedError, upsert_user


async def _remove_cancel_notice(bot: Bot | None, state_data: dict[str, Any]) -> None:
    """Remove only the stale cancel keyboard, not the form message itself."""
    if bot is None:
        return
    message_id = state_data.get("_cancel_message_id")
    chat_id = state_data.get("_cancel_chat_id")
    if not isinstance(message_id, int) or not isinstance(chat_id, int):
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramBadRequest:
        pass


def _callback_is_cancel_notice(
    event: CallbackQuery, state_data: dict[str, Any]
) -> bool:
    if event.message is None:
        return False
    return (
        state_data.get("_cancel_message_id") == event.message.message_id
        and state_data.get("_cancel_chat_id") == event.message.chat.id
    )


async def _cancel_callback(
    state_name: str | None,
    state_data: dict[str, Any],
    session: AsyncSession,
) -> str:
    """Return the logical parent screen for a pending text form."""
    explicit = state_data.get("_cancel_callback")
    if isinstance(explicit, str) and explicit:
        return explicit

    name = state_name or ""
    group_id = state_data.get("group_id")
    user_id = state_data.get("user_id")
    reason_id = state_data.get("reason_id")
    ticket_id = state_data.get("ticket_id")
    item_id = state_data.get("item_id")
    listing_id = state_data.get("listing_id")

    if name.endswith(":support_new"):
        return "panel:support"
    if name.endswith(":ticket_reply") and isinstance(ticket_id, int):
        return f"ticket:{ticket_id}"
    if name.endswith(":word_add") and isinstance(group_id, int):
        return f"words:{group_id}"
    if name.endswith(":channel_add") and isinstance(group_id, int):
        return f"channels:{group_id}"
    if (name.endswith(":welcome_text") or name.endswith(":rules_text")) and isinstance(
        group_id, int
    ):
        return f"settings_detail:{group_id}"
    if name.endswith(":role_add") and isinstance(group_id, int):
        return f"roles:{group_id}"
    if name.endswith(":find_member") and isinstance(group_id, int):
        return f"group_section:{group_id}:members"
    if (
        (name.endswith(":add_note") or name.endswith(":add_tag"))
        and isinstance(group_id, int)
        and isinstance(user_id, int)
    ):
        return f"member_card:{group_id}:{user_id}"
    if name.endswith(":adding") and isinstance(group_id, int):
        return f"reasons:{group_id}"
    if (
        name.endswith(":renaming")
        and isinstance(group_id, int)
        and isinstance(reason_id, int)
    ):
        return f"reason_edit:{group_id}:{reason_id}"

    # Advertising editor forms keep their logical parent in state data.  These
    # forms used to fall through to panel:home, so pressing "Отменить ввод"
    # unexpectedly threw the user out of the editor.
    if name.startswith("GlobalPostForm:") and isinstance(item_id, int):
        return f"gpost:editor:{item_id}"
    if name.startswith("RequiredListingForm:") and isinstance(group_id, int):
        return f"reqlist:group:{group_id}"
    if name.startswith("RequiredDealForm:") and isinstance(listing_id, int):
        return f"reqmarket:{listing_id}"

    if isinstance(group_id, int):
        return f"group:{group_id}"
    return "panel:home"


def _is_regular_group_message(event: Message) -> bool:
    """Return True for a new, non-service group message."""
    return not bool(event.new_chat_members or event.left_chat_member)


async def _track_daily_message(
    session: AsyncSession, group_id: int, event: Message
) -> None:
    """Atomically count one newly-created regular message for any account.

    Human users and bot accounts are both part of group activity statistics.
    Edited-message updates pass through the same database middleware, therefore
    edit_date must be rejected here or every edit would inflate the counters.
    """
    if (
        event.from_user is None
        or event.edit_date is not None
        or not _is_regular_group_message(event)
    ):
        return
    day = event.date.date().isoformat()
    statement = insert(DailyStat).values(
        group_id=group_id,
        user_telegram_id=event.from_user.id,
        date=day,
        messages_count=1,
        deleted_count=0,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[DailyStat.group_id, DailyStat.user_telegram_id, DailyStat.date],
        set_={"messages_count": DailyStat.messages_count + 1},
    )
    await session.execute(statement)


class DatabaseMiddleware(BaseMiddleware):
    """Provide one transaction-scoped AsyncSession per Telegram update."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        log = structlog.get_logger()
        async with SessionFactory() as session:
            data["session"] = session
            try:
                tg_user = getattr(event, "from_user", None)
                group: Group | None = None
                untouchable = False
                if tg_user is not None:
                    user = await upsert_user(session, tg_user)
                    chat = getattr(event, "chat", None)
                    if (
                        isinstance(event, Message)
                        and chat is not None
                        and chat.type in {"group", "supergroup"}
                    ):
                        untouchable_exists = (
                            select(RankAssignment.id)
                            .where(
                                RankAssignment.group_id == Group.id,
                                RankAssignment.user_telegram_id == tg_user.id,
                                RankAssignment.rank_code == UNTOUCHABLE,
                                RankAssignment.active.is_(True),
                            )
                            .exists()
                        )
                        group_row = (
                            await session.execute(
                                select(
                                    Group, untouchable_exists.label("is_untouchable")
                                ).where(
                                    Group.telegram_chat_id == chat.id,
                                    Group.is_active.is_(True),
                                )
                            )
                        ).one_or_none()
                        if group_row is not None:
                            group = group_row[0]
                            untouchable = bool(group_row[1])
                            # upsert_user() already ran above, and this hot path does
                            # not need the GroupMember ORM row back. Avoid repeating
                            # the user upsert and returning a member row per message.
                            await track_group_member(
                                session,
                                group.id,
                                tg_user,
                                present=True,
                                ensure_user=False,
                                return_row=False,
                            )
                            await _track_daily_message(session, group.id, event)
                            session.add(
                                UserMessage(
                                    group_id=group.id,
                                    user_telegram_id=tg_user.id,
                                    message_id=event.message_id,
                                )
                            )
                    completed_payment = (
                        isinstance(event, Message)
                        and event.successful_payment is not None
                    )
                    if user.service_blocked and not completed_payment:
                        if isinstance(event, PreCheckoutQuery):
                            await event.answer(
                                ok=False,
                                error_message="Доступ к Mimoru ограничен. Новая оплата сейчас недоступна.",
                            )
                        elif isinstance(event, CallbackQuery):
                            await event.answer(
                                "Доступ к Mimoru ограничен.", show_alert=True
                            )
                        elif isinstance(event, Message):
                            await event.answer(
                                "Доступ к Mimoru ограничен. Обратитесь в поддержку."
                            )
                        await session.commit()
                        return None

                    # "Недотрога" keeps presence/activity accounting, but its
                    # regular messages do not trigger Mimoru commands/automod.
                    if (
                        isinstance(event, Message)
                        and group is not None
                        and _is_regular_group_message(event)
                        and untouchable
                    ):
                        await session.commit()
                        return None

                state = data.get("state")
                bot = data.get("bot") if isinstance(data.get("bot"), Bot) else None
                state_before: str | None = None
                state_data_before: dict[str, Any] = {}
                if isinstance(state, FSMContext):
                    state_before = await state.get_state()
                    if state_before:
                        state_data_before = await state.get_data()

                # A cancel button attached to an active text/photo form is a
                # navigation action, not merely another callback. Clear the FSM
                # before dispatching its target callback so the requested
                # parent screen is restored and the next user message cannot be
                # consumed by the cancelled form.
                if (
                    isinstance(state, FSMContext)
                    and state_before
                    and isinstance(event, CallbackQuery)
                    and _callback_is_cancel_notice(event, state_data_before)
                ):
                    await state.clear()

                result = await handler(event, data)
                if session.in_transaction():
                    await session.commit()

                if isinstance(state, FSMContext):
                    state_after = await state.get_state()
                    if isinstance(event, CallbackQuery) and event.message is not None:
                        if state_after and state_after != state_before:
                            form_data = await state.get_data()
                            cancel_callback = await _cancel_callback(
                                state_after, form_data, session
                            )
                            try:
                                await event.message.edit_reply_markup(
                                    reply_markup=cancel_input_menu(cancel_callback)
                                )
                            except TelegramBadRequest:
                                pass
                            await state.update_data(
                                _cancel_message_id=event.message.message_id,
                                _cancel_chat_id=event.message.chat.id,
                                _cancel_callback=cancel_callback,
                            )
                        elif state_before and state_after == state_before:
                            await _remove_cancel_notice(bot, state_data_before)
                        elif state_before and not state_after:
                            if not (
                                isinstance(event, CallbackQuery)
                                and _callback_is_cancel_notice(event, state_data_before)
                            ):
                                await _remove_cancel_notice(bot, state_data_before)
                    elif (
                        isinstance(event, Message) and state_before and not state_after
                    ):
                        await _remove_cancel_notice(bot, state_data_before)
                return result
            except GroupNotConnectedError:
                await session.rollback()
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        "Сначала подключите группу командой «подключить».",
                        show_alert=True,
                    )
                elif isinstance(event, Message):
                    await event.answer(
                        "Сначала создатель группы должен подключить Mimoru командой «подключить»."
                    )
                return None
            except SQLAlchemyError:
                await session.rollback()
                log.exception("database_update_failed")
                raise
            except Exception:
                await session.rollback()
                raise
