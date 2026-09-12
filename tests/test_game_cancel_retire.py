"""Тесты для #9a: game_cancel должен снимать stale-кнопки.

Проверяют contract-стилем, что:
- retire_active_messages импортирован в app/games/handlers.py;
- game_cancel вызывает retire_active_messages ДО close_lobby_message;
- retire вызывается с game_id и replacement_text;
- show_alert=True сохранён для permission denied в game_cancel и других функциях.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── #9a: retire_active_messages в game_cancel ────────────────────────────


class TestGameCancelRetiresMessages:
    def test_import_present(self) -> None:
        source = _source("app/games/handlers.py")
        assert "from app.games.messages import retire_active_messages" in source

    def test_game_cancel_block_uses_retire(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def game_cancel", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "retire_active_messages" in block

    def test_retire_called_before_close_lobby(self) -> None:
        """retire_active_messages должен вызываться ДО close_lobby_message."""
        source = _source("app/games/handlers.py")
        block = source.split("async def game_cancel", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        retire_pos = block.find("retire_active_messages")
        close_pos = block.find("close_lobby_message")
        assert retire_pos != -1, "retire_active_messages не найден в game_cancel"
        assert close_pos != -1, "close_lobby_message не найден в game_cancel"
        assert retire_pos < close_pos, (
            "retire_active_messages должен вызываться до close_lobby_message"
        )

    def test_retire_uses_game_id_and_replacement(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def game_cancel", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "game_id=game.id" in block
        assert "replacement_text=" in block

    def test_cancel_reason_preserved(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def game_cancel", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert 'reason="cancelled_by_user"' in block


# ── #9b: show_alert=True в permission denied ─────────────────────────────


class TestPermissionDeniedHasShowAlert:
    """Все permission-denied ответы в game handlers должны иметь show_alert=True."""

    def test_game_cancel_not_creator_has_show_alert(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def game_cancel", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "может создатель или администратор" in block
        idx = block.find("может создатель или администратор")
        snippet = block[max(0, idx - 200) : idx + 200]
        assert "show_alert=True" in snippet

    def test_callback_game_group_has_show_alert(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def _callback_game_group", 1)[1].split(
            "async def _is_joined_player", 1
        )[0]
        assert block.count("show_alert=True") >= 3

    def test_game_leave_lobby_has_show_alert(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def game_leave_lobby", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "show_alert=True" in block

    def test_game_start_lobby_has_show_alert(self) -> None:
        source = _source("app/games/handlers.py")
        block = source.split("async def game_start_lobby", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "show_alert=True" in block


# ── #9a: admin cancel уже использует retire ──────────────────────────────


class TestAdminCancelUsesRetire:
    def test_admin_cancel_confirm_uses_retire(self) -> None:
        source = _source("app/games/admin_handlers.py")
        block = source.split("async def game_admin_cancel_confirm", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "retire_active_messages" in block
        assert "replacement_text=" in block