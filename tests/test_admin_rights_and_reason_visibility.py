from pathlib import Path

from app.services.ui import manual_action_notice


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_telegram_rank_assignment_preflights_can_promote_members() -> None:
    handler = _source("app/handlers/admin_access_mode.py")
    helper = handler.split("async def _telegram_provisioning_ready", 1)[1].split(
        "async def _apply_assignment", 1
    )[0]
    apply = handler.split("async def _apply_assignment", 1)[1].split(
        "def _mode_markup", 1
    )[0]

    assert 'getattr(bot_member, "can_promote_members", False)' in helper
    assert "нет права назначать администраторов" in helper
    assert "Добавлять администраторов" in helper
    assert "ready, readiness_error = await _telegram_provisioning_ready(bot, group)" in apply
    assert apply.index("_telegram_provisioning_ready") < apply.index("promote_admin")


def test_selected_reason_is_forwarded_through_durable_callback_chain() -> None:
    durable = _source("app/handlers/moderation_durable_guard.py")
    reasons = _source("app/handlers/reason_admin.py")

    durable_callback = durable.split("async def durable_reason_action", 1)[1].split(
        "async def durable_reply_unmute", 1
    )[0]
    reason_callback = reasons.split("async def moderation_reason_selected", 1)[1].split(
        "async def moderation_reason_cancel", 1
    )[0]

    assert "reason=reason.name" in durable_callback
    assert "reason=reason.name" in reason_callback


def test_manual_warn_mute_and_ban_notices_show_selected_reason() -> None:
    selected_reason = "Оскорбление участников"
    common = dict(
        target="Нарушитель",
        moderator="Администратор",
        reason=selected_reason,
        actor_role="admin",
    )

    warn = manual_action_notice(
        action="warn",
        warning_count=1,
        warning_limit=3,
        **common,
    )
    mute = manual_action_notice(
        action="mute",
        duration_seconds=300,
        **common,
    )
    ban = manual_action_notice(
        action="ban",
        duration_seconds=None,
        **common,
    )

    assert selected_reason in warn
    assert selected_reason in mute
    assert selected_reason in ban
