from __future__ import annotations

from typing import Any

from app.db.models import Group

EXPORTABLE_FIELDS = (
    "antiflood_enabled",
    "links_enabled",
    "captcha_enabled",
    "welcome_enabled",
    "welcome_text",
    "warnings_limit",
    "warning_expire_hours",
    "automation_enabled",
    "deleted_cleanup_schedule",
    "default_mute_seconds",
    "antiflood_limit",
    "antiflood_window_seconds",
    "antiflood_mute_seconds",
    "repeats_enabled",
    "repeats_limit",
    "caps_enabled",
    "caps_percent",
    "caps_min_length",
    "voices_allowed",
    "stickers_allowed",
    "forwards_allowed",
    "rules_text",
    "reports_enabled",
    "report_hour_utc",
    "timezone_name",
    "night_mode_enabled",
    "night_mode_start",
    "night_mode_end",
    "join_requests_enabled",
    "join_requests_auto_approve",
    "newcomer_quarantine_enabled",
    "newcomer_quarantine_seconds",
    "newcomer_quarantine_block_links",
    "newcomer_quarantine_block_media",
    "newcomer_quarantine_block_forwards",
    "slow_mode_enabled",
    "slow_mode_seconds",
    "edit_protection_enabled",
    "edit_protection_window_seconds",
    "mention_filter_enabled",
    "mention_limit",
    "hashtag_limit",
    "mention_mute_seconds",
    "sender_chat_filter_enabled",
    "allow_group_sender_identity",
)

INT_RANGES: dict[str, tuple[int, int]] = {
    "warnings_limit": (1, 20),
    "warning_expire_hours": (0, 87_600),
    "default_mute_seconds": (30, 31_536_000),
    "antiflood_limit": (2, 100),
    "antiflood_window_seconds": (1, 3600),
    "antiflood_mute_seconds": (30, 31_536_000),
    "repeats_limit": (2, 100),
    "caps_percent": (10, 100),
    "caps_min_length": (1, 1000),
    "report_hour_utc": (0, 23),
    "newcomer_quarantine_seconds": (300, 2_592_000),
    "slow_mode_seconds": (1, 3600),
    "edit_protection_window_seconds": (300, 2_592_000),
    "mention_limit": (1, 100),
    "hashtag_limit": (1, 100),
    "mention_mute_seconds": (30, 31_536_000),
}

TEXT_LIMITS = {
    "welcome_text": 2000,
    "rules_text": 4000,
    "timezone_name": 64,
    "night_mode_start": 5,
    "night_mode_end": 5,
    "deleted_cleanup_schedule": 16,
}


def export_group_settings(group: Group) -> dict[str, Any]:
    return {
        "format": "mimoru-settings-v1",
        "group": {"id": group.id, "title": group.title},
        "settings": {field: getattr(group.settings, field) for field in EXPORTABLE_FIELDS if hasattr(group.settings, field)},
    }


def _validated_value(field: str, value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ValueError(f"Поле {field} должно быть true или false")
        return value
    if isinstance(current, int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Поле {field} должно быть целым числом")
        minimum, maximum = INT_RANGES.get(field, (0, 31_536_000))
        if not minimum <= value <= maximum:
            raise ValueError(f"Поле {field} должно быть от {minimum} до {maximum}")
        return value
    if isinstance(current, str):
        if not isinstance(value, str):
            raise ValueError(f"Поле {field} должно быть строкой")
        value = value.strip()
        if field == "deleted_cleanup_schedule" and value not in {"off", "weekly", "monthly"}:
            raise ValueError("Расписание очистки должно быть off, weekly или monthly")
        if not value:
            raise ValueError(f"Поле {field} не может быть пустым")
        limit = TEXT_LIMITS.get(field, 4000)
        if len(value) > limit:
            raise ValueError(f"Поле {field} длиннее {limit} символов")
        return value
    raise ValueError(f"Поле {field} имеет неподдерживаемый тип")


def import_group_settings(group: Group, payload: dict[str, Any]) -> list[str]:
    if payload.get("format") not in {"mimoru-settings-v1", "ru-moderator-settings-v1"}:
        raise ValueError("Неподдерживаемый формат файла настроек")
    raw = payload.get("settings")
    if not isinstance(raw, dict):
        raise ValueError("В файле отсутствует раздел settings")
    changed: list[str] = []
    for field in EXPORTABLE_FIELDS:
        if field not in raw or not hasattr(group.settings, field):
            continue
        current = getattr(group.settings, field)
        value = _validated_value(field, raw[field], current)
        if value != current:
            setattr(group.settings, field, value)
            changed.append(field)
    return changed
