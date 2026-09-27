from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_direct_warn_mute_ban_use_single_configurable_entrypoint() -> None:
    main = _source("app/main.py")
    modes = _source("app/handlers/moderation_command_modes.py")
    kick_retirement = _source("app/handlers/kick_retirement.py")
    group_commands = _source("app/handlers/group_commands.py")
    durable_guard = _source("app/handlers/moderation_durable_guard.py")

    assert "_disable_legacy_direct_moderation_handlers" not in main
    assert "moderation_reason_entry" not in kick_retirement
    assert "direct_reply_moderation" not in group_commands
    assert "durable_direct_reply" not in durable_guard
    assert "DIRECT_MODERATION_RE" not in group_commands

    assert "async def moderation_command_mode" in modes
    handler = modes.split("async def moderation_command_mode", 1)[1]
    assert "second_line_reason" in handler
    assert 'mode in {"text", "both"}' in handler
    assert "await _open_buttons(" in handler
    assert "await execute(" in handler
    assert handler.index("if use_direct:") < handler.index("await _open_buttons(")


def test_moderation_mode_callback_renders_configured_mode_screen() -> None:
    source = _source("app/handlers/moderation_command_modes.py")
    handler = source.split("async def moderation_mode", 1)[1].split(
        "@router.callback_query", 1
    )[0]
    assert "await _render_mode(callback, session, group)" in handler
    assert "await callback.answer()" in handler


def test_noop_callback_filter_covers_exact_and_namespaced_values() -> None:
    source = _source("app/handlers/members.py")
    assert 'F.data.regexp(r"^noop(?::|$)")' in source


def test_existing_unmanaged_telegram_admin_is_attached_without_promote_rewrite() -> None:
    source = _source("app/handlers/rank_provisioning_handlers.py")
    helper = source.split("async def _attach_existing_unmanaged_telegram_admin", 1)[1].split(
        "@router.callback_query", 1
    )[0]
    apply = source.split("async def safe_admin_access_apply", 1)[1].split(
        "@router.callback_query(F.data.regexp(r\"^rank_change", 1
    )[0]

    assert "member.status != ChatMemberStatus.ADMINISTRATOR" in helper
    assert "assignment.telegram_admin_managed" in helper
    assert "assignment.telegram_admin_managed = False" in helper
    assert '"telegram_rights_preserved": True' in helper
    assert "promote_chat_member" not in helper
    assert "_attach_existing_unmanaged_telegram_admin" in apply
    assert apply.index("_attach_existing_unmanaged_telegram_admin") < apply.index("provision_assignment")


def test_bot_only_demotion_failure_explains_correct_mode() -> None:
    source = _source("app/handlers/rank_provisioning_handlers.py")
    assert "Вы выбрали режим «Только Mimoru»" in source
    assert "выберите «Telegram + Mimoru»" in source
