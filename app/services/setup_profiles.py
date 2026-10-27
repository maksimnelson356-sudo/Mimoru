from __future__ import annotations

PROFILE_LABELS = {
    "community": "Сообщество",
    "gaming": "Игры",
    "crypto": "Крипта",
    "sales": "Продажи",
    "news": "Новости",
    "education": "Обучение",
}

LEVEL_LABELS = {
    "minimal": "Минимальный",
    "standard": "Стандартный",
    "maximum": "Максимальный",
}


def apply_setup_profile(settings, profile: str, level: str) -> None:
    """Apply a conservative preset using only existing Mimoru settings.

    Individual settings can still be changed after the wizard. The wizard deliberately
    avoids changing texts, moderator roles, required channels, billing or advertising.
    """
    if profile not in PROFILE_LABELS:
        raise ValueError("unknown profile")
    if level not in LEVEL_LABELS:
        raise ValueError("unknown level")

    # Safe baseline shared by all profiles.
    settings.antiflood_enabled = True
    settings.repeats_enabled = True
    settings.anti_raid_enabled = True
    settings.mention_filter_enabled = True
    settings.sender_chat_filter_enabled = True
    settings.welcome_enabled = True

    # Links are intentionally profile-specific because many education/gaming groups
    # legitimately exchange links while finance/sales communities often need a tighter policy.
    settings.links_enabled = profile in {"gaming", "education"}

    if level == "minimal":
        settings.edit_protection_enabled = False
        settings.caps_enabled = False
        settings.captcha_enabled = False
        settings.newcomer_quarantine_enabled = False
        settings.antiflood_limit = 8
        settings.antiflood_window_seconds = 12
        settings.antiflood_mute_seconds = 600
    elif level == "standard":
        settings.edit_protection_enabled = True
        settings.caps_enabled = False
        settings.antiflood_limit = 6
        settings.antiflood_window_seconds = 10
        settings.antiflood_mute_seconds = 1800
        # Higher-risk public categories default to captcha in the standard preset.
        settings.captcha_enabled = profile in {"crypto", "sales"}
        settings.newcomer_quarantine_enabled = False
    else:
        settings.edit_protection_enabled = True
        settings.caps_enabled = True
        settings.captcha_enabled = True
        settings.newcomer_quarantine_enabled = True
        settings.antiflood_limit = 4
        settings.antiflood_window_seconds = 8
        settings.antiflood_mute_seconds = 3600
        settings.links_enabled = False

    # Profiles can add a small amount of context without making hidden destructive changes.
    settings.join_requests_enabled = profile in {"crypto", "sales", "news"}
    if profile == "news" and level == "maximum":
        settings.slow_mode_enabled = True
        settings.slow_mode_seconds = max(settings.slow_mode_seconds, 10)
