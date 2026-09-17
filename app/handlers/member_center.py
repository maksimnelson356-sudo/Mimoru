from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from html import escape

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Complaint,
    DailyStat,
    Group,
    GroupMember,
    MemberTag,
    MemberTagAssignment,
    ModerationLog,
    ModeratorNote,
    Punishment,
    User,
    Warning,
)
from app.keyboards.panel import (
    active_punishments_menu,
    complaint_review_menu,
    complaints_menu,
    member_card_menu,
    member_tags_menu,
    moderation_duration_picker,
    moderation_reason_picker,
    people_list_menu,
)
from app.services.access import (
    accessible_group,
    is_service_owner,
)
from app.services.moderation import UNMUTED, deactivate_punishments, log_action
from app.services.moderation_reasons import active_reasons
from app.services.people import calculate_reputation, days_since, trust_label
from app.services.permissions import target_is_protected
from app.services.public_identity import public_user_token, stored_visible_name
from app.services.ui import manual_action_notice, panel_header

router = Router(name=__name__)

# Callback families are explicit for coverage/documentation.
PEOPLE_CALLBACK_FAMILIES = (
    "people_active",
    "people_inactive",
    "people_new",
    "people_suspicious",
)


class MemberCenterForm(StatesGroup):
    find_member = State()
    add_note = State()
    add_tag = State()


async def owned_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


def _user_name(user: User | None, user_id: int) -> str:
    return public_user_token(user_id)


async def _member_card(session: AsyncSession, group: Group, user_id: int):
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(DailyStat.messages_count), 0),
                func.coalesce(func.sum(DailyStat.deleted_count), 0),
            ).where(
                DailyStat.group_id == group.id, DailyStat.user_telegram_id == user_id
            )
        )
    ).one()
    warnings = int(
        await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(
                Warning.group_id == group.id,
                Warning.user_telegram_id == user_id,
                Warning.active.is_(True),
            )
        )
        or 0
    )
    all_warnings = int(
        await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(Warning.group_id == group.id, Warning.user_telegram_id == user_id)
        )
        or 0
    )
    mutes_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Punishment)
            .where(
                Punishment.group_id == group.id,
                Punishment.user_telegram_id == user_id,
                Punishment.kind == "mute",
            )
        )
        or 0
    )
    bans_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Punishment)
            .where(
                Punishment.group_id == group.id,
                Punishment.user_telegram_id == user_id,
                Punishment.kind == "ban",
            )
        )
        or 0
    )
    complaints_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Complaint)
            .where(
                Complaint.group_id == group.id, Complaint.target_telegram_id == user_id
            )
        )
        or 0
    )
    member = await session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group.id, GroupMember.user_telegram_id == user_id
        )
    )
    days = days_since(member.joined_at if member else None)
    rep = calculate_reputation(
        messages=int(totals[0]),
        warnings=all_warnings,
        mutes=mutes_count,
        bans=bans_count,
        complaints=complaints_count,
        days_in_group=days,
        deleted_messages=int(totals[1]),
        override=(member.reputation_override if member else None),
    )
    tag_names = (
        await session.scalars(
            select(MemberTag.name)
            .join(MemberTagAssignment, MemberTagAssignment.tag_id == MemberTag.id)
            .where(
                MemberTagAssignment.group_id == group.id,
                MemberTagAssignment.user_telegram_id == user_id,
            )
            .order_by(MemberTag.name)
        )
    ).all()
    notes_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ModeratorNote)
            .where(
                ModeratorNote.group_id == group.id,
                ModeratorNote.target_telegram_id == user_id,
            )
        )
        or 0
    )
    punishments = (
        await session.scalars(
            select(Punishment)
            .where(
                Punishment.group_id == group.id,
                Punishment.user_telegram_id == user_id,
                Punishment.active.is_(True),
            )
            .order_by(Punishment.created_at.desc())
        )
    ).all()
    has_mute = any(p.kind == "mute" for p in punishments)
    has_ban = any(p.kind == "ban" for p in punishments)
    active_lines = []
    for p in punishments[:5]:
        until = (
            "без срока"
            if p.ends_at is None
            else p.ends_at.astimezone(UTC).strftime("до %d.%m %H:%M UTC")
        )
        active_lines.append(f"• {escape(p.kind)} — {escape(p.reason)} · {until}")
    text = (
        panel_header("Карточка участника", public_user_token(user_id))
        + f"\n\n🏠 Группа: <b>{escape(group.title)}</b>"
        + f"\n💬 Сообщений: {int(totals[0])}"
        + f"\n🗑 Удалено: {int(totals[1])}"
        + f"\n⭐ Репутация: <b>{rep.score}/100</b>"
        + f"\n🤝 Доверие: {trust_label(member.trust_status if member and member.trust_status else rep.trust)}"
        + f"\n📅 Известен группе: {days} дн."
        + f"\n🚩 Жалоб: {complaints_count} · 📝 Заметок: {notes_count}"
        + (f"\n🏷 Теги: {escape(', '.join(tag_names[:8]))}" if tag_names else "")
        + f"\n⚠️ Активных предупреждений: {warnings}/{group.settings.warnings_limit}"
        + "\n\n<b>Активные ограничения</b>\n"
        + ("\n".join(active_lines) if active_lines else "Нет активных ограничений.")
    )
    return text, member_card_menu(group.id, user_id, has_mute, has_ban, warnings)


@router.callback_query(F.data.regexp(r"^member_find:\d+$"))
async def member_find(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    group_id = int(callback.data.split(":")[1])
    if not await accessible_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MemberCenterForm.find_member)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(
        panel_header(
            "Найти участника",
            "Отправьте Telegram ID или @username участника одним сообщением.",
        )
    )
    await callback.answer()


@router.message(MemberCenterForm.find_member, F.chat.type == "private")
async def member_find_input(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    group_id = int(data["group_id"])
    group = await accessible_group(session, group_id, message.from_user.id)
    if not group:
        await state.clear()
        await message.answer("Доступ к группе потерян.")
        return
    raw = (message.text or "").strip()
    if raw.lstrip("-").isdigit():
        user_id = int(raw)
    else:
        username = raw.lstrip("@").strip()
        user = (
            await session.scalar(
                select(User).where(func.lower(User.username) == username.casefold())
            )
            if username
            else None
        )
        if user is None:
            await message.answer(
                "Участник не найден среди известных Mimoru. Отправьте Telegram ID или @username."
            )
            return
        user_id = user.telegram_id
    await state.clear()
    text, markup = await _member_card(session, group, user_id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^member_card:\d+:-?\d+$"))
async def member_card(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text, markup = await _member_card(session, group, int(raw_user))
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^active_punishments:\d+:(warn|mute|ban)$"))
async def active_punishments(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, kind = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if kind == "warn":
        rows = (
            await session.scalars(
                select(Warning)
                .where(Warning.group_id == group.id, Warning.active.is_(True))
                .order_by(Warning.created_at.desc())
                .limit(30)
            )
        ).all()
        title = "Активные предупреждения"
    else:
        rows = (
            await session.scalars(
                select(Punishment)
                .where(
                    Punishment.group_id == group.id,
                    Punishment.kind == kind,
                    Punishment.active.is_(True),
                )
                .order_by(Punishment.created_at.desc())
                .limit(30)
            )
        ).all()
        title = "Активные муты" if kind == "mute" else "Активные блокировки"
    await callback.message.edit_text(
        panel_header(title, f"Найдено: {len(rows)}"),
        reply_markup=await active_punishments_menu(group.id, kind, rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^member_punish:\d+:-?\d+:(warn|mute|kick|ban)$"))
async def member_punish(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, redis: Redis
) -> None:
    _, raw_group, raw_user, action = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group), callback.from_user.id, for_update=True
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(raw_user)
    if user_id == callback.from_user.id:
        await callback.answer("Нельзя применить наказание к себе.", show_alert=True)
        return
    try:
        if user_id == group.owner_telegram_id or await target_is_protected(
            bot, group.telegram_chat_id, user_id
        ):
            await callback.answer(
                "Нельзя применить действие к владельцу или администратору Telegram.",
                show_alert=True,
            )
            return
    except TelegramBadRequest:
        if action not in {"ban"}:
            await callback.answer(
                "Telegram не смог проверить участника в этой группе.", show_alert=True
            )
            return
    target_name = public_user_token(user_id)
    token = secrets.token_hex(5)
    payload = {
        "group_id": group.id,
        "chat_id": group.telegram_chat_id,
        "target_id": user_id,
        "target_name": target_name,
        "moderator_id": callback.from_user.id,
        "moderator_name": public_user_token(callback.from_user.id),
        "action": action,
        "duration": None,
        "warnings_limit": group.settings.warnings_limit,
        "default_mute": group.settings.default_mute_seconds,
        "origin": "panel",
        "actor_role": "owner",
    }
    key = f"mimoru:modpending:{token}"
    await redis.setex(key, 600, json.dumps(payload, ensure_ascii=False))
    if action == "mute":
        await callback.message.edit_text(
            panel_header(
                "Мут участника",
                f"{target_name} · {group.title}\n\nВыберите срок ограничения.",
            ),
            reply_markup=moderation_duration_picker(token),
        )
        await callback.answer()
        return
    reasons = await active_reasons(session, group.id, action)
    await session.commit()
    if not reasons:
        await redis.delete(key)
        await callback.answer(
            "Для этого действия нет активных причин.", show_alert=True
        )
        return
    labels = {"warn": "предупреждения", "kick": "исключения", "ban": "блокировки"}
    await callback.message.edit_text(
        panel_header(
            "Выберите причину",
            f"{target_name} · {group.title}\n\nПричина {labels[action]}:",
        ),
        reply_markup=moderation_reason_picker(token, reasons),
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(r"^member_action:\d+:-?\d+:(unwarn|unmute|unban)$")
)
async def member_action(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    _, raw_group, raw_user, action = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    # Mutating operations require owner/for_update
    group = await owned_group(
        session, int(raw_group), callback.from_user.id, for_update=True
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(raw_user)
    try:
        if action == "unmute":
            await bot.restrict_chat_member(
                group.telegram_chat_id, user_id, permissions=UNMUTED
            )
            await deactivate_punishments(session, group.id, user_id, "mute")
            log_action(
                session,
                group.id,
                callback.from_user.id,
                user_id,
                "unmute",
                "Снято из панели",
            )
        elif action == "unban":
            await bot.unban_chat_member(
                group.telegram_chat_id, user_id, only_if_banned=True
            )
            await deactivate_punishments(session, group.id, user_id, "ban")
            log_action(
                session,
                group.id,
                callback.from_user.id,
                user_id,
                "unban",
                "Снято из панели",
            )
        else:
            warning = await session.scalar(
                select(Warning)
                .where(
                    Warning.group_id == group.id,
                    Warning.user_telegram_id == user_id,
                    Warning.active.is_(True),
                )
                .order_by(Warning.created_at.desc())
            )
            if warning is None:
                await callback.answer(
                    "Активных предупреждений уже нет.", show_alert=True
                )
                return
            warning.active = False
            log_action(
                session,
                group.id,
                callback.from_user.id,
                user_id,
                "unwarn",
                "Снято из панели",
                {"warning_id": warning.id},
            )
        await session.commit()
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await session.rollback()
        await callback.answer(
            f"Telegram не разрешил действие: {str(exc)[:120]}", show_alert=True
        )
        return
    try:
        await bot.send_message(
            group.telegram_chat_id,
            manual_action_notice(
                action=action,
                target=public_user_token(user_id),
                moderator=public_user_token(callback.from_user.id),
                reason="Снято владельцем из панели Mimoru",
                actor_role="owner",
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as notify_exc:
        structlog.get_logger().warning(
            "moderation_release_notice_failed",
            group_id=group.id,
            target_id=user_id,
            action=action,
            error=str(notify_exc),
        )
    text, markup = await _member_card(session, group, user_id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Готово")


@router.callback_query(F.data.regexp(r"^member_history:\d+:-?\d+$"))
async def member_history(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(raw_user)
    rows = (
        await session.scalars(
            select(ModerationLog)
            .where(
                ModerationLog.group_id == group.id,
                ModerationLog.target_telegram_id == user_id,
            )
            .order_by(ModerationLog.created_at.desc())
            .limit(20)
        )
    ).all()
    lines = [
        f"• {r.created_at.strftime('%d.%m %H:%M')} · <b>{escape(r.action)}</b>"
        + (f" · {escape(r.reason)}" if r.reason else "")
        for r in rows
    ]
    text = (
        panel_header("История участника", public_user_token(user_id))
        + "\n\n"
        + ("\n".join(lines) if lines else "История пуста.")
    )
    _, markup = await _member_card(session, group, user_id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^people_(active|inactive|new|suspicious):\d+$"))
async def people_segment(callback: CallbackQuery, session: AsyncSession) -> None:
    prefix, raw_group = callback.data.rsplit(":", 1)
    kind = prefix.split("_", 1)[1]
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    now = datetime.now(UTC)
    query = (
        select(GroupMember, User)
        .outerjoin(User, User.telegram_id == GroupMember.user_telegram_id)
        .where(
            GroupMember.group_id == group.id,
            GroupMember.is_present.is_(True),
            GroupMember.is_deleted_account.is_(False),
        )
    )
    if kind == "inactive":
        query = query.where(
            GroupMember.last_seen_at < now - timedelta(days=30)
        ).order_by(GroupMember.last_seen_at.asc())
        title = "Неактивные 30+ дней"
    elif kind == "new":
        query = query.where(
            GroupMember.joined_at.is_not(None),
            GroupMember.joined_at >= now - timedelta(days=7),
        ).order_by(GroupMember.joined_at.desc())
        title = "Новички · 7 дней"
    elif kind == "active":
        query = query.order_by(GroupMember.last_seen_at.desc())
        title = "Недавно активные"
    else:
        query = query.where(
            (GroupMember.joined_at >= now - timedelta(days=2))
            | (GroupMember.trust_status == "watch")
        ).order_by(GroupMember.joined_at.desc().nullslast())
        title = "Требуют внимания"
    result = (await session.execute(query.limit(30))).all()
    rows = []
    for member, user in result:
        suffix = member.last_seen_at.strftime("%d.%m") if member.last_seen_at else "—"
        name = await stored_visible_name(member.user_telegram_id)
        rows.append(
            (
                member.user_telegram_id,
                f"{name} · {suffix}",
            )
        )
    await callback.message.edit_text(
        panel_header(title, f"Показано: {len(rows)}"),
        reply_markup=people_list_menu(group.id, rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^member_notes:\d+:-?\d+$"))
async def member_notes(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    uid = int(raw_user)
    notes = (
        await session.scalars(
            select(ModeratorNote)
            .where(
                ModeratorNote.group_id == group.id,
                ModeratorNote.target_telegram_id == uid,
            )
            .order_by(ModeratorNote.created_at.desc())
            .limit(15)
        )
    ).all()
    lines = [f"• {n.created_at.strftime('%d.%m.%Y')} · {escape(n.text)}" for n in notes]
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить заметку",
                    callback_data=f"member_note_new:{group.id}:{uid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ К карточке", callback_data=f"member_card:{group.id}:{uid}"
                )
            ],
        ]
    )
    await callback.message.edit_text(
        panel_header("Заметки модераторов", public_user_token(uid))
        + "\n\n"
        + ("\n".join(lines) if lines else "Заметок пока нет."),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^member_note_new:\d+:-?\d+$"))
async def member_note_new(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    if not await owned_group(session, int(raw_group), callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MemberCenterForm.add_note)
    await state.update_data(group_id=int(raw_group), user_id=int(raw_user))
    await callback.message.edit_text(
        panel_header(
            "Новая заметка",
            "Отправьте текст заметки. Её увидят только управляющие группой.",
        )
    )
    await callback.answer()


@router.message(MemberCenterForm.add_note, F.chat.type == "private")
async def member_note_input(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    group = await owned_group(
        session, int(data["group_id"]), message.from_user.id, for_update=True
    )
    if not group:
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Заметка не может быть пустой.")
        return
    session.add(
        ModeratorNote(
            group_id=group.id,
            target_telegram_id=int(data["user_id"]),
            author_telegram_id=message.from_user.id,
            text=text[:1000],
        )
    )
    await session.commit()
    await state.clear()
    card, markup = await _member_card(session, group, int(data["user_id"]))
    await message.answer("✅ Заметка сохранена.\n\n" + card, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^member_tags:\d+:-?\d+$"))
async def member_tags(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    uid = int(raw_user)
    tags = (
        await session.scalars(
            select(MemberTag)
            .where(MemberTag.group_id == group.id)
            .order_by(MemberTag.name)
        )
    ).all()
    assigned = set(
        (
            await session.scalars(
                select(MemberTagAssignment.tag_id).where(
                    MemberTagAssignment.group_id == group.id,
                    MemberTagAssignment.user_telegram_id == uid,
                )
            )
        ).all()
    )
    await callback.message.edit_text(
        panel_header(
            "Теги участника",
            f"{public_user_token(uid)}\n\nТеги настраиваются отдельно для каждой группы.",
        ),
        reply_markup=member_tags_menu(group.id, uid, tags, assigned),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^member_tag_toggle:\d+:-?\d+:\d+$"))
async def member_tag_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    _, rg, ru, rt = callback.data.split(":")
    group = await accessible_group(session, int(rg), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    uid, tid = int(ru), int(rt)
    tag = await session.get(MemberTag, tid)
    if not tag or tag.group_id != group.id:
        await callback.answer("Тег не найден.", show_alert=True)
        return
    # Mutating operation requires owner/for_update
    group = await owned_group(session, int(rg), callback.from_user.id, for_update=True)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    uid, tid = int(ru), int(rt)
    tag = await session.get(MemberTag, tid)
    if not tag or tag.group_id != group.id:
        await callback.answer("Тег не найден.", show_alert=True)
        return
    row = await session.scalar(
        select(MemberTagAssignment).where(
            MemberTagAssignment.group_id == group.id,
            MemberTagAssignment.user_telegram_id == uid,
            MemberTagAssignment.tag_id == tid,
        )
    )
    if row:
        await session.delete(row)
    else:
        session.add(
            MemberTagAssignment(
                group_id=group.id,
                user_telegram_id=uid,
                tag_id=tid,
                assigned_by_telegram_id=callback.from_user.id,
            )
        )
    await session.commit()
    tags = (
        await session.scalars(
            select(MemberTag)
            .where(MemberTag.group_id == group.id)
            .order_by(MemberTag.name)
        )
    ).all()
    assigned = set(
        (
            await session.scalars(
                select(MemberTagAssignment.tag_id).where(
                    MemberTagAssignment.group_id == group.id,
                    MemberTagAssignment.user_telegram_id == uid,
                )
            )
        ).all()
    )
    await callback.message.edit_reply_markup(
        reply_markup=member_tags_menu(group.id, uid, tags, assigned)
    )
    await callback.answer("Обновлено")


@router.callback_query(F.data.regexp(r"^member_tag_new:\d+:-?\d+$"))
async def member_tag_new(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    _, rg, ru = callback.data.split(":")
    group = await owned_group(session, int(rg), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MemberCenterForm.add_tag)
    await state.update_data(group_id=group.id, user_id=int(ru))
    await callback.message.edit_text(
        panel_header(
            "Новый тег",
            "Введите короткое название тега, например: VIP, Старожил, Под наблюдением.",
        )
    )
    await callback.answer()


@router.message(MemberCenterForm.add_tag, F.chat.type == "private")
async def member_tag_input(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    group = await owned_group(
        session, int(data["group_id"]), message.from_user.id, for_update=True
    )
    if not group:
        await state.clear()
        return
    name = (message.text or "").strip()[:48]
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    tag = await session.scalar(
        select(MemberTag).where(
            MemberTag.group_id == group.id,
            func.lower(MemberTag.name) == name.casefold(),
        )
    )
    if not tag:
        tag = MemberTag(
            group_id=group.id, name=name, created_by_telegram_id=message.from_user.id
        )
        session.add(tag)
        await session.flush()
    exists = await session.scalar(
        select(MemberTagAssignment).where(
            MemberTagAssignment.group_id == group.id,
            MemberTagAssignment.user_telegram_id == int(data["user_id"]),
            MemberTagAssignment.tag_id == tag.id,
        )
    )
    if not exists:
        session.add(
            MemberTagAssignment(
                group_id=group.id,
                user_telegram_id=int(data["user_id"]),
                tag_id=tag.id,
                assigned_by_telegram_id=message.from_user.id,
            )
        )
    await session.commit()
    await state.clear()
    card, markup = await _member_card(session, group, int(data["user_id"]))
    await message.answer("✅ Тег добавлен.\n\n" + card, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^complaints:\d+$"))
async def complaints(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rows = (
        await session.scalars(
            select(Complaint)
            .where(Complaint.group_id == group.id, Complaint.status == "pending")
            .order_by(Complaint.created_at.desc())
            .limit(30)
        )
    ).all()
    await callback.message.edit_text(
        panel_header("Жалобы участников", f"Ожидают рассмотрения: {len(rows)}"),
        reply_markup=await complaints_menu(group.id, rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^complaint:\d+:\d+$"))
async def complaint_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    row = await session.get(Complaint, int(raw_id))
    if not group or not row or row.group_id != group.id:
        await callback.answer("Жалоба не найдена.", show_alert=True)
        return
    text = (
        panel_header("Жалоба", f"#{row.id}")
        + f"\n\n👤 На: {public_user_token(row.target_telegram_id)}\n🙋 От: {public_user_token(row.reporter_telegram_id)}\n📝 Сообщение: {escape((row.message_text or 'без текста')[:800])}\n📌 Статус: {escape(row.status)}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=complaint_review_menu(group.id, row.id, row.target_telegram_id),
    )
    await callback.answer()


async def _close_complaint(
    callback: CallbackQuery, session: AsyncSession, status: str, resolution: str
) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group), callback.from_user.id, for_update=True
    )
    row = await session.get(Complaint, int(raw_id))
    if not group or not row or row.group_id != group.id:
        await callback.answer("Жалоба не найдена.", show_alert=True)
        return
    if row.status != "pending":
        await callback.answer("Эта жалоба уже рассмотрена.", show_alert=True)
        return
    row.status = status
    row.reviewed_by_telegram_id = callback.from_user.id
    row.reviewed_at = datetime.now(UTC)
    row.resolution = resolution
    log_action(
        session,
        group.id,
        callback.from_user.id,
        row.target_telegram_id,
        f"complaint_{status}",
        resolution,
        {"complaint_id": row.id},
    )
    await session.commit()
    rows = (
        await session.scalars(
            select(Complaint)
            .where(Complaint.group_id == group.id, Complaint.status == "pending")
            .order_by(Complaint.created_at.desc())
            .limit(30)
        )
    ).all()
    await callback.message.edit_text(
        panel_header("Жалобы участников", f"Ожидают рассмотрения: {len(rows)}"),
        reply_markup=await complaints_menu(group.id, rows),
    )
    await callback.answer("Жалоба закрыта")


@router.callback_query(F.data.regexp(r"^complaint_close:\d+:\d+$"))
async def complaint_close(callback: CallbackQuery, session: AsyncSession) -> None:
    await _close_complaint(callback, session, "resolved", "Рассмотрено из панели")


@router.callback_query(F.data.regexp(r"^complaint_reject:\d+:\d+$"))
async def complaint_reject(callback: CallbackQuery, session: AsyncSession) -> None:
    await _close_complaint(callback, session, "rejected", "Отклонено из панели")
