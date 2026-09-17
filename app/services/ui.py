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
    text = format_action_notice(
        target_name=target,
        action=action,
        moderator_name=moderator,
        reason=reason if reason else None,
        duration_seconds=duration_seconds,
    )
    if action == "warn" and warning_count is not None and warning_limit is not None:
        text += f"\n\n⚠️ Активных предупреждений: {warning_count}/{warning_limit}."
        if warning_count >= warning_limit:
            text += "\n🚨 Достигнут лимит — применён автоматический мут."
    return text


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


_ACTION_LABELS = {
    "ban": "заблокирован",
    "mute": "замучен",
    "warn": "получил предупреждение",
    "kick": "исключён",
    "unban": "разблокирован",
    "unmute": "размучен",
    "unwarn": "предупреждение снято",
}

_ACTIONS_WITH_DURATION = {"ban", "mute"}


def format_action_notice(
    *,
    target_name: str,
    action: str,
    moderator_name: str,
    reason: str | None = None,
    duration_seconds: int | None = None,
) -> str:
    """Единый формат уведомления о модерации в группе.

    Пример:
        @target
        заблокирован
        администратором @moderator ✨
        за Спам
    """
    verb = _ACTION_LABELS.get(action, action)
    suffix = ""
    if action in _ACTIONS_WITH_DURATION and duration_seconds:
        suffix = f" на {human_duration(duration_seconds)}"
    lines = [
        target_name,
        f"{verb}{suffix}",
        f"администратором {moderator_name} ✨",
    ]
    if reason and reason.strip():
        lines.append(f"за {reason.strip()}")
    return "\n".join(lines)
