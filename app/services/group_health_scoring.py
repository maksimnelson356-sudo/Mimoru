from __future__ import annotations


def health_level(score: int) -> str:
    if score >= 90:
        return "🟢 Отлично"
    if score >= 75:
        return "🟢 Хорошо"
    if score >= 55:
        return "🟡 Требует внимания"
    return "🔴 Нужна настройка"


def hygiene_points(known_members: int, deleted_accounts: int) -> int:
    if known_members <= 0:
        return 10
    ratio = deleted_accounts / known_members
    if ratio <= 0.01:
        return 20
    if ratio <= 0.03:
        return 16
    if ratio <= 0.05:
        return 12
    if ratio <= 0.10:
        return 7
    return 3


def protection_points(settings) -> int:
    flags = (
        settings.antiflood_enabled,
        settings.repeats_enabled,
        settings.anti_raid_enabled,
        settings.edit_protection_enabled,
        settings.mention_filter_enabled,
        settings.sender_chat_filter_enabled,
    )
    return sum(4 for enabled in flags if enabled)


def newcomer_points(settings) -> int:
    return (
        (4 if settings.captcha_enabled else 0)
        + (4 if settings.newcomer_quarantine_enabled else 0)
        + (2 if settings.welcome_enabled else 0)
        + (2 if settings.join_requests_enabled else 0)
    )
