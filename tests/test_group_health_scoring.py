from types import SimpleNamespace

from app.services.group_health_scoring import health_level, hygiene_points, newcomer_points, protection_points


def test_health_levels_are_stable():
    assert health_level(95).startswith("🟢")
    assert health_level(75) == "🟢 Хорошо"
    assert health_level(60).startswith("🟡")
    assert health_level(20).startswith("🔴")


def test_deleted_account_ratio_reduces_hygiene_score():
    assert hygiene_points(1000, 5) == 20
    assert hygiene_points(1000, 40) == 12
    assert hygiene_points(1000, 120) == 3


def test_protection_and_newcomer_scores_use_enabled_features():
    s = SimpleNamespace(
        antiflood_enabled=True,
        repeats_enabled=True,
        anti_raid_enabled=True,
        edit_protection_enabled=True,
        mention_filter_enabled=True,
        sender_chat_filter_enabled=True,
        captcha_enabled=True,
        newcomer_quarantine_enabled=True,
        welcome_enabled=True,
        join_requests_enabled=True,
    )
    assert protection_points(s) == 24
    assert newcomer_points(s) == 12
