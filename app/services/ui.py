from __future__ import annotations

import re
from html import unescape

from app.utils.duration import human_duration

_TAG_RE = re.compile(r"</?[A-Za-z][^>\n]*>")
_ID_LINE_RE = re.compile(
    r"(?im)^\s*(?:🆔\s*)?(?:telegram\s+id|id владельца|id группы|id)\s*:\s*-?\d+\s*$\n?"
)
_INLINE_ID_RE = re.compile(r"\s*·\s*ID\s+-?\d+", re.IGNORECASE)
_ASSIGNED_BY_ID_RE = re.compile(r"\s*·\s*назначил\s+-?\d+", re.IGNORECASE)
_RETIRED_KICK_LINE_RE = re.compile(r"(?im)^\s*кик\s*$\n?")


def clean_ui_text(text: str) -> str:
    """Return plain Telegram text and hide explicit internal Telegram IDs from UI."""
    value = str(text)
    for _ in range(3):
        decoded = unescape(value)
        if decoded == value:
            break
        value = decoded
    value = _TAG_RE.sub("", value)
    value = _RETIRED_KICK_LINE_RE.sub("", value)
    value = _ID_LINE_RE.sub("", value)
    value = _INLINE_ID_RE.sub("", value)
    value = _ASSIGNED_BY_ID_RE.sub("", value)
    return value


def display_name(
    *,
    full_name: str | None = None,
    username: str | None = None,
    user_id: int | None = None,
) -> str:
    """Return a human-facing label without exposing Telegram IDs."""
    full = clean_ui_text(full_name.strip()) if full_name and full_name.strip() else ""
    handle = (
        "@" + clean_ui_text(username.strip().lstrip("@"))
        if username and username.strip()
        else ""
    )
    if full and handle:
        return f"{full} · {handle}"
    if full:
        return full
    if handle:
        return handle
    return "пользователь"


def manual_action_notice(
    *,
    action: str,
    target: str,
    moderator: str,
    reason: str | None,
    duration_seconds: int | None = None,
    actor_role: str = "admin",
    warning_count: int | None = None,
    warning_limit: int | None = None,
) -> str:
    target = clean_ui_text(target)
    moderator = clean_ui_text(moderator)
    reason = clean_ui_text(reason or "").strip()
    if reason.casefold() == "не указана":
        reason = ""
    actor_nom = "Владелец группы" if actor_role == "owner" else "Администратор"
    actor_gen = "владельца группы" if actor_role == "owner" else "администратора"
    reason_block = f"\n\nПричина: {reason}." if reason else ""

    if action == "warn":
        count = warning_count or 1
        limit = warning_limit or 3
        reason_text = f" за {reason}" if reason else ""
        return (
            f"⚠️ {target}, Вам выдано предупреждение {count}/{limit} "
            f"от {actor_gen} {moderator}{reason_text}.\n\n"
            "Будьте аккуратнее!"
        )
    if action == "mute":
        duration = human_duration(duration_seconds or 0)
        return f"🔇 {actor_nom} {moderator} запретил {target} писать {duration}.{reason_block}"
    if action == "ban":
        duration = f" на {human_duration(duration_seconds)}" if duration_seconds else ""
        return (
            f"⛔ {actor_nom} {moderator} заблокировал {target}{duration}.{reason_block}"
        )
    if action == "kick":
        return f"🚪 {actor_nom} {moderator} исключил {target} из группы.{reason_block}"
    if action == "unmute":
        return f"✅ {actor_nom} {moderator} снял ограничения с {target}."
    if action == "unban":
        return f"✅ {actor_nom} {moderator} разблокировал {target}."
    if action == "unwarn":
        return f"✅ {actor_nom} {moderator} снял последнее предупреждение с {target}."
    return f"✅ Действие выполнено для {target}."


def automatic_action_notice(
    *,
    action: str,
    target: str,
    reason: str,
    duration_seconds: int | None = None,
    warning_count: int | None = None,
    warning_limit: int | None = None,
) -> str:
    target = clean_ui_text(target)
    reason = clean_ui_text(reason or "Нарушение правил")
    if action == "warn":
        count = warning_count or 1
        limit = warning_limit or 3
        return (
            f"⚠️ Mimoru выдала {target} предупреждение {count}/{limit} за {reason}.\n\n"
            f"{target}, будьте аккуратнее!"
        )
    if action == "mute":
        return (
            f"🔇 Mimoru запретила {target} писать {human_duration(duration_seconds or 0)} "
            f"за {reason}."
        )
    if action == "ban":
        return f"⛔ Mimoru заблокировала {target} за {reason}."
    if action == "delete":
        return f"🗑 Сообщение {target} удалено. Причина: {reason}."
    return f"⚠️ Mimoru применила ограничение к {target}. Причина: {reason}."


def panel_header(title: str, subtitle: str | None = None) -> str:
    text = f"🟣 Mimoru · {title}"
    if subtitle:
        text += f"\n\n{subtitle}"
    return clean_ui_text(text).rstrip()


_CHAT_UNAVAILABLE_MARKERS = (
    "chat not found",
    "bot was kicked",
    "bot was blocked",
    "not enough rights",
    "member not found",
    "user not found",
    "chat_write_forbidden",
    "have no rights to send a message",
)


def is_chat_unavailable(error: BaseException) -> bool:
    """True, если ошибка означает, что бот не может работать с чатом
    (удалён, кикнут, нет прав). Это НЕ ошибка бота — это состояние группы."""
    message = str(error).casefold()
    return any(marker in message for marker in _CHAT_UNAVAILABLE_MARKERS)
