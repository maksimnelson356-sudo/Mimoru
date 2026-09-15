from app.services.ui import automatic_action_notice, display_name, manual_action_notice, panel_header


def test_display_name_priority_and_plain_text():
    assert display_name(full_name="Соня <3", username="sonya", user_id=1) == "Соня <3 · @sonya"
    assert display_name(username="@sonya", user_id=1) == "@sonya"
    assert display_name(user_id=42) == "пользователь"


def test_manual_warning_notice_is_human_readable():
    text = manual_action_notice(
        action="warn",
        target="Соня",
        moderator="Олег",
        reason="оскорбление участника",
        warning_count=1,
        warning_limit=3,
    )
    assert "Соня" in text
    assert "получил предупреждение" in text
    assert "Олег" in text
    assert "оскорбление участника" in text
    assert "администратором" in text


def test_manual_mute_notice_contains_duration():
    text = manual_action_notice(
        action="mute",
        target="Соня",
        moderator="Олег",
        reason="маты",
        duration_seconds=300,
    )
    assert "5 мин." in text
    assert "маты" in text


def test_manual_notices_hide_missing_reason_sentinel():
    cases = (
        ("warn", {}),
        ("mute", {"duration_seconds": 300}),
        ("ban", {}),
    )
    for action, extra in cases:
        text = manual_action_notice(
            action=action,
            target="Соня",
            moderator="Олег",
            reason="Не указана",
            **extra,
        )
        assert "Не указана" not in text
        assert "Причина:" not in text


def test_manual_notice_accepts_none_reason():
    text = manual_action_notice(
        action="warn",
        target="Соня",
        moderator="Олег",
        reason=None,
        warning_count=1,
        warning_limit=3,
    )
    assert "Не указана" not in text
    assert " за " not in text


def test_automatic_notice_identifies_mimoru():
    text = automatic_action_notice(
        action="mute",
        target="Соня",
        reason="флуд",
        duration_seconds=60,
    )
    assert "Mimoru" in text
    assert "1 мин." in text


def test_panel_header_removes_html_like_fragments():
    assert panel_header("Группа <test>") == "🟣 Mimoru · Группа"
    assert "<" not in panel_header("<b>Тест</b>")
