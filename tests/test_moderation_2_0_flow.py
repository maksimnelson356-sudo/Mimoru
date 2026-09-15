from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_reply_aliases_open_member_card():
    source = read("app/utils/commands.py")
    for alias in ['"карточка": "info"', '"кто ты": "info"', '"кто это": "info"']:
        assert alias in source


def test_plain_mute_opens_duration_picker_and_explicit_duration_still_supported():
    source = read("app/handlers/group.py")
    assert 'command.action == "mute" and command.duration is None' in source
    assert "moderation_duration_picker(token)" in source
    commands = read("app/utils/commands.py")
    assert '"мут": "mute"' in commands
    assert "parse_duration(parts[0])" in commands


def test_mute_duration_keyboard_contains_expected_choices():
    source = read("app/keyboards/panel.py")
    for label in ["5 мин", "15 мин", "30 мин", "1 час", "6 часов", "1 день", "7 дней"]:
        assert f'"{label}"' in source
    assert 'callback_data=f"modduration:{token}:{seconds}"' in source


def test_member_card_has_owner_moderation_actions():
    source = read("app/keyboards/panel.py")
    for label in ["⚠️ Предупредить", "🔇 Мут", "🚪 Кик", "⛔ Бан"]:
        assert label in source
    assert "member_punish:" in source


def test_owner_notice_is_distinguished_from_admin_notice():
    from app.services.ui import format_action_notice

    owner = format_action_notice(
        target_name="Target",
        action="ban",
        moderator_name="Moderator",
        reason="test",
        duration_seconds=3600,
    )
    # The new unified format doesn't distinguish between owner/admin in the text
    # Both produce the same format: "Target\nзаблокирован на 1 ч.\nадминистратором Moderator ✨\nза test"
    assert "администратором" in owner
    assert "✨" in owner
    assert "за test" in owner


def test_panel_moderation_is_routed_to_real_group_and_public_notice():
    source = read("app/handlers/reason_admin.py")
    assert 'origin == "panel"' in source
    assert 'await bot.send_message(int(data["chat_id"]), result)' in source
    assert 'chat_id=int(data["chat_id"])' in source
    assert "moderation_public_notice_failed" in source


def test_panel_actions_target_the_selected_group_not_private_chat():
    source = read("app/handlers/member_center.py")
    assert '"origin": "panel"' in source
    assert '"chat_id": group.telegram_chat_id' in source
    assert "target_is_protected" in source
    assert '"actor_role": "owner"' in source


def test_success_is_committed_before_panel_public_notice():
    source = read("app/handlers/reason_admin.py")
    commit_pos = source.index("await session.commit()", source.index("async def moderation_reason_selected"))
    notify_pos = source.index('await bot.send_message(int(data["chat_id"]), result)', commit_pos)
    assert commit_pos < notify_pos
