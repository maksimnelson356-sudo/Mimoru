from pathlib import Path

from app.services.moderation import ModerationOutcome

ROOT = Path(__file__).resolve().parents[1]


def test_moderation_outcome_remains_string_compatible() -> None:
    result = ModerationOutcome(
        "Telegram отказал",
        success=False,
        commit=False,
        public_notice=False,
    )
    assert isinstance(result, str)
    assert str(result) == "Telegram отказал"
    assert result.success is False
    assert result.commit is False
    assert result.public_notice is False


def test_execute_distinguishes_failure_partial_and_success() -> None:
    source = (ROOT / "app/services/moderation.py").read_text(encoding="utf-8")
    assert "def _failure(" in source
    assert "def _partial(" in source
    assert "def _success(" in source
    assert 'return _failure(' in source
    assert 'Telegram не позволил забанить пользователя' in source
    assert "return _partial(" in source
    assert "return _success(" in source
    assert "manual_action_notice" in source


def test_reason_callback_uses_outcome_flags_before_success_ui() -> None:
    source = (ROOT / "app/handlers/reason_admin.py").read_text(encoding="utf-8")
    handler = source.split("async def moderation_reason_selected", 1)[1]
    assert "if result.commit:" in handler
    assert "if not result.success:" in handler
    assert "if result.public_notice:" in handler
    assert "❌ Действие не выполнено" in handler
    assert "⚠️ Действие выполнено частично" in handler

    failure_pos = handler.index("if not result.success:")
    success_pos = handler.index("✅ Действие выполнено", failure_pos)
    assert failure_pos < success_pos
