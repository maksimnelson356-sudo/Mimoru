from __future__ import annotations

from typing import Literal

ActionType = Literal[
    "complaint",
    "warn",
    "mute",
    "filter",
    "ban",
    "kick",
    "unwarn",
    "unmute",
    "unban",
]

_ICONS: dict[str, str] = {
    "complaint": "🟣",
    "warn": "🟡",
    "mute": "🟠",
    "filter": "🔴",
    "ban": "🚫",
    "kick": "🚫",
    "unwarn": "🟢",
    "unmute": "🟢",
    "unban": "🟢",
}

_TITLES: dict[str, str] = {
    "complaint": "Жалоба от пользователя",
    "warn": "Предупреждение",
    "mute": "Ограничение чата (Мут)",
    "filter": "Срабатывание фильтра (ЧС)",
    "ban": "Исключение из чата (Бан)",
    "kick": "Исключение из чата (Кик)",
    "unwarn": "Снятие предупреждения",
    "unmute": "Снятие ограничения",
    "unban": "Разбан",
}


def format_action_panel(
    action: ActionType,
    *,
    fields: list[tuple[str, str]],
    footer: str | None = None,
    title_suffix: str = "",
) -> str:
    """Единый шаблон уведомления для всех действий бота.

    action — тип события (жалоба, варн, мут, бан...).
    fields — список (иконка, текст), выводится построчно.
    footer — нижний блок (причина, цитата), выводится через ">".
    title_suffix — дополнительный текст к заголовку (например, "[1/3]").
    """
    icon = _ICONS.get(action, "🟣")
    title = _TITLES.get(action, action)
    if title_suffix:
        title = f"{title} {title_suffix}"

    lines = [f"{icon} Mimoru | {title}", "—", ""]
    for field_icon, field_text in fields:
        lines.append(f"{field_icon} {field_text}")

    if footer:
        lines.append("")
        lines.append(f"> {footer}")

    return "\n".join(lines)


__all__ = ["ActionType", "format_action_panel"]
