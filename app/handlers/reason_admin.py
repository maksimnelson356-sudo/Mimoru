from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationReason
from app.keyboards.panel import moderation_reason_picker, reason_delete_confirm, reason_edit_menu, reasons_menu
from app.services.access import accessible_group, is_service_owner
from app.services.moderation_reasons import ensure_default_reasons, normalize_actions
from app.services.ui import panel_header
from app.services.plans import plan_limit

router = Router(name=__name__)


class ReasonForm(StatesGroup):
    adding = State()
    renaming = State()


async def owned_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    q = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        q = q.where(Group.owner_telegram_id == user_id)
    if for_update:
        q = q.with_for_update()
    return await session.scalar(q)


async def get_reason(session: AsyncSession, group_id: int, reason_id: int) -> ModerationReason | None:
    return await session.scalar(select(ModerationReason).where(ModerationReason.id == reason_id, ModerationReason.group_id == group_id))


@router.callback_query(F.data.regexp(r"^reasons:\d+$"))
async def reasons(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id, for_update=True)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await ensure_default_reasons(session, group.id)
    rows = (await session.scalars(select(ModerationReason).where(ModerationReason.group_id == group.id).order_by(ModerationReason.sort_order, ModerationReason.id))).all()
    await session.commit()
    await callback.message.edit_text(
        panel_header("Причины наказаний", "У каждой группы свой набор. Нажмите причину для настройки."),
        reply_markup=reasons_menu(group.id, rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reason_add:\d+$"))
async def reason_add(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    current = int(await session.scalar(select(func.count()).select_from(ModerationReason).where(ModerationReason.group_id == group.id)) or 0)
    if current >= plan_limit(group, "reasons"):
        await callback.answer("Достигнут лимит причин текущего тарифа.", show_alert=True)
        return
    await state.set_state(ReasonForm.adding)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(panel_header("Новая причина", "Отправьте одним сообщением название причины. Например: Провокация конфликта"))
    await callback.answer()


@router.message(ReasonForm.adding, F.chat.type == "private")
async def reason_add_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    group_id = int(data["group_id"])
    if not await owned_group(session, group_id, message.from_user.id, for_update=True):
        await state.clear()
        await message.answer("Доступ к группе потерян.")
        return
    name = (message.text or "").strip()[:120]
    if len(name) < 2:
        await message.answer("Название слишком короткое. Попробуйте ещё раз.")
        return
    row = ModerationReason(group_id=group_id, name=name, actions=["warn", "mute", "kick", "ban"], active=True, sort_order=100)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.answer("Такая причина уже существует. Отправьте другое название.")
        return
    await state.clear()
    await message.answer(panel_header("Причина добавлена", f"{name}\nПо умолчанию доступна для предупреждения, мута, кика и бана."), reply_markup=reason_edit_menu(group_id, row))


@router.callback_query(F.data.regexp(r"^reason_edit:\d+:\d+$"))
async def reason_edit(callback: CallbackQuery, session: AsyncSession) -> None:
    _, g, r = callback.data.split(":")
    group_id, reason_id = int(g), int(r)
    if not await accessible_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    row = await get_reason(session, group_id, reason_id)
    if not row:
        await callback.answer("Причина уже удалена.", show_alert=True)
        return
    actions = {"warn": "предупреждение", "mute": "мут", "kick": "кик", "ban": "бан"}
    enabled = ", ".join(actions[x] for x in normalize_actions(row.actions)) or "ни для одного действия"
    await callback.message.edit_text(panel_header("Причина", row.name) + f"\n\nСтатус: {'✅ включена' if row.active else '❌ выключена'}\nИспользуется: {enabled}", reply_markup=reason_edit_menu(group_id, row))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reason_action:\d+:\d+:(warn|mute|kick|ban)$"))
async def reason_action(callback: CallbackQuery, session: AsyncSession) -> None:
    _, g, r, action = callback.data.split(":")
    group_id, reason_id = int(g), int(r)
    if not await owned_group(session, group_id, callback.from_user.id, for_update=True):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    row = await get_reason(session, group_id, reason_id)
    if not row:
        await callback.answer("Причина не найдена.", show_alert=True)
        return
    actions = normalize_actions(row.actions)
    if action in actions:
        actions.remove(action)
    else:
        actions.append(action)
    row.actions = actions
    await session.commit()
    await callback.message.edit_reply_markup(reply_markup=reason_edit_menu(group_id, row))
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^reason_toggle:\d+:\d+$"))
async def reason_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    _, g, r = callback.data.split(":")
    group_id, reason_id = int(g), int(r)
    if not await owned_group(session, group_id, callback.from_user.id, for_update=True):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    row = await get_reason(session, group_id, reason_id)
    if not row:
        await callback.answer("Причина не найдена.", show_alert=True)
        return
    row.active = not row.active
    await session.commit()
    await callback.message.edit_reply_markup(reply_markup=reason_edit_menu(group_id, row))
    await callback.answer("Включена" if row.active else "Выключена")


@router.callback_query(F.data.regexp(r"^reason_rename:\d+:\d+$"))
async def reason_rename(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, g, r = callback.data.split(":")
    group_id, reason_id = int(g), int(r)
    if not await accessible_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    row = await get_reason(session, group_id, reason_id)
    if not row:
        await callback.answer("Причина не найдена.", show_alert=True)
        return
    await state.set_state(ReasonForm.renaming)
    await state.update_data(group_id=group_id, reason_id=reason_id)
    await callback.message.edit_text(panel_header("Переименование причины", f"Сейчас: {row.name}\n\nОтправьте новое название одним сообщением."))
    await callback.answer()


@router.message(ReasonForm.renaming, F.chat.type == "private")
async def reason_rename_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    group_id, reason_id = int(data["group_id"]), int(data["reason_id"])
    if not await owned_group(session, group_id, message.from_user.id, for_update=True):
        await state.clear(); await message.answer("Доступ к группе потерян."); return
    row = await get_reason(session, group_id, reason_id)
    if not row:
        await state.clear(); await message.answer("Причина уже удалена."); return
    name = (message.text or "").strip()[:120]
    if len(name) < 2:
        await message.answer("Название слишком короткое."); return
    row.name = name
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback(); await message.answer("Такая причина уже есть. Введите другое название."); return
    await state.clear()
    await message.answer(panel_header("Причина переименована", name), reply_markup=reason_edit_menu(group_id, row))


@router.callback_query(F.data.regexp(r"^reason_delete_confirm:\d+:\d+$"))
async def reason_delete_ask(callback: CallbackQuery, session: AsyncSession) -> None:
    _, g, r = callback.data.split(":")
    group_id, reason_id = int(g), int(r)
    if not await accessible_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    row = await get_reason(session, group_id, reason_id)
    if not row:
        await callback.answer("Причина не найдена.", show_alert=True); return
    await callback.message.edit_text(panel_header("Удалить причину?", f"{row.name}\n\nСтарые записи в истории наказаний сохранят свой текст."), reply_markup=reason_delete_confirm(group_id, reason_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reason_delete:\d+:\d+$"))
async def reason_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    _, g, r = callback.data.split(":")
    group_id, reason_id = int(g), int(r)
    if not await owned_group(session, group_id, callback.from_user.id, for_update=True):
        await callback.answer("Нет доступа.", show_alert=True); return
    row = await get_reason(session, group_id, reason_id)
    if row:
        await session.delete(row); await session.commit()
    await reasons(callback, session)


@router.callback_query(F.data.regexp(r"^moderation_help:\d+$"))
async def moderation_help(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    if not await accessible_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    text = panel_header("Как модерировать") + "\n\nОтветьте на сообщение участника одной из команд:\n<code>пред</code>\n<code>мут 5м</code>\n<code>кик</code>\n<code>бан</code>\n\nMimoru покажет причины этой группы кнопками и выполнит действие только после выбора."
    from app.keyboards.panel import moderation_menu
    await callback.message.edit_text(text, reply_markup=moderation_menu(group_id))
    await callback.answer()

# ---- group moderation reason selection ----
import json
from redis.asyncio import Redis
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from app.services.access import can_moderate
from app.services.moderation import execute
from app.services.moderation_reasons import active_reasons, normalize_actions


@router.callback_query(F.data.regexp(r"^modduration:[0-9a-f]{10}:\d+$"))
async def moderation_duration_selected(callback: CallbackQuery, session: AsyncSession, redis: Redis) -> None:
    _, token, raw_seconds = callback.data.split(":")
    key = f"mimoru:modpending:{token}"
    raw = await redis.get(key)
    if not raw:
        await callback.answer("Это действие уже выполнено или устарело.", show_alert=True)
        return
    data = json.loads(raw)
    if int(data["moderator_id"]) != callback.from_user.id:
        await callback.answer("Выбрать срок может только инициатор действия.", show_alert=True)
        return
    seconds = int(raw_seconds)
    if seconds not in {300, 900, 1800, 3600, 21600, 86400, 604800}:
        await callback.answer("Недопустимый срок.", show_alert=True)
        return
    group = await session.scalar(select(Group).where(Group.id == int(data["group_id"]), Group.is_active.is_(True)))
    if not group:
        await redis.delete(key)
        await callback.answer("Группа больше недоступна.", show_alert=True)
        return
    reasons = await active_reasons(session, group.id, "mute")
    await session.commit()
    if not reasons:
        await redis.delete(key)
        await callback.answer("Для мута нет активных причин.", show_alert=True)
        return
    data["duration"] = seconds
    await redis.setex(key, 600, json.dumps(data, ensure_ascii=False))
    await callback.message.edit_text(
        f"📌 Выберите причину мута для <b>{__import__('html').escape(data['target_name'])}</b>.",
        reply_markup=moderation_reason_picker(token, reasons),
    )
    await callback.answer()


async def _deliver_outcome_message(callback: CallbackQuery, bot: Bot, chat_id: int, text: str) -> None:
    try:
        await callback.message.edit_text(text)
    except (TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(chat_id, text)


@router.callback_query(F.data.regexp(r"^modreason:[0-9a-f]{10}:\d+$"))
async def moderation_reason_selected(callback: CallbackQuery, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    _, token, raw_reason_id = callback.data.split(":")
    key = f"mimoru:modpending:{token}"
    raw = await redis.get(key)
    if not raw:
        await callback.answer("Это действие уже выполнено или устарело.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as exc:
            import structlog
            structlog.get_logger().debug("stale_reason_keyboard_cleanup_failed", error=str(exc))
        return
    data = json.loads(raw)
    if int(data["moderator_id"]) != callback.from_user.id:
        await callback.answer("Причину может выбрать только администратор, который вызвал команду.", show_alert=True)
        return
    origin = data.get("origin", "group")
    if origin == "group" and callback.message.chat.id != int(data["chat_id"]):
        await callback.answer("Эта кнопка относится к другой группе.", show_alert=True)
        return
    if origin == "panel" and callback.message.chat.type != "private":
        await callback.answer("Это действие нужно завершить в личной панели Mimoru.", show_alert=True)
        return
    group = await session.scalar(select(Group).where(Group.id == int(data["group_id"]), Group.is_active.is_(True)))
    if not group or not await can_moderate(bot, session, group, callback.from_user.id, data["action"]):
        await callback.answer("Право на это действие больше недоступно.", show_alert=True)
        await redis.delete(key)
        return
    reason = await get_reason(session, group.id, int(raw_reason_id))
    if not reason or not reason.active or data["action"] not in normalize_actions(reason.actions):
        await callback.answer("Эта причина отключена или больше не подходит для действия.", show_alert=True)
        return
    # Atomic consume: double taps cannot execute the punishment twice.
    deleted = await redis.delete(key)
    if not deleted:
        await callback.answer("Действие уже выполнено.", show_alert=True)
        return
    try:
        result = await execute(
            bot=bot,
            session=session,
            chat_id=int(data["chat_id"]),
            group_id=group.id,
            target_id=int(data["target_id"]),
            moderator_id=callback.from_user.id,
            action=data["action"],
            duration=data.get("duration"),
            reason=reason.name,
            warnings_limit=int(data["warnings_limit"]),
            default_mute=int(data["default_mute"]),
            target_name=data["target_name"],
            moderator_name=data["moderator_name"],
            actor_role=data.get("actor_role", "admin"),
        )
        if result.commit:
            await session.commit()
        else:
            await session.rollback()

        if not result.success:
            if origin == "panel":
                public_delivered = False
                if result.public_notice:
                    try:
                        await bot.send_message(int(data["chat_id"]), result)
                        public_delivered = True
                    except (TelegramBadRequest, TelegramForbiddenError) as notify_exc:
                        import structlog
                        structlog.get_logger().warning(
                            "moderation_public_notice_failed",
                            group_id=group.id,
                            target_id=int(data["target_id"]),
                            action=data["action"],
                            error=str(notify_exc),
                        )
                if result.commit:
                    prefix = f"⚠️ Действие выполнено частично в группе «{__import__('html').escape(group.title)}»."
                    if result.public_notice and not public_delivered:
                        prefix += " Уведомление в группу отправить не удалось."
                else:
                    prefix = f"❌ Действие не выполнено в группе «{__import__('html').escape(group.title)}»."
                await callback.message.edit_text(
                    f"{prefix}\n\n{result}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="👤 К карточке", callback_data=f"member_card:{group.id}:{int(data['target_id'])}")
                    ]]),
                )
            else:
                await _deliver_outcome_message(callback, bot, int(data["chat_id"]), result)
            await callback.answer(
                "Частично выполнено" if result.commit else "Не выполнено",
                show_alert=not result.commit,
            )
            return

        if origin == "panel":
            public_delivered = True
            if result.public_notice:
                try:
                    await bot.send_message(int(data["chat_id"]), result)
                except (TelegramBadRequest, TelegramForbiddenError) as notify_exc:
                    public_delivered = False
                    import structlog
                    structlog.get_logger().warning(
                        "moderation_public_notice_failed",
                        group_id=group.id, target_id=int(data["target_id"]), action=data["action"], error=str(notify_exc),
                    )
            status = (
                f"✅ Действие выполнено в группе «{__import__('html').escape(group.title)}».\n\n{result}"
                if public_delivered
                else f"⚠️ Действие выполнено в группе «{__import__('html').escape(group.title)}», но уведомление в группу отправить не удалось.\n\n{result}"
            )
            await callback.message.edit_text(
                status,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👤 К карточке", callback_data=f"member_card:{group.id}:{int(data['target_id'])}")
                ]]),
            )
        else:
            await _deliver_outcome_message(callback, bot, int(data["chat_id"]), result)
        await callback.answer("Готово")
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await session.rollback()
        await callback.message.edit_text(f"❌ Не удалось выполнить действие. Проверьте права Mimoru в группе.\n\n{exc.message}")
        await callback.answer("Ошибка Telegram", show_alert=True)


@router.callback_query(F.data.regexp(r"^modcancel:[0-9a-f]{10}$"))
async def moderation_reason_cancel(callback: CallbackQuery, redis: Redis) -> None:
    token = callback.data.split(":")[1]
    key = f"mimoru:modpending:{token}"
    raw = await redis.get(key)
    if not raw:
        await callback.answer("Действие уже устарело.")
        return
    data = json.loads(raw)
    if int(data["moderator_id"]) != callback.from_user.id:
        await callback.answer("Отменить может только администратор, который вызвал команду.", show_alert=True)
        return
    await redis.delete(key)
    if data.get("origin") == "panel":
        await callback.message.edit_text(
            "✖️ Действие отменено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="👤 К карточке", callback_data=f"member_card:{int(data['group_id'])}:{int(data['target_id'])}")
            ]]),
        )
    else:
        await callback.message.edit_text("✖️ Действие отменено.")
    await callback.answer()
