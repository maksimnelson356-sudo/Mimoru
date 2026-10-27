from types import SimpleNamespace

import pytest

from app.services.setup_profiles import apply_setup_profile


def settings():
    return SimpleNamespace(
        antiflood_enabled=False,
        repeats_enabled=False,
        anti_raid_enabled=False,
        mention_filter_enabled=False,
        sender_chat_filter_enabled=False,
        welcome_enabled=False,
        links_enabled=True,
        edit_protection_enabled=False,
        caps_enabled=False,
        captcha_enabled=False,
        newcomer_quarantine_enabled=False,
        antiflood_limit=99,
        antiflood_window_seconds=99,
        antiflood_mute_seconds=99,
        join_requests_enabled=False,
        slow_mode_enabled=False,
        slow_mode_seconds=1,
    )


def test_standard_crypto_profile_is_safe_for_public_finance_group():
    s = settings()
    apply_setup_profile(s, "crypto", "standard")
    assert s.antiflood_enabled is True
    assert s.anti_raid_enabled is True
    assert s.links_enabled is False
    assert s.captcha_enabled is True
    assert s.edit_protection_enabled is True
    assert s.newcomer_quarantine_enabled is False


def test_maximum_profile_enables_strong_newcomer_protection():
    s = settings()
    apply_setup_profile(s, "community", "maximum")
    assert s.captcha_enabled is True
    assert s.newcomer_quarantine_enabled is True
    assert s.links_enabled is False
    assert s.antiflood_limit == 4
    assert s.antiflood_mute_seconds == 3600


def test_gaming_minimal_keeps_links_available():
    s = settings()
    apply_setup_profile(s, "gaming", "minimal")
    assert s.links_enabled is True
    assert s.captcha_enabled is False
    assert s.newcomer_quarantine_enabled is False


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError):
        apply_setup_profile(settings(), "unknown", "standard")
