from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RequiredChannel
from app.keyboards.home import channels_admin_menu, content_menu, settings_menu
from app.services.access import accessible_group
from app.services.group_health import calculate_group_health
from app.services.ui import panel_header

router = Router(name=__name__)


@router.callback_query(F.data.regexp(r"^channels:\d+$"))
async def required_subscriptions(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    channels = (
        await session.scalars(
            select(RequiredChannel.channel_username)
            .where(
                RequiredChannel.group_id == group.id, RequiredChannel.active.is_(True)
            )
            .order_by(RequiredChannel.channel_username)
        )
    ).all()
    text = (
        panel_header(
            "Обязательная подписка",
            "Новые участники должны быть подписаны на указанные каналы. Добавьте канал или удалите ненужный.",
        )
        + "\n\n"
        + (
            "\n".join(f"• {channel}" for channel in channels)
            if channels
            else "Каналы пока не добавлены."
        )
    )
    await callback.message.edit_text(
        text, reply_markup=channels_admin_menu(group.id, channels)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^group_section:\d+:(content|settings)$"))
async def simplified_group_section(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    _, raw_group_id, section = callback.data.split(":")
    group = await accessible_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if section == "content":
        text = panel_header(
            "Контент и правила", "Запрещённые слова и фразы для этой группы."
        )
        keyboard = content_menu(group.id)
    else:
        text = panel_header("Настройки группы", "Основные параметры поведения Mimoru.")
        keyboard = settings_menu(group)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def _health_keyboard(group_id: int, source: str) -> InlineKeyboardMarkup:
    back_callback = f"ops:{group_id}" if source == "ops" else f"group:{group_id}"
    rows = [
        [
            InlineKeyboardButton(
                text="🔄 Проверить снова",
                callback_data=f"health_{'from_ops' if source == 'ops' else 'direct'}:{group_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🛡 Защита", callback_data=f"group_section:{group_id}:protection"
            ),
            InlineKeyboardButton(
                text="⚙️ Настройки", callback_data=f"group_section:{group_id}:settings"
            ),
        ],
    ]
    if source == "group":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧰 Расширенная диагностика", callback_data=f"ops:{group_id}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_health(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    group_id: int,
    source: str,
) -> None:
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    health = await calculate_group_health(bot, session, group)
    lines = [
        f"{'✅' if health.bot_is_admin else '❌'} Mimoru — администратор",
        f"{'✅' if health.can_delete_messages else '❌'} Удаление сообщений",
        f"{'✅' if health.can_restrict_members else '❌'} Ограничение участников",
        f"{'✅' if health.can_invite_users else '⚪'} Управление приглашениями",
        "",
        f"Общая оценка: {health.score}/100 · {health.level}",
    ]
    if health.recommendations:
        lines += ["", "Что можно улучшить:"] + [
            f"• {item}" for item in health.recommendations
        ]
    else:
        lines += ["", "✅ Критичных проблем не найдено."]
    await callback.message.edit_text(
        panel_header("Диагностика группы", group.title) + "\n\n" + "\n".join(lines),
        reply_markup=_health_keyboard(group.id, source),
    )
    await callback.answer("Проверено")


@router.callback_query(F.data.regexp(r"^health_direct:\d+$"))
async def direct_health(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    await _render_health(
        callback, bot, session, int(callback.data.split(":")[-1]), "group"
    )


@router.callback_query(F.data.regexp(r"^health_from_ops:\d+$"))
async def health_from_ops(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    await _render_health(
        callback, bot, session, int(callback.data.split(":")[-1]), "ops"
    )
