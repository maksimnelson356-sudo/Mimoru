from __future__ import annotations

from html import escape
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemberTag, MemberTagAssignment, ModerationLog, ModeratorNote
from app.handlers.member_center import MemberCenterForm, _member_card, owned_group
from app.services.access import accessible_group
from app.services.public_identity import public_user_token
from app.services.ui import panel_header

router = Router(name=__name__)


def _title(callback: CallbackQuery) -> str:
    first = ((callback.message.text if callback.message else "") or "").splitlines()[0].strip()
    prefix = "🟣 Mimoru · "
    return first[len(prefix):] if first.startswith(prefix) else first


def _source_from_screen(callback: CallbackQuery) -> str:
    title = _title(callback)
    mapping = {
        "Недавно активные": "a",
        "Неактивные 30+ дней": "i",
        "Новички · 7 дней": "n",
        "Требуют внимания": "s",
        "Активные предупреждения": "w",
        "Активные муты": "m",
        "Активные блокировки": "b",
    }
    if title in mapping:
        return mapping[title]
    if title == "Жалоба":
        match = re.search(r"#(\d+)", callback.message.text or "")
        if match:
            return f"q{match.group(1)}"
    return "u"


def _back_for_source(source: str, group_id: int) -> tuple[str, str]:
    mapping = {
        "a": (f"people_active:{group_id}", "◀️ К активным"),
        "i": (f"people_inactive:{group_id}", "◀️ К неактивным"),
        "n": (f"people_new:{group_id}", "◀️ К новичкам"),
        "s": (f"people_suspicious:{group_id}", "◀️ К списку"),
        "w": (f"active_punishments:{group_id}:warn", "◀️ К предупреждениям"),
        "m": (f"active_punishments:{group_id}:mute", "◀️ К мутам"),
        "b": (f"active_punishments:{group_id}:ban", "◀️ К блокировкам"),
    }
    if source in mapping:
        return mapping[source]
    if source.startswith("q") and source[1:].isdigit():
        return f"complaint:{group_id}:{source[1:]}", "◀️ К жалобе"
    return f"group_section:{group_id}:members", "◀️ К участникам"


def _contextual_member_markup(markup: InlineKeyboardMarkup, source: str, group_id: int, user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        updated: list[InlineKeyboardButton] = []
        for button in row:
            data = button.callback_data
            if data == f"member_history:{group_id}:{user_id}":
                data = f"mh:{source}:{group_id}:{user_id}"
            elif data == f"member_tags:{group_id}:{user_id}":
                data = f"mt:{source}:{group_id}:{user_id}"
            elif data == f"member_notes:{group_id}:{user_id}":
                data = f"mn:{source}:{group_id}:{user_id}"
            updated.append(button.model_copy(update={"callback_data": data}) if data != button.callback_data else button)
        rows.append(updated)
    back_callback, back_text = _back_for_source(source, group_id)
    if rows:
        rows[-1] = [InlineKeyboardButton(text=back_text, callback_data=back_callback)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_card(callback: CallbackQuery, session: AsyncSession, source: str, group_id: int, user_id: int) -> None:
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text, markup = await _member_card(session, group, user_id)
    await callback.message.edit_text(
        text,
        reply_markup=_contextual_member_markup(markup, source, group.id, user_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^member_card:\d+:-?\d+$"))
async def member_card_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    await _render_card(callback, session, _source_from_screen(callback), int(raw_group), int(raw_user))


@router.callback_query(F.data.regexp(r"^mc:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+$"))
async def member_card_explicit(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_group, raw_user = callback.data.split(":")
    await _render_card(callback, session, source, int(raw_group), int(raw_user))


@router.callback_query(F.data.regexp(r"^mh:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+$"))
async def member_history_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_group, raw_user = callback.data.split(":")
    group_id, user_id = int(raw_group), int(raw_user)
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rows = list((await session.scalars(
        select(ModerationLog)
        .where(ModerationLog.group_id == group.id, ModerationLog.target_telegram_id == user_id)
        .order_by(ModerationLog.created_at.desc())
        .limit(20)
    )).all())
    lines = [
        f"• {row.created_at:%d.%m %H:%M} · {escape(row.action)}"
        + (f" · {escape(row.reason)}" if row.reason else "")
        for row in rows
    ]
    await callback.message.edit_text(
        panel_header("История участника", public_user_token(user_id))
        + "\n\n" + ("\n".join(lines) if lines else "История пуста."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад к карточке", callback_data=f"mc:{source}:{group.id}:{user_id}")
        ]]),
    )
    await callback.answer()


async def _render_tags(callback: CallbackQuery, session: AsyncSession, source: str, group_id: int, user_id: int) -> None:
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    tags = list((await session.scalars(
        select(MemberTag).where(MemberTag.group_id == group.id).order_by(MemberTag.name)
    )).all())
    assigned = set((await session.scalars(
        select(MemberTagAssignment.tag_id).where(
            MemberTagAssignment.group_id == group.id,
            MemberTagAssignment.user_telegram_id == user_id,
        )
    )).all())
    rows = []
    for tag in tags[:20]:
        mark = "✅" if tag.id in assigned else "▫️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {tag.name}",
            callback_data=f"mtt:{source}:{group.id}:{user_id}:{tag.id}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Новый тег", callback_data=f"mtn:{source}:{group.id}:{user_id}")])
    rows.append([InlineKeyboardButton(text="◀️ К карточке", callback_data=f"mc:{source}:{group.id}:{user_id}")])
    await callback.message.edit_text(
        panel_header("Теги участника", f"{public_user_token(user_id)}\n\nТеги настраиваются отдельно для каждой группы."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.regexp(r"^mt:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+$"))
async def member_tags_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_group, raw_user = callback.data.split(":")
    await _render_tags(callback, session, source, int(raw_group), int(raw_user))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mtt:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+:\d+$"))
async def member_tag_toggle_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_group, raw_user, raw_tag = callback.data.split(":")
    group_id, user_id, tag_id = int(raw_group), int(raw_user), int(raw_tag)
    group = await owned_group(session, group_id, callback.from_user.id, for_update=True)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    tag = await session.get(MemberTag, tag_id)
    if tag is None or tag.group_id != group.id:
        await callback.answer("Тег не найден.", show_alert=True)
        return
    row = await session.scalar(select(MemberTagAssignment).where(
        MemberTagAssignment.group_id == group.id,
        MemberTagAssignment.user_telegram_id == user_id,
        MemberTagAssignment.tag_id == tag_id,
    ))
    if row:
        await session.delete(row)
    else:
        session.add(MemberTagAssignment(
            group_id=group.id,
            user_telegram_id=user_id,
            tag_id=tag.id,
            assigned_by_telegram_id=callback.from_user.id,
        ))
    await session.commit()
    await _render_tags(callback, session, source, group.id, user_id)
    await callback.answer("Обновлено")


@router.callback_query(F.data.regexp(r"^mtn:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+$"))
async def member_tag_new_context(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, source, raw_group, raw_user = callback.data.split(":")
    group_id, user_id = int(raw_group), int(raw_user)
    if not await owned_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MemberCenterForm.add_tag)
    await state.update_data(
        group_id=group_id,
        user_id=user_id,
        member_source=source,
        _cancel_callback=f"mt:{source}:{group_id}:{user_id}",
    )
    await callback.message.edit_text(panel_header("Новый тег", "Введите короткое название тега, например: VIP, Старожил, Под наблюдением."))
    await callback.answer()


@router.message(MemberCenterForm.add_tag, F.chat.type == "private")
async def member_tag_input_context(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    group_id, user_id = int(data["group_id"]), int(data["user_id"])
    source = str(data.get("member_source") or "u")
    group = await owned_group(session, group_id, message.from_user.id, for_update=True)
    if not group:
        await state.clear()
        return
    name = (message.text or "").strip()[:48]
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    tag = await session.scalar(select(MemberTag).where(
        MemberTag.group_id == group.id,
        func.lower(MemberTag.name) == name.casefold(),
    ))
    if not tag:
        tag = MemberTag(group_id=group.id, name=name, created_by_telegram_id=message.from_user.id)
        session.add(tag)
        await session.flush()
    exists = await session.scalar(select(MemberTagAssignment).where(
        MemberTagAssignment.group_id == group.id,
        MemberTagAssignment.user_telegram_id == user_id,
        MemberTagAssignment.tag_id == tag.id,
    ))
    if not exists:
        session.add(MemberTagAssignment(
            group_id=group.id,
            user_telegram_id=user_id,
            tag_id=tag.id,
            assigned_by_telegram_id=message.from_user.id,
        ))
    await session.commit()
    await state.clear()
    text, markup = await _member_card(session, group, user_id)
    await message.answer(
        "✅ Тег добавлен.\n\n" + text,
        reply_markup=_contextual_member_markup(markup, source, group.id, user_id),
    )


async def _render_notes(callback: CallbackQuery, session: AsyncSession, source: str, group_id: int, user_id: int) -> None:
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    notes = list((await session.scalars(
        select(ModeratorNote)
        .where(ModeratorNote.group_id == group.id, ModeratorNote.target_telegram_id == user_id)
        .order_by(ModeratorNote.created_at.desc())
        .limit(15)
    )).all())
    lines = [f"• {note.created_at:%d.%m.%Y} · {escape(note.text)}" for note in notes]
    await callback.message.edit_text(
        panel_header("Заметки модераторов", public_user_token(user_id))
        + "\n\n" + ("\n".join(lines) if lines else "Заметок пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить заметку", callback_data=f"mnn:{source}:{group.id}:{user_id}")],
            [InlineKeyboardButton(text="◀️ К карточке", callback_data=f"mc:{source}:{group.id}:{user_id}")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^mn:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+$"))
async def member_notes_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_group, raw_user = callback.data.split(":")
    await _render_notes(callback, session, source, int(raw_group), int(raw_user))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mnn:(a|i|n|s|w|m|b|u|q\d+):\d+:-?\d+$"))
async def member_note_new_context(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, source, raw_group, raw_user = callback.data.split(":")
    group_id, user_id = int(raw_group), int(raw_user)
    if not await owned_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MemberCenterForm.add_note)
    await state.update_data(
        group_id=group_id,
        user_id=user_id,
        member_source=source,
        _cancel_callback=f"mn:{source}:{group_id}:{user_id}",
    )
    await callback.message.edit_text(panel_header("Новая заметка", "Отправьте текст заметки. Её увидят только управляющие группой."))
    await callback.answer()


@router.message(MemberCenterForm.add_note, F.chat.type == "private")
async def member_note_input_context(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    group_id, user_id = int(data["group_id"]), int(data["user_id"])
    source = str(data.get("member_source") or "u")
    group = await owned_group(session, group_id, message.from_user.id, for_update=True)
    if not group:
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Заметка не может быть пустой.")
        return
    session.add(ModeratorNote(
        group_id=group.id,
        target_telegram_id=user_id,
        author_telegram_id=message.from_user.id,
        text=text[:1000],
    ))
    await session.commit()
    await state.clear()
    card, markup = await _member_card(session, group, user_id)
    await message.answer(
        "✅ Заметка сохранена.\n\n" + card,
        reply_markup=_contextual_member_markup(markup, source, group.id, user_id),
    )
