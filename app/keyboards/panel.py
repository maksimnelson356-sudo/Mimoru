from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.public_identity import stored_visible_name


def _status(enabled: bool) -> str:
    return "✅" if enabled else "❌"


def main_menu(is_service_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🏠 Мои группы", callback_data="panel:groups"),
            InlineKeyboardButton(text="📊 Аналитика", callback_data="panel:my_stats"),
        ],
        [
            InlineKeyboardButton(text="📢 Реклама", callback_data="ads:home"),
            InlineKeyboardButton(text="💎 Подписка", callback_data="panel:plans"),
        ],
        [
            InlineKeyboardButton(text="💬 Поддержка", callback_data="panel:support"),
            InlineKeyboardButton(text="⚙️ Помощь", callback_data="panel:commands"),
        ],
    ]
    if is_service_owner:
        rows.insert(0, [InlineKeyboardButton(text="👑 Панель Mimoru", callback_data="service:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def groups_menu(groups) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🏠 {g.title[:44]}", callback_data=f"group:{g.id}")] for g in groups]
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_menu(group) -> InlineKeyboardMarkup:
    gid = group.id
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡 Защита", callback_data=f"group_section:{gid}:protection"),
            InlineKeyboardButton(text="👮 Модерация", callback_data=f"group_section:{gid}:moderation"),
        ],
        [
            InlineKeyboardButton(text="👥 Участники", callback_data=f"group_section:{gid}:members"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"group_section:{gid}:analytics"),
        ],
        [
            InlineKeyboardButton(text="💬 Контент", callback_data=f"group_section:{gid}:content"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"group_section:{gid}:settings"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Автоматизация", callback_data=f"automation:{gid}"),
            InlineKeyboardButton(text="💎 Подписка", callback_data=f"plan:{gid}"),
        ],
        [
            InlineKeyboardButton(text="🧰 Операции", callback_data=f"ops:{gid}"),
            InlineKeyboardButton(text="📢 Реклама", callback_data=f"ads:placement:{gid}"),
        ],
        [InlineKeyboardButton(text="◀️ К группам", callback_data="panel:groups")],
    ])


def analytics_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сегодня", callback_data=f"stats:{group_id}:1"),
            InlineKeyboardButton(text="7 дней", callback_data=f"stats:{group_id}:7"),
            InlineKeyboardButton(text="30 дней", callback_data=f"stats:{group_id}:30"),
        ],
        [
            InlineKeyboardButton(text="👥 Активность", callback_data=f"analytics:{group_id}:activity"),
            InlineKeyboardButton(text="🛡 Модерация", callback_data=f"analytics:{group_id}:moderation"),
        ],
        [
            InlineKeyboardButton(text="📈 Динамика", callback_data=f"analytics:{group_id}:growth"),
            InlineKeyboardButton(text="📬 Отчёты", callback_data=f"analytics:{group_id}:reports"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{group_id}")],
    ])


def analytics_back(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К аналитике", callback_data=f"group_section:{group_id}:analytics")],
    ])


def report_settings_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if s.reports_enabled else '❌'} Ежедневный отчёт",
            callback_data=f"analytics_report_toggle:{gid}",
        )],
        [
            InlineKeyboardButton(text="06:00", callback_data=f"analytics_report_hour:{gid}:6"),
            InlineKeyboardButton(text="08:00", callback_data=f"analytics_report_hour:{gid}:8"),
            InlineKeyboardButton(text="12:00", callback_data=f"analytics_report_hour:{gid}:12"),
        ],
        [
            InlineKeyboardButton(text="18:00", callback_data=f"analytics_report_hour:{gid}:18"),
            InlineKeyboardButton(text="21:00", callback_data=f"analytics_report_hour:{gid}:21"),
        ],
        [InlineKeyboardButton(text="◀️ К аналитике", callback_data=f"group_section:{gid}:analytics")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def protection_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{_status(s.antiflood_enabled)} Антифлуд", callback_data=f"toggle:{gid}:antiflood"),
            InlineKeyboardButton(text=f"{_status(s.repeats_enabled)} Повторы", callback_data=f"toggle:{gid}:repeats"),
        ],
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
        [InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data=f"words:{gid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{gid}")],
    ])


def moderation_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚩 Жалобы", callback_data=f"complaints:{group_id}")],
        [InlineKeyboardButton(text="📌 Причины наказаний", callback_data=f"reasons:{group_id}")],
        [
            InlineKeyboardButton(text="👮 Модераторы", callback_data=f"roles:{group_id}"),
            InlineKeyboardButton(text="📋 Журнал", callback_data=f"logs:{group_id}"),
        ],
        [InlineKeyboardButton(text="ℹ️ Как модерировать", callback_data=f"moderation_help:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{group_id}")],
    ])


def reasons_menu(group_id: int, reasons) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{'✅' if r.active else '❌'} {r.name[:44]}", callback_data=f"reason_edit:{group_id}:{r.id}")] for r in reasons]
    rows += [
        [InlineKeyboardButton(text="➕ Добавить причину", callback_data=f"reason_add:{group_id}")],
        [InlineKeyboardButton(text="◀️ К модерации", callback_data=f"group_section:{group_id}:moderation")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reason_edit_menu(group_id: int, reason) -> InlineKeyboardMarkup:
    def a(code: str, label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=f"{'✅' if code in (reason.actions or []) else '❌'} {label}", callback_data=f"reason_action:{group_id}:{reason.id}:{code}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [a("warn", "Пред"), a("mute", "Мут")],
        [a("kick", "Кик"), a("ban", "Бан")],
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"reason_rename:{group_id}:{reason.id}")],
        [InlineKeyboardButton(text="⏯ Вкл / выкл", callback_data=f"reason_toggle:{group_id}:{reason.id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"reason_delete_confirm:{group_id}:{reason.id}")],
        [InlineKeyboardButton(text="◀️ К причинам", callback_data=f"reasons:{group_id}")],
    ])


def reason_delete_confirm(group_id: int, reason_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"reason_delete:{group_id}:{reason_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"reason_edit:{group_id}:{reason_id}")],
    ])


def moderation_duration_picker(token: str) -> InlineKeyboardMarkup:
    variants = [
        (300, "5 мин"), (900, "15 мин"), (1800, "30 мин"),
        (3600, "1 час"), (21600, "6 часов"), (86400, "1 день"), (604800, "7 дней"),
    ]
    rows = []
    for i in range(0, len(variants), 2):
        row = [InlineKeyboardButton(text=label, callback_data=f"modduration:{token}:{seconds}") for seconds, label in variants[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"modcancel:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def moderation_reason_picker(token: str, reasons) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📌 {r.name[:48]}", callback_data=f"modreason:{token}:{r.id}")] for r in reasons]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"modcancel:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def members_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти участника", callback_data=f"member_find:{group_id}")],
        [InlineKeyboardButton(text="🏆 Активные", callback_data=f"people_active:{group_id}"), InlineKeyboardButton(text="😴 Неактивные", callback_data=f"people_inactive:{group_id}")],
        [InlineKeyboardButton(text="🆕 Новички", callback_data=f"people_new:{group_id}"), InlineKeyboardButton(text="🤖 Подозрительные", callback_data=f"people_suspicious:{group_id}")],
        [
            InlineKeyboardButton(text="⚠️ Предупреждения", callback_data=f"active_punishments:{group_id}:warn"),
            InlineKeyboardButton(text="🔇 Муты", callback_data=f"active_punishments:{group_id}:mute"),
        ],
        [InlineKeyboardButton(text="⛔ Блокировки", callback_data=f"active_punishments:{group_id}:ban")],
        [InlineKeyboardButton(text="🪦 Удалённые аккаунты", callback_data=f"deleted_accounts:{group_id}")],
        [InlineKeyboardButton(text="🏆 Активность участников", callback_data=f"members_stats:{group_id}")],
        [InlineKeyboardButton(text="👮 Роли модераторов", callback_data=f"roles:{group_id}")],
        [InlineKeyboardButton(text="📋 Журнал действий", callback_data=f"logs:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{group_id}")],
    ])


def content_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data=f"words:{group_id}")],
        [InlineKeyboardButton(text="📢 Обязательные каналы", callback_data=f"channels:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{group_id}")],
    ])


def settings_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{_status(s.welcome_enabled)} Приветствие", callback_data=f"toggle:{gid}:welcome"),
            InlineKeyboardButton(text=f"{_status(s.reports_enabled)} Отчёты", callback_data=f"toggle:{gid}:reports"),
        ],
        [
            InlineKeyboardButton(text=f"{_status(s.night_mode_enabled)} Ночной режим", callback_data=f"toggle:{gid}:night"),
            InlineKeyboardButton(text=f"{_status(s.join_requests_enabled)} Заявки", callback_data=f"toggle:{gid}:joinreq"),
        ],
        [
            InlineKeyboardButton(text="⭐ Состояние группы", callback_data=f"health:{gid}"),
            InlineKeyboardButton(text="🚀 Мастер настройки", callback_data=f"setup:{gid}:start"),
        ],
        [InlineKeyboardButton(text="🧰 Параметры группы", callback_data=f"settings_detail:{gid}")],
        [InlineKeyboardButton(text="📢 Обязательные каналы", callback_data=f"channels:{gid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{gid}")],
    ])


def back_to_group(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{group_id}")]])



def setup_start_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Настроить Mimoru", callback_data=f"setup:{group_id}:start")],
        [InlineKeyboardButton(text="⭐ Проверить состояние", callback_data=f"health:{group_id}")],
    ])


def setup_profile_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Сообщество", callback_data=f"setup:{group_id}:type:community"),
            InlineKeyboardButton(text="🎮 Игры", callback_data=f"setup:{group_id}:type:gaming"),
        ],
        [
            InlineKeyboardButton(text="🪙 Крипта", callback_data=f"setup:{group_id}:type:crypto"),
            InlineKeyboardButton(text="🛍 Продажи", callback_data=f"setup:{group_id}:type:sales"),
        ],
        [
            InlineKeyboardButton(text="📰 Новости", callback_data=f"setup:{group_id}:type:news"),
            InlineKeyboardButton(text="🎓 Обучение", callback_data=f"setup:{group_id}:type:education"),
        ],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"group:{group_id}")],
    ])


def setup_level_menu(group_id: int, profile: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Минимальный", callback_data=f"setup:{group_id}:level:{profile}:minimal")],
        [InlineKeyboardButton(text="🟡 Стандартный", callback_data=f"setup:{group_id}:level:{profile}:standard")],
        [InlineKeyboardButton(text="🔴 Максимальный", callback_data=f"setup:{group_id}:level:{profile}:maximum")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"setup:{group_id}:start")],
    ])


def _setup_yes_no(group_id: int, step: str, yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=yes, callback_data=f"setup:{group_id}:{step}:on"),
            InlineKeyboardButton(text=no, callback_data=f"setup:{group_id}:{step}:off"),
        ],
        [InlineKeyboardButton(text="✖️ Завершить позже", callback_data=f"group:{group_id}")],
    ])


def setup_captcha_menu(group_id: int) -> InlineKeyboardMarkup:
    return _setup_yes_no(group_id, "captcha", "✅ Да", "❌ Нет")


def setup_welcome_menu(group_id: int) -> InlineKeyboardMarkup:
    return _setup_yes_no(group_id, "welcome", "✅ Да", "❌ Нет")


def setup_quarantine_menu(group_id: int) -> InlineKeyboardMarkup:
    return _setup_yes_no(group_id, "quarantine", "✅ Да", "❌ Нет")


def setup_reports_menu(group_id: int) -> InlineKeyboardMarkup:
    return _setup_yes_no(group_id, "reports", "✅ Да", "❌ Нет")


def setup_finish_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Проверить состояние", callback_data=f"health:{group_id}")],
        [InlineKeyboardButton(text="🛡 Открыть защиту", callback_data=f"group_section:{group_id}:protection")],
        [InlineKeyboardButton(text="🏠 К группе", callback_data=f"group:{group_id}")],
    ])


def group_health_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"health:{group_id}")],
        [
            InlineKeyboardButton(text="🛡 Защита", callback_data=f"group_section:{group_id}:protection"),
            InlineKeyboardButton(text="🪦 Удалённые аккаунты", callback_data=f"deleted_accounts:{group_id}"),
        ],
        [InlineKeyboardButton(text="🚀 Мастер настройки", callback_data=f"setup:{group_id}:start")],
        [InlineKeyboardButton(text="◀️ К настройкам", callback_data=f"group_section:{group_id}:settings")],
    ])


def subscription_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ STANDARD · 250", callback_data=f"plan_buy:{group_id}:standard"),
            InlineKeyboardButton(text="💎 PRO · 500", callback_data=f"plan_buy:{group_id}:pro"),
        ],
        [InlineKeyboardButton(text="📜 История платежей", callback_data=f"plan_history:{group_id}")],
        [InlineKeyboardButton(text="📋 Сравнить тарифы", callback_data=f"plan_compare:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{group_id}")],
    ])


def subscription_back(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К подписке", callback_data=f"plan:{group_id}")],
    ])


def stats_periods(group_id: int) -> InlineKeyboardMarkup:
    return analytics_menu(group_id)


def service_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Клиенты", callback_data="service:clients"),
            InlineKeyboardButton(text="🏠 Группы", callback_data="service:groups"),
        ],
        [
            InlineKeyboardButton(text="💳 Платежи", callback_data="service:billing"),
            InlineKeyboardButton(text="💎 Тарифы", callback_data="service:subscriptions"),
        ],
        [InlineKeyboardButton(text="📢 Реклама", callback_data="service:ads")],
        [
            InlineKeyboardButton(text="💬 Обращения", callback_data="service:tickets"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="service:stats"),
        ],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")],
    ])

ROLE_LABELS = {"senior": "Старший", "moderator": "Модератор", "helper": "Помощник"}
PERMISSION_LABELS = {
    "warn": "Пред",
    "unwarn": "Снять пред",
    "mute": "Мут",
    "unmute": "Размут",
    "kick": "Кик",
    "ban": "Бан",
    "unban": "Разбан",
    "delete": "Удалять",
    "info": "Инфо",
    "history": "История",
    "warnings": "Список предов",
}


async def roles_menu(group_id: int, moderators) -> InlineKeyboardMarkup:
    rows = []
    for m in moderators:
        name = await stored_visible_name(m.user_telegram_id)
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if m.active else '❌'} {ROLE_LABELS.get(m.role, m.role)} · {name}",
            callback_data=f"role_edit:{group_id}:{m.id}",
        )])
    rows += [
        [InlineKeyboardButton(text="➕ Добавить по Telegram ID", callback_data=f"role_add:{group_id}")],
        [InlineKeyboardButton(text="◀️ К модерации", callback_data=f"group_section:{group_id}:moderation")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def role_edit_menu(group_id: int, moderator, effective_permissions: dict[str, bool]) -> InlineKeyboardMarkup:
    def p(code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=f"{'✅' if effective_permissions.get(code, False) else '❌'} {PERMISSION_LABELS[code]}",
            callback_data=f"role_perm:{group_id}:{moderator.id}:{code}",
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👑 Старший", callback_data=f"role_set:{group_id}:{moderator.id}:senior"),
            InlineKeyboardButton(text="🛡 Модератор", callback_data=f"role_set:{group_id}:{moderator.id}:moderator"),
        ],
        [InlineKeyboardButton(text="🤝 Помощник", callback_data=f"role_set:{group_id}:{moderator.id}:helper")],
        [p("warn"), p("unwarn")],
        [p("mute"), p("unmute")],
        [p("kick"), p("delete")],
        [p("ban"), p("unban")],
        [p("info"), p("history")],
        [p("warnings")],
        [InlineKeyboardButton(text="♻️ Сбросить права роли", callback_data=f"role_reset:{group_id}:{moderator.id}")],
        [InlineKeyboardButton(text="⏯ Вкл / выкл", callback_data=f"role_toggle:{group_id}:{moderator.id}")],
        [InlineKeyboardButton(text="🗑 Удалить роль", callback_data=f"role_remove_confirm:{group_id}:{moderator.id}")],
        [InlineKeyboardButton(text="◀️ К ролям", callback_data=f"roles:{group_id}")],
    ])


def role_remove_confirm(group_id: int, moderator_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить роль", callback_data=f"role_remove:{group_id}:{moderator_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"role_edit:{group_id}:{moderator_id}")],
    ])


def words_admin_menu(group_id: int, words) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 {word[:45]}", callback_data=f"word_remove:{group_id}:{idx}")]
        for idx, word in enumerate(words[:50])
    ]
    rows += [
        [InlineKeyboardButton(text="➕ Добавить слово / фразу", callback_data=f"word_add:{group_id}")],
        [InlineKeyboardButton(text="◀️ К контенту", callback_data=f"group_section:{group_id}:content")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channels_admin_menu(group_id: int, channels) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 {channel}", callback_data=f"channel_remove:{group_id}:{idx}")]
        for idx, channel in enumerate(channels[:50])
    ]
    rows += [
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data=f"channel_add:{group_id}")],
        [InlineKeyboardButton(text="◀️ К контенту", callback_data=f"group_section:{group_id}:content")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_detail_menu(group) -> InlineKeyboardMarkup:
    gid, s = group.id, group.settings
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Текст приветствия", callback_data=f"setting_text:{gid}:welcome")],
        [InlineKeyboardButton(text="📜 Правила группы", callback_data=f"setting_text:{gid}:rules")],
        [InlineKeyboardButton(text=f"⚠️ Лимит предупреждений: {s.warnings_limit}", callback_data=f"setting_num:{gid}:warnings")],
        [InlineKeyboardButton(text=f"🔇 Мут по умолчанию: {s.default_mute_seconds // 60} мин", callback_data=f"setting_num:{gid}:defaultmute")],
        [InlineKeyboardButton(text=f"🌊 Антифлуд: {s.antiflood_limit}/{s.antiflood_window_seconds}с", callback_data=f"setting_num:{gid}:antiflood")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group_section:{gid}:settings")],
    ])


def support_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Новое обращение", callback_data="support:new")],
        [InlineKeyboardButton(text="📂 Мои обращения", callback_data="support:mine")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")],
    ])


def service_ticket_menu(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"ticket_reply:{ticket_id}")],
        [InlineKeyboardButton(text="✅ Закрыть", callback_data=f"ticket_close:{ticket_id}")],
        [InlineKeyboardButton(text="◀️ К обращениям", callback_data="service:tickets")],
    ])


def warnings_limit_menu(group_id: int, current: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if current == n else ''}{n}", callback_data=f"setting_set:{group_id}:warnings:{n}") for n in (1, 2, 3, 4, 5)],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"settings_detail:{group_id}")],
    ])


def default_mute_menu(group_id: int, current: int) -> InlineKeyboardMarkup:
    variants = [(300, "5 мин"), (900, "15 мин"), (3600, "1 час"), (21600, "6 часов"), (86400, "24 часа")]
    rows = [[InlineKeyboardButton(text=f"{'✅ ' if current == seconds else ''}{label}", callback_data=f"setting_set:{group_id}:defaultmute:{seconds}")] for seconds, label in variants]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"settings_detail:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def antiflood_preset_menu(group_id: int, current_limit: int, current_window: int) -> InlineKeyboardMarkup:
    variants = [(4, 5, "Строго · 4 за 5с"), (6, 10, "Обычно · 6 за 10с"), (8, 15, "Мягко · 8 за 15с")]
    rows = [[InlineKeyboardButton(text=f"{'✅ ' if (current_limit, current_window) == (limit, window) else ''}{label}", callback_data=f"setting_flood:{group_id}:{limit}:{window}")] for limit, window, label in variants]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"settings_detail:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def service_tickets_menu(tickets) -> InlineKeyboardMarkup:
    rows = []
    for t in tickets[:30]:
        name = await stored_visible_name(t.user_telegram_id)
        rows.append([InlineKeyboardButton(
            text=f"#{t.id} · {t.status} · {name}",
            callback_data=f"ticket:{t.id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Панель Mimoru", callback_data="service:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_card_menu(group_id: int, user_id: int, has_mute: bool, has_ban: bool, warnings: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="⚠️ Предупредить", callback_data=f"member_punish:{group_id}:{user_id}:warn"),
            InlineKeyboardButton(text="🔇 Мут", callback_data=f"member_punish:{group_id}:{user_id}:mute"),
        ],
        [
            InlineKeyboardButton(text="🚪 Кик", callback_data=f"member_punish:{group_id}:{user_id}:kick"),
            InlineKeyboardButton(text="⛔ Бан", callback_data=f"member_punish:{group_id}:{user_id}:ban"),
        ],
    ]
    if warnings:
        rows.append([InlineKeyboardButton(text=f"⚠️ Снять предупреждение ({warnings})", callback_data=f"member_action:{group_id}:{user_id}:unwarn")])
    if has_mute:
        rows.append([InlineKeyboardButton(text="🔊 Снять мут", callback_data=f"member_action:{group_id}:{user_id}:unmute")])
    if has_ban:
        rows.append([InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"member_action:{group_id}:{user_id}:unban")])
    rows += [
        [InlineKeyboardButton(text="📜 История действий", callback_data=f"member_history:{group_id}:{user_id}")],
        [InlineKeyboardButton(text="🏷 Теги", callback_data=f"member_tags:{group_id}:{user_id}"), InlineKeyboardButton(text="📝 Заметки", callback_data=f"member_notes:{group_id}:{user_id}")],
        [InlineKeyboardButton(text="◀️ К участникам", callback_data=f"group_section:{group_id}:members")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def active_punishments_menu(group_id: int, kind: str, rows) -> InlineKeyboardMarkup:
    buttons = []
    icon = {"warn": "⚠️", "mute": "🔇", "ban": "⛔"}.get(kind, "•")
    for row in rows[:30]:
        name = await stored_visible_name(row.user_telegram_id)
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {name}",
            callback_data=f"member_card:{group_id}:{row.user_telegram_id}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ К участникам", callback_data=f"group_section:{group_id}:members")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def complaints_menu(group_id: int, complaints) -> InlineKeyboardMarkup:
    rows = []
    for c in complaints[:30]:
        name = await stored_visible_name(c.target_telegram_id)
        rows.append([InlineKeyboardButton(
            text=f"🚩 #{c.id} · {name}",
            callback_data=f"complaint:{group_id}:{c.id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ К модерации", callback_data=f"group_section:{group_id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_review_menu(group_id: int, complaint_id: int, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Карточка участника", callback_data=f"member_card:{group_id}:{target_id}")],
        [InlineKeyboardButton(text="✅ Закрыть как рассмотренную", callback_data=f"complaint_close:{group_id}:{complaint_id}")],
        [InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"complaint_reject:{group_id}:{complaint_id}")],
        [InlineKeyboardButton(text="◀️ К жалобам", callback_data=f"complaints:{group_id}")],
    ])


def deleted_accounts_menu(group_id: int, has_deleted: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data=f"deleted_accounts_scan:{group_id}")],
    ]
    if has_deleted:
        rows.append([InlineKeyboardButton(text="🧹 Удалить все", callback_data=f"deleted_accounts_remove_confirm:{group_id}")])
    rows.append([InlineKeyboardButton(text="◀️ К участникам", callback_data=f"group_section:{group_id}:members")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deleted_accounts_confirm_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Да, удалить найденные", callback_data=f"deleted_accounts_remove:{group_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"deleted_accounts:{group_id}")],
    ])


def service_plan_group_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="STANDARD +30д", callback_data=f"service_plan_grant:{group_id}:standard:30"),
            InlineKeyboardButton(text="PRO +30д", callback_data=f"service_plan_grant:{group_id}:pro:30"),
        ],
        [InlineKeyboardButton(text="TRIAL +7д", callback_data=f"service_plan_grant:{group_id}:trial:7")],
        [InlineKeyboardButton(text="🆓 Перевести на FREE", callback_data=f"service_plan_grant:{group_id}:free:0")],
        [InlineKeyboardButton(text="◀️ К тарифам", callback_data="service:subscriptions")],
    ])


def people_list_menu(group_id: int, rows) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=str(label)[:52], callback_data=f"member_card:{group_id}:{uid}")] for uid, label in rows[:30]]
    buttons.append([InlineKeyboardButton(text="◀️ К участникам", callback_data=f"group_section:{group_id}:members")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def member_tags_menu(group_id: int, user_id: int, tags, assigned_ids) -> InlineKeyboardMarkup:
    rows=[]
    for tag in tags[:20]:
        mark="✅" if tag.id in assigned_ids else "▫️"
        rows.append([InlineKeyboardButton(text=f"{mark} {tag.name}", callback_data=f"member_tag_toggle:{group_id}:{user_id}:{tag.id}")])
    rows.append([InlineKeyboardButton(text="➕ Новый тег", callback_data=f"member_tag_new:{group_id}:{user_id}")])
    rows.append([InlineKeyboardButton(text="◀️ К карточке", callback_data=f"member_card:{group_id}:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def automation_menu(group) -> InlineKeyboardMarkup:
    s, gid = group.settings, group.id
    schedule_names = {"off": "выкл", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{_status(s.automation_enabled)} Автоматизация", callback_data=f"automation_toggle:{gid}")],
        [InlineKeyboardButton(text=f"🪦 Очистка аккаунтов: {schedule_names.get(s.deleted_cleanup_schedule, 'выкл')}", callback_data=f"automation_cleanup:{gid}")],
        [InlineKeyboardButton(text=f"⚠️ Срок предупреждений: {s.warning_expire_days if s.warning_expire_days else 'не удалять'}", callback_data=f"automation_warnings:{gid}")],
        [InlineKeyboardButton(text=f"⚠️ {s.warnings_limit}/{s.warnings_limit} предупреждений → мут", callback_data=f"setting_num:{gid}:warnings")],
        [InlineKeyboardButton(text="👋 Сценарий новичка", callback_data=f"automation_newcomer:{gid}")],
        [InlineKeyboardButton(text="📋 Журнал автоматизации", callback_data=f"automation_logs:{gid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"group:{gid}")],
    ])

def automation_cleanup_menu(group_id: int, current: str) -> InlineKeyboardMarkup:
    def b(code, label):
        return InlineKeyboardButton(text=("✅ " if current == code else "") + label, callback_data=f"automation_cleanup_set:{group_id}:{code}")
    return InlineKeyboardMarkup(inline_keyboard=[[b("off","Выкл")],[b("weekly","Раз в неделю")],[b("monthly","Раз в месяц")],[InlineKeyboardButton(text="◀️ Автоматизация", callback_data=f"automation:{group_id}")]])

def automation_warning_menu(group_id: int, current: int) -> InlineKeyboardMarkup:
    vals=[(0,"Не удалять"),(7,"7 дней"),(30,"30 дней"),(90,"90 дней")]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=("✅ " if current==v else "")+label, callback_data=f"automation_warning_set:{group_id}:{v}")] for v,label in vals]+[[InlineKeyboardButton(text="◀️ Автоматизация", callback_data=f"automation:{group_id}")]])

def automation_newcomer_menu(group) -> InlineKeyboardMarkup:
    gid=group.id; s=group.settings
    if s.captcha_enabled and s.newcomer_quarantine_enabled and s.welcome_enabled: current="strict"
    elif s.welcome_enabled and (s.captcha_enabled or s.newcomer_quarantine_enabled): current="standard"
    else: current="basic"
    def b(code,label): return InlineKeyboardButton(text=("✅ " if current==code else "")+label, callback_data=f"automation_newcomer_set:{gid}:{code}")
    return InlineKeyboardMarkup(inline_keyboard=[[b("basic","Базовый")],[b("standard","Стандартный")],[b("strict","Строгий")],[InlineKeyboardButton(text="◀️ Автоматизация", callback_data=f"automation:{gid}")]])


def operations_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🩺 Диагностика и права", callback_data=f"ops_diag:{group_id}")],
        [InlineKeyboardButton(text="📋 Единый журнал", callback_data=f"ops_logs:{group_id}")],
        [InlineKeyboardButton(text="📦 Резервные копии", callback_data=f"ops_backup:{group_id}")],
        [InlineKeyboardButton(text="⭐ Состояние группы", callback_data=f"health:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])

def snapshot_targets_menu(snapshot_id: int, groups, source_group_id: int) -> InlineKeyboardMarkup:
    rows=[]
    for group in groups:
        suffix=" · текущая" if group.id==source_group_id else ""
        rows.append([InlineKeyboardButton(text=f"🏠 {group.title[:38]}{suffix}", callback_data=f"ops_snapshot_confirm:{snapshot_id}:{group.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"ops_snapshot:{snapshot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
