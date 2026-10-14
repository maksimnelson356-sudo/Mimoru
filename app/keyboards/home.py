from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.safety import warning_expiry_label


def _status(enabled: bool) -> str:
    return "✅" if enabled else "❌"


def home_menu(is_service_owner: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_service_owner:
        rows.append([InlineKeyboardButton(text="👑 Управление Mimoru", callback_data="service:home")])
    rows.extend(
        [
            [InlineKeyboardButton(text="🏠 Управлять моими группами", callback_data="panel:groups")],
            [InlineKeyboardButton(text="💎 Тарифы и подписка", callback_data="panel:plans")],
            [
                InlineKeyboardButton(text="📢 Реклама", callback_data="ads:home"),
                InlineKeyboardButton(text="💬 Поддержка", callback_data="panel:support"),
            ],
            [
                InlineKeyboardButton(text="ℹ️ О боте Mimoru", callback_data="panel:about"),
                InlineKeyboardButton(text="❓ Как пользоваться", callback_data="panel:commands"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Клиенты", callback_data="service:clients"),
            InlineKeyboardButton(text="🏠 Все группы", callback_data="service:groups"),
        ],
        [
            InlineKeyboardButton(text="💎 Подписки и TRIAL", callback_data="service:subscriptions"),
            InlineKeyboardButton(text="💳 Платежи", callback_data="service:billing"),
        ],
        [
            InlineKeyboardButton(text="📢 Реклама", callback_data="service:ads"),
            InlineKeyboardButton(text="📣 Рассылка по группам", callback_data="service:broadcast"),
        ],
        [
            InlineKeyboardButton(text="💬 Обращения", callback_data="service:tickets"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="service:stats"),
        ],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="panel:home")],
    ])


def cancel_input_menu(callback_data: str = "panel:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отменить ввод", callback_data=callback_data)],
    ])


def reply_cancel_menu(prompt_message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отменить ввод", callback_data=f"reply_cancel:{prompt_message_id}")],
    ])


def group_home_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡 Защита от спама", callback_data=f"group_section:{group_id}:protection"),
            InlineKeyboardButton(text="👮 Модерация", callback_data=f"group_section:{group_id}:moderation"),
        ],
        [
            InlineKeyboardButton(text="👥 Участники", callback_data=f"group_section:{group_id}:members"),
            InlineKeyboardButton(text="📊 Статистика группы", callback_data=f"group_section:{group_id}:analytics"),
        ],
        [
            InlineKeyboardButton(text="📝 Контент и правила", callback_data=f"group_section:{group_id}:content"),
            InlineKeyboardButton(text="⚙️ Настройки группы", callback_data=f"group_section:{group_id}:settings"),
        ],
        [
            InlineKeyboardButton(text="🤖 Автоматизация", callback_data=f"automation:{group_id}"),
            InlineKeyboardButton(text="💎 Тариф группы", callback_data=f"plan:{group_id}"),
        ],
        [
            InlineKeyboardButton(text="🩺 Диагностика", callback_data=f"health_direct:{group_id}"),
            InlineKeyboardButton(text="📢 Реклама группы", callback_data=f"ads:placement:{group_id}"),
        ],
        [InlineKeyboardButton(text="⛔ Отключить группу", callback_data=f"group_disconnect:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к моим группам", callback_data="panel:groups")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")],
    ])


def protection_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{_status(s.antiflood_enabled)} Антифлуд", callback_data=f"toggle:{gid}:antiflood"),
            InlineKeyboardButton(text=f"{_status(s.repeats_enabled)} Повторы", callback_data=f"toggle:{gid}:repeats"),
        ],
        [InlineKeyboardButton(text="🌊 Настроить антифлуд", callback_data=f"setting_num:{gid}:antiflood")],
        [
            InlineKeyboardButton(text=f"{'🚫' if not s.links_enabled else '✅'} Ссылки", callback_data=f"toggle:{gid}:links"),
            InlineKeyboardButton(text=f"{_status(s.caps_enabled)} Капс", callback_data=f"toggle:{gid}:caps"),
        ],
        [
            InlineKeyboardButton(text=f"{_status(s.captcha_enabled)} Капча", callback_data=f"toggle:{gid}:captcha"),
            InlineKeyboardButton(text=f"{_status(s.newcomer_quarantine_enabled)} Карантин", callback_data=f"toggle:{gid}:quarantine"),
        ],
        [
            InlineKeyboardButton(text=f"{_status(s.edit_protection_enabled)} Редактирование", callback_data=f"toggle:{gid}:edit"),
            InlineKeyboardButton(text=f"{_status(s.mention_filter_enabled)} Упоминания", callback_data=f"toggle:{gid}:mentions"),
        ],
        [
            InlineKeyboardButton(text=f"{_status(s.sender_chat_filter_enabled)} Sender Chat", callback_data=f"toggle:{gid}:sender"),
            InlineKeyboardButton(text=f"{_status(s.anti_raid_enabled)} Anti-Raid", callback_data=f"toggle:{gid}:raid"),
        ],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{gid}")],
    ])


def moderation_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚩 Жалобы", callback_data=f"complaints:{group_id}")],
        [InlineKeyboardButton(text="📌 Причины наказаний", callback_data=f"reasons:{group_id}")],
        [InlineKeyboardButton(text="🔇 Мут по умолчанию", callback_data=f"setting_num:{group_id}:defaultmute")],
        [
            InlineKeyboardButton(text="👮 Модераторы", callback_data=f"roles:{group_id}"),
            InlineKeyboardButton(text="📋 Журнал", callback_data=f"logs:{group_id}"),
        ],
        [InlineKeyboardButton(text="ℹ️ Как модерировать", callback_data=f"moderation_help:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


def members_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти участника", callback_data=f"member_find:{group_id}")],
        [
            InlineKeyboardButton(text="🏆 Активные", callback_data=f"people_active:{group_id}"),
            InlineKeyboardButton(text="😴 Неактивные", callback_data=f"people_inactive:{group_id}"),
        ],
        [
            InlineKeyboardButton(text="🆕 Новички", callback_data=f"people_new:{group_id}"),
            InlineKeyboardButton(text="🤖 Подозрительные", callback_data=f"people_suspicious:{group_id}"),
        ],
        [
            InlineKeyboardButton(text="⚠️ Предупреждения", callback_data=f"active_punishments:{group_id}:warn"),
            InlineKeyboardButton(text="🔇 Муты", callback_data=f"active_punishments:{group_id}:mute"),
        ],
        [InlineKeyboardButton(text="⛔ Блокировки", callback_data=f"active_punishments:{group_id}:ban")],
        [InlineKeyboardButton(text="🪦 Удалённые аккаунты", callback_data=f"deleted_accounts:{group_id}")],
        [InlineKeyboardButton(text="🏆 Активность участников", callback_data=f"members_stats:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


def content_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Запрещённые слова и фразы", callback_data=f"words:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


def settings_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    status = lambda value: "✅" if value else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{status(s.welcome_enabled)} Приветствие", callback_data=f"toggle:{gid}:welcome")],
        [
            InlineKeyboardButton(text=f"{status(s.night_mode_enabled)} Ночной режим", callback_data=f"toggle:{gid}:night"),
            InlineKeyboardButton(text=f"{status(s.join_requests_enabled)} Заявки", callback_data=f"toggle:{gid}:joinreq"),
        ],
        [InlineKeyboardButton(text="📝 Приветствие и правила", callback_data=f"settings_detail:{gid}")],
        [InlineKeyboardButton(text="🚀 Мастер настройки", callback_data=f"setup:{gid}:start")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{gid}")],
    ])


def settings_detail_menu(group) -> InlineKeyboardMarkup:
    gid = group.id
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Текст приветствия", callback_data=f"setting_text:{gid}:welcome")],
        [InlineKeyboardButton(text="📜 Правила группы", callback_data=f"setting_text:{gid}:rules")],
        [InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data=f"group_section:{gid}:settings")],
    ])


def channels_admin_menu(group_id: int, channels) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 {channel}", callback_data=f"channel_remove:{group_id}:{idx}")]
        for idx, channel in enumerate(channels[:50])
    ]
    rows += [
        [InlineKeyboardButton(text="➕ Добавить обязательный канал", callback_data=f"channel_add:{group_id}")],
        [InlineKeyboardButton(text="◀️ К рекламе группы", callback_data=f"ads:placement:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def automation_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    schedule_names = {"off": "выкл", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{_status(s.automation_enabled)} Автоматизация", callback_data=f"automation_toggle:{gid}")],
        [InlineKeyboardButton(text=f"🪦 Очистка аккаунтов: {schedule_names.get(s.deleted_cleanup_schedule, 'выкл')}", callback_data=f"automation_cleanup:{gid}")],
        [InlineKeyboardButton(text=f"⚠️ Срок предупреждений: {warning_expiry_label(s.warning_expire_hours)}", callback_data=f"automation_warnings:{gid}")],
        [InlineKeyboardButton(text=f"⚠️ Лимит предупреждений: {s.warnings_limit}", callback_data=f"automation_warning_limit:{gid}")],
        [InlineKeyboardButton(text="👋 Сценарий новичка", callback_data=f"automation_newcomer:{gid}")],
        [InlineKeyboardButton(text="📋 Журнал автоматизации", callback_data=f"automation_logs:{gid}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{gid}")],
    ])


def operations_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🩺 Диагностика и права", callback_data=f"ops_diag:{group_id}")],
        [InlineKeyboardButton(text="📋 Единый журнал", callback_data=f"ops_logs:{group_id}")],
        [InlineKeyboardButton(text="📦 Резервные копии", callback_data=f"ops_backup:{group_id}")],
        [InlineKeyboardButton(text="⭐ Состояние группы", callback_data=f"health_from_ops:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


def group_health_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"health:{group_id}")],
        [InlineKeyboardButton(text="🚀 Мастер настройки", callback_data=f"setup:{group_id}:start")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


HOME_HINT = (
    "Если вы здесь впервые, начните с «Управлять моими группами».\n\n"
    "🏠 Группы — выберите группу и управляйте её защитой, модерацией, участниками, статистикой, контентом и настройками.\n"
    "💎 Тарифы — возможности и подписка.\n"
    "📢 Реклама — размещения и обязательная подписка на каналы.\n"
    "💬 Поддержка — обращения по проблемам.\n"
    "❓ Как пользоваться — короткая инструкция по Mimoru."
)
