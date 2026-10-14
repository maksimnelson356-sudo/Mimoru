from pathlib import Path

from app.services.moderation import ModerationOutcome


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _ban_branch() -> str:
    source = _source("app/services/moderation.py")
    return source.split('    if action == "ban":', 1)[1].split('    if action == "unban":', 1)[0]


def test_ban_cleanup_runs_after_ban_and_never_before() -> None:
    branch = _ban_branch()

    ban = branch.index("await bot.ban_chat_member")
    cleanup = branch.index("if cleanup:", ban)
    delete = branch.index("await bot.delete_messages", cleanup)
    outcome = branch.index("return _success(", delete)

    assert ban < cleanup < delete < outcome


def test_ban_cleanup_is_batched_and_keeps_telegram_window_margin() -> None:
    branch = _ban_branch()

    assert "timedelta(hours=47)" in branch
    assert "for i in range(0, cleanup_total, 100):" in branch
    assert "UserMessage.created_at > cutoff" in branch


def test_failed_cleanup_batch_is_logged_and_marked_incomplete() -> None:
    branch = _ban_branch()

    failed = branch.index("cleanup_failed_batches += 1")
    incomplete = branch.index("cleanup_incomplete = bool(", failed)
    warning = branch.index('"ban_cleanup_incomplete"', incomplete)

    assert failed < incomplete < warning
    assert "Очистка сообщений выполнена не полностью" in branch


def test_incomplete_cleanup_keeps_ban_successful_and_surfaces_it() -> None:
    source = _source("app/services/moderation.py")
    outcome_block = source.split("class ModerationOutcome(str):", 1)[1].split(
        "def log_action", 1
    )[0]

    for field in ("cleanup_requested", "cleanup_deleted", "cleanup_incomplete"):
        assert f"{field}: bool" in outcome_block or f"{field}: int" in outcome_block

    base = ModerationOutcome("ok", success=True, commit=True, public_notice=True)
    assert base.cleanup_incomplete is False
    assert base.cleanup_requested is False
    assert base.cleanup_deleted == 0

    partial = ModerationOutcome(
        "ok",
        success=True,
        commit=True,
        public_notice=True,
        cleanup_requested=True,
        cleanup_deleted=3,
        cleanup_incomplete=True,
    )
    assert partial.success is True
    assert partial.commit is True
    assert partial.cleanup_incomplete is True


def test_durable_ban_keeps_both_cleanup_sources() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def _durable_ban_execute", 1)[1]

    assert "cleanup = cleanup or bool(data.get(\"cleanup\"))" in body
    assert "cleanup=cleanup," in body
    # Параметр обработчика не должен затираться payload-значением.
    assert "cleanup = bool(data.get(\"cleanup\"))\n" not in body


def test_clean_button_forwards_cleanup_into_execute() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def durable_ban_clean", 1)[1].split(
        "async def durable_ban_cancel", 1
    )[0]

    assert "_durable_ban_execute(callback, bot, session, redis, cleanup=True)" in body
    assert 'callback_data=f"modreason:clean:{token}:{raw_reason_id}"' in guard


def test_reason_selection_forwards_pending_cleanup_flag() -> None:
    reasons = _source("app/handlers/reason_admin.py")
    body = reasons.split("async def moderation_reason_selected", 1)[1].split(
        "async def moderation_reason_cancel", 1
    )[0]

    assert 'cleanup=bool(data.get("cleanup"))' in body
    assert "cleanup=False," not in body


def test_pending_payloads_declare_cleanup_explicitly() -> None:
    member_center = _source("app/handlers/member_center.py")
    panel_payload = member_center.split('payload = {', 1)[1].split("}", 1)[0]
    assert '"cleanup": False' in panel_payload

    group = _source("app/handlers/group.py")
    group_payload = group.split("payload = {", 1)[1].split("}", 1)[0]
    assert '"cleanup": False' in group_payload

    command_modes = _source("app/handlers/moderation_command_modes.py")
    buttons_payload = command_modes.split("async def _open_buttons", 1)[1].split(
        "await redis.setex", 1
    )[0]
    assert '"cleanup": cleanup' in buttons_payload
    assert 'cleanup_requested = command_word == "банчистка"' in command_modes


def test_durable_ban_answers_partial_cleanup_to_moderator() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def _durable_ban_execute", 1)[1]

    detect = body.index('getattr(result, "cleanup_incomplete", False)')
    answer = body.index("Забанен, очистка сообщений выполнена не полностью.", detect)
    alert = body.index("show_alert=not result.success or cleanup_incomplete", answer)

    assert detect < answer < alert