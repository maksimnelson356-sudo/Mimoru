from app.services.public_identity import public_user_token
from app.services.ui import clean_ui_text


# Telegram-observed and automatic actions are stored with actor 0
# (SYSTEM_DISCONNECT_ACTOR_ID in app.services.group_disconnects).
SYSTEM_ACTOR_ID = 0


ACTION_LABELS = {
    "ban": "🚫 Бан", "unban": "✅ Разбан", "mute": "🔇 Мут", "unmute": "🔊 Размут",
    "kick": "🚪 Кик", "warn": "⚠️ Предупреждение", "unwarn": "↩️ Снятие предупреждения",
    "auto_mute": "🤖 Автоматический мут", "delete_message": "🗑 Удаление сообщения",
    "lockdown_on": "🔒 Локдаун включён", "lockdown_off": "🔓 Локдаун выключен",
    "note_add": "📝 Заметка добавлена", "note_delete": "🗑 Заметка удалена",
    "rules_update": "📜 Правила обновлены",
    "rank_removed_by_leave": "📉 Ранг снят (человек вышел из группы)",
}


def render_log(group, row) -> str:
    """Render one journal entry for the audit chat.

    People are rendered through public_user_token so the audit chat shows a real
    name (resolved by PlainTextBot at send time) instead of a bare numeric ID.
    """
    label = clean_ui_text(ACTION_LABELS.get(row.action, f"⚙️ {row.action}"))
    lines = [label, f"Группа: {clean_ui_text(group.title)}"]
    if row.actor_telegram_id in (None, SYSTEM_ACTOR_ID):
        lines.append("Действие: Mimoru автоматически")
    else:
        lines.append(f"Администратор: {public_user_token(row.actor_telegram_id)}")
    if row.target_telegram_id not in (None, SYSTEM_ACTOR_ID):
        lines.append(f"Участник: {public_user_token(row.target_telegram_id)}")
    if row.reason:
        lines.append(f"Причина: {clean_ui_text(row.reason)}")
    lines.append(f"Событие: LOG-{row.id}")
    return "\n".join(lines)
