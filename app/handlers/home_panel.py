from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.db.rank_models import RankAssignment
from app.keyboards.home import HOME_HINT, group_home_menu, home_menu
from app.services.access import accessible_group, is_service_owner
from app.services.plans import effective_plan
from app.services.telegram_admins import sync_telegram_administrators
from app.services.ui import panel_header

router = Router(name=__name__)


HOME_TEXT = panel_header(
    "Главное меню",
    "Управление Mimoru без необходимости помнить команды.\n\n" + HOME_HINT,
)

HELP_TEXT = (
    panel_header("Как пользоваться Mimoru", "Основные действия доступны кнопками. Команды нужны в основном для быстрой модерации прямо в группе.")
    + "\n\nЕсли вы только начали\n"
    "1. Добавьте Mimoru администратором Telegram-группы.\n"
    "2. В самой группе напишите «подключить».\n"
    "3. В личном чате откройте «🏠 Управлять моими группами».\n"
    "4. Выберите группу — внутри будут защита, участники, модерация, статистика, контент и настройки.\n\n"
    "Быстрая модерация в группе\n"
    "Ответьте на сообщение участника: пред, мут 2ч или бан. "
    "Mimoru предложит причину кнопками перед выполнением действия.\n\n"
    "Если Mimoru просит прислать текст\n"
    "Используйте кнопку «✖️ Отменить ввод», которая появляется рядом с запросом текста. "
    "Нажатие другой кнопки навигации также отменяет незавершённый ввод."
)

ABOUT_TEXT = (
    panel_header("О боте Mimoru", "Полнофункциональный инструмент для управления Telegram-сообществами.")
    + "\n\n<b>🛡 Защита от спама и флуда</b>\n"
    "• Антифлуд с настраиваемыми лимитами (сообщений за интервал)\n"
    "• Блокировка ссылок, капс-лока, повторов, стикеров, голосовых, пересылок\n"
    "• Капча для новичков и карантин новых участников\n"
    "• Защита от редактирования сообщений (anti-edit)\n"
    "• Фильтр упоминаний и sender-чатов\n"
    "• Anti-Raid — массовая защита при атаках\n"
    "• Запрещённые слова и фразы с гибкими действиями\n\n"
    "<b>👮 Модерация</b>\n"
    "• Предупреждения, муты, кики, баны — одной командой или кнопкой\n"
    "• Система причин наказаний с кастомными действиями\n"
    "• Роли модераторов: старший, модератор, помощник с гранулярными правами\n"
    "• Журнал действий с поиском и фильтрами\n"
    "• Жалобы участников с уведомлением владельца\n\n"
    "<b>👥 Управление участниками</b>\n"
    "• Поиск участников, карточки с историей, тегами, заметками\n"
    "• Списки: активные, неактивные, новички, подозрительные\n"
    "• Активные наказания с пагинацией\n"
    "• Очистка удалённых аккаунтов (по расписанию или вручную)\n\n"
    "<b>📊 Статистика и аналитика</b>\n"
    "• Сообщения, активные участники, топ участников за день/неделю/месяц\n"
    "• Динамика роста, отчёты по модерации, ежедневные отчёты владельцу\n\n"
    "<b>📝 Контент и правила</b>\n"
    "• Приветствие с переменными {имя}, {группа}\n"
    "• Правила группы\n"
    "• Обязательная подписка на каналы для вступления/активности\n"
    "• Автоответы (триггеры) на ключевые слова\n\n"
    "<b>⚙️ Автоматизация</b>\n"
    "• Еженедельная/ежемесячная очистка удалённых аккаунтов\n"
    "• Автоудаление истёкших предупреждений\n"
    "• Лимит предупреждений → авто-мут\n"
    "• Сценарии новичка: базовый / стандартный / строгий\n"
    "• Журнал автоматизации\n\n"
    "<b>📢 Реклама и монетизация</b>\n"
    "• Рекламные размещения в группах\n"
    "• Обязательные каналы (force-subscribe)\n"
    "• Маркетплейс рекламы для покупателей\n\n"
    "<b>💎 Тарифы</b>\n"
    "• FREE — базовая модерация и защита\n"
    "• STANDARD (250⭐/мес) — расширенная защита, аналитика, реклама, автоответы\n"
    "• PRO (500⭐/мес) — максимум лимитов, приоритетная поддержка, все коммерческие фичи\n\n"
    "<b>🎮 Игры и развлечения</b>\n"
    "Викторины, Крокодил, Мафия, Шпион, Морской бой, Карты, Словарные игры, Рулетка, Арена\n\n"
    "<b>🔧 Дополнительно</b>\n"
    "• Панель владельца сервиса (клиенты, группы, платежи, рассылки)\n"
    "• Поддержка через тикеты\n"
    "• Настройка ночного режима, slow-mode\n"
    "• Резервные копии настроек группы\n"
    "• Мастер быстрой настройки группы\n"
)


async def _clear_pending_input(state: FSMContext) -> None:
    if await state.get_state():
        await state.clear()



@router.message(
    F.chat.type == "private",
    F.text.casefold().in_({"панель", "меню", "настройки", "мои группы", "главное меню"}),
)
async def open_guided_home(message: Message, state: FSMContext) -> None:
    await _clear_pending_input(state)
    await message.answer(HOME_TEXT, reply_markup=home_menu(is_service_owner(message.from_user.id)))


@router.callback_query(F.data == "panel:home")
async def guided_home(callback: CallbackQuery, state: FSMContext) -> None:
    await _clear_pending_input(state)
    await callback.message.edit_text(HOME_TEXT, reply_markup=home_menu(is_service_owner(callback.from_user.id)))
    await callback.answer()


@router.callback_query(F.data == "panel:commands")
async def guided_help(callback: CallbackQuery, state: FSMContext) -> None:
    await _clear_pending_input(state)
    await callback.message.edit_text(HELP_TEXT, reply_markup=home_menu(is_service_owner(callback.from_user.id)))
    await callback.answer()


@router.callback_query(F.data == "panel:about")
async def guided_about(callback: CallbackQuery, state: FSMContext) -> None:
    await _clear_pending_input(state)
    await callback.message.edit_text(ABOUT_TEXT, reply_markup=home_menu(is_service_owner(callback.from_user.id)))
    await callback.answer()


@router.callback_query(F.data == "panel:my_stats")
async def choose_group_statistics(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await _clear_pending_input(state)
    query = select(Group).where(Group.is_active.is_(True))
    if not is_service_owner(callback.from_user.id):
        admin_subq = (
            select(RankAssignment.id)
            .where(
                RankAssignment.group_id == Group.id,
                RankAssignment.user_telegram_id == callback.from_user.id,
                RankAssignment.active.is_(True),
                RankAssignment.rank_code.in_({"deputy_owner", "chief_admin", "chat_admin"}),
            )
            .exists()
        )
        query = query.where(
            or_(
                Group.owner_telegram_id == callback.from_user.id,
                admin_subq,
            )
        )
    groups = (await session.scalars(query.order_by(Group.title))).all()
    rows = [
        [InlineKeyboardButton(text=f"📊 {group.title[:44]}", callback_data=f"group_section:{group.id}:analytics")]
        for group in groups
    ]
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")])
    text = panel_header(
        "Статистика по группам",
        "Выберите группу. Статистика каждой группы показывается отдельно.",
    )
    if not groups:
        text += "\n\nПодключённых групп пока нет."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^group:\d+$"))
async def guided_group_home(callback: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    await _clear_pending_input(state)
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return

    # Telegram is authoritative for administrator status. Refresh the list here
    # so administrators who existed before Mimoru was installed are imported
    # into known members and displayed with their real Telegram role.
    await sync_telegram_administrators(bot, session, group)
    await session.commit()

    await callback.message.edit_text(
        panel_header(
            group.title,
            "Выберите, что хотите сделать с этой группой.\n\n"
            "🛡 Защита — спам и фильтры.\n"
            "👮 Модерация — наказания, причины и роли.\n"
            "👥 Участники — поиск, карточки и ограничения.\n"
            "📊 Статистика — показатели именно этой группы.\n"
            "📝 Контент — слова и правила.\n"
            "⚙️ Настройки — поведение Mimoru.\n"
            f"\nТариф группы: {effective_plan(group).upper()}" + (f" · до {group.plan_expires_at:%d.%m.%Y}" if group.plan_expires_at else ""),
        ),
        reply_markup=group_home_menu(group.id),
    )
    await callback.answer()
