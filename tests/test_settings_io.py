from types import SimpleNamespace

from app.services.settings_io import export_group_settings, import_group_settings


def make_group():
    settings = SimpleNamespace(
        antiflood_enabled=True, links_enabled=False, captcha_enabled=False,
        welcome_enabled=True, welcome_text="Привет", warnings_limit=3,
        default_mute_seconds=3600, antiflood_limit=6,
        antiflood_window_seconds=10, antiflood_mute_seconds=1800,
        repeats_enabled=True, repeats_limit=3, caps_enabled=False,
        caps_percent=70, caps_min_length=15, voices_allowed=True,
        stickers_allowed=True, forwards_allowed=True, rules_text="Правила",
        reports_enabled=False, report_hour_utc=8, timezone_name="Europe/Warsaw",
        night_mode_enabled=False, night_mode_start="23:00", night_mode_end="07:00",
        join_requests_enabled=True, join_requests_auto_approve=False,
        newcomer_quarantine_enabled=False, newcomer_quarantine_seconds=86400,
        newcomer_quarantine_block_links=True, newcomer_quarantine_block_media=True,
        newcomer_quarantine_block_forwards=True,
        slow_mode_enabled=False,
        slow_mode_seconds=10,
        edit_protection_enabled=True, edit_protection_window_seconds=172800,
        mention_filter_enabled=True, mention_limit=5, hashtag_limit=10,
        mention_mute_seconds=1800,
        sender_chat_filter_enabled=True,
        allow_group_sender_identity=True,
    )
    return SimpleNamespace(id=1, title="Test", settings=settings)


def test_export_and_import_roundtrip():
    group = make_group()
    payload = export_group_settings(group)
    payload["settings"]["links_enabled"] = True
    changed = import_group_settings(group, payload)
    assert "links_enabled" in changed
    assert group.settings.links_enabled is True


def test_import_rejects_unknown_format():
    group = make_group()
    try:
        import_group_settings(group, {"format": "bad", "settings": {}})
    except ValueError:
        return
    raise AssertionError("ValueError expected")


def test_import_rejects_wrong_boolean_type():
    group = make_group()
    payload = export_group_settings(group)
    payload["settings"]["antiflood_enabled"] = "yes"
    try:
        import_group_settings(group, payload)
    except ValueError as error:
        assert "true или false" in str(error)
        return
    raise AssertionError("ValueError expected")


def test_import_rejects_out_of_range_integer():
    group = make_group()
    payload = export_group_settings(group)
    payload["settings"]["warnings_limit"] = 999
    try:
        import_group_settings(group, payload)
    except ValueError as error:
        assert "от 1 до 20" in str(error)
        return
    raise AssertionError("ValueError expected")


def test_import_ignores_unknown_fields():
    group = make_group()
    payload = export_group_settings(group)
    payload["settings"]["unexpected_admin_override"] = True
    changed = import_group_settings(group, payload)
    assert "unexpected_admin_override" not in changed
    assert not hasattr(group.settings, "unexpected_admin_override")
