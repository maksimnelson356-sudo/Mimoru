from __future__ import annotations

import inspect
from pathlib import Path

from app.games.base import BaseGame, LeaveResult
from app.games.manager import GameManager


ROOT = Path(__file__).parent.parent


def _source(module_or_class) -> str:
    """Вернуть исходный код модуля/класса/функции как строку."""
    return inspect.getsource(module_or_class)


# ──────────────────────────────────────────────────────────────
# TestLeaveResult — проверки enum
# ──────────────────────────────────────────────────────────────
class TestLeaveResult:
    def test_enum_members_exist(self) -> None:
        assert hasattr(LeaveResult, "REMOVED")
        assert hasattr(LeaveResult, "CANCELLED")
        assert hasattr(LeaveResult, "REJECTED")

    def test_enum_values(self) -> None:
        assert LeaveResult.REMOVED.value == "removed"
        assert LeaveResult.CANCELLED.value == "cancelled"
        assert LeaveResult.REJECTED.value == "rejected"

    def test_enum_iterable(self) -> None:
        members = list(LeaveResult)
        assert len(members) == 3
        assert LeaveResult.REMOVED in members
        assert LeaveResult.CANCELLED in members
        assert LeaveResult.REJECTED in members


# ──────────────────────────────────────────────────────────────
# TestBaseGameHandleLeave — contract base.py
# ──────────────────────────────────────────────────────────────
class TestBaseGameHandleLeave:
    def test_handle_leave_exists(self) -> None:
        assert hasattr(BaseGame, "handle_leave"), "BaseGame должен иметь метод handle_leave"

    def test_handle_leave_is_coroutine(self) -> None:
        method = getattr(BaseGame, "handle_leave")
        assert inspect.iscoroutinefunction(method), "handle_leave должен быть coroutine (async def)"

    def test_handle_leave_not_abstractmethod(self) -> None:
        """handle_leave НЕ должен быть @abstractmethod — он имеет default-реализацию."""
        method = getattr(BaseGame, "handle_leave")
        # Проверяем через исходный код: нет декоратора @abstractmethod
        src = _source(method)
        assert "@abstractmethod" not in src, "handle_leave не должен быть abstractmethod"

    def test_handle_leave_default_returns_removed(self) -> None:
        """Default-реализация возвращает LeaveResult.REMOVED."""
        src = _source(BaseGame.handle_leave)
        assert "LeaveResult.REMOVED" in src, "Default реализация должна возвращать LeaveResult.REMOVED"

    def test_handle_leave_signature(self) -> None:
        """Проверка сигнатуры: (self, session, game, *, actor_telegram_id) -> LeaveResult."""
        sig = inspect.signature(BaseGame.handle_leave)
        params = list(sig.parameters.keys())
        assert params[:3] == ["self", "session", "game"], f"Первые параметры: {params[:3]}"
        # actor_telegram_id — keyword-only
        actor_param = sig.parameters.get("actor_telegram_id")
        assert actor_param is not None, "Должен быть параметр actor_telegram_id"
        assert actor_param.kind == inspect.Parameter.KEYWORD_ONLY, "actor_telegram_id должен быть keyword-only"
        # return annotation
        assert sig.return_annotation is LeaveResult or str(sig.return_annotation) == "LeaveResult"


# ──────────────────────────────────────────────────────────────
# TestManagerLeaveRunning — contract manager.py
# ──────────────────────────────────────────────────────────────
class TestManagerLeaveRunning:
    def test_leave_running_exists(self) -> None:
        assert hasattr(GameManager, "leave_running"), "GameManager должен иметь метод leave_running"

    def test_leave_running_is_coroutine(self) -> None:
        method = getattr(GameManager, "leave_running")
        assert inspect.iscoroutinefunction(method), "leave_running должен быть coroutine (async def)"

    def test_leave_running_checks_status(self) -> None:
        """leave_running проверяет RUNNING/RECOVERING статус."""
        src = _source(GameManager.leave_running)
        assert "RUNNING" in src and "RECOVERING" in src, "Должна быть проверка статуса RUNNING/RECOVERING"
        assert "GameSessionStatus" in src, "Должен использоваться GameSessionStatus"

    def test_leave_running_creator_cancelled(self) -> None:
        """Если creator — возвращает CANCELLED и вызывает cancel_game."""
        src = _source(GameManager.leave_running)
        assert "creator_telegram_id" in src, "Должна быть проверка creator_telegram_id"
        assert "LeaveResult.CANCELLED" in src, "Для creator должен возвращаться CANCELLED"
        assert "cancel_game" in src, "Должен вызываться cancel_game"

    def test_leave_running_calls_engine_handle_leave(self) -> None:
        """Для обычного игрока вызывает engine.handle_leave."""
        src = _source(GameManager.leave_running)
        assert "engine.handle_leave" in src or "engine.handle_leave" in src.replace(" ", ""), \
            "Должен вызываться engine.handle_leave"
        assert "actor_telegram_id" in src, "Должен передавать actor_telegram_id в engine"

    def test_leave_running_marks_player_left(self) -> None:
        """При REMOVED — помечает player.status = 'left'."""
        src = _source(GameManager.leave_running)
        assert "player.status" in src and "left" in src, "Должно быть обновление player.status = 'left'"
        assert "left_at" in src, "Должно быть заполнение left_at"

    def test_leave_running_signature(self) -> None:
        """Проверка сигнатуры: (self, session, *, game_id, user_telegram_id) -> LeaveResult."""
        sig = inspect.signature(GameManager.leave_running)
        params = list(sig.parameters.keys())
        assert params[:2] == ["self", "session"]
        assert "game_id" in params
        assert "user_telegram_id" in params
        game_id_param = sig.parameters["game_id"]
        user_param = sig.parameters["user_telegram_id"]
        assert game_id_param.kind == inspect.Parameter.KEYWORD_ONLY
        assert user_param.kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.return_annotation is LeaveResult or str(sig.return_annotation) == "LeaveResult"


# ──────────────────────────────────────────────────────────────
# TestHandlerRegistered — contract handlers.py
# ──────────────────────────────────────────────────────────────
class TestHandlerRegistered:
    def test_game_leave_running_handler_exists(self) -> None:
        handlers_py = ROOT / "app" / "games" / "handlers.py"
        src = handlers_py.read_text(encoding="utf-8")
        assert "game_leave_running" in src, "В handlers.py должен быть handler game_leave_running"

    def test_handler_uses_gm_leave_regexp(self) -> None:
        handlers_py = ROOT / "app" / "games" / "handlers.py"
        src = handlers_py.read_text(encoding="utf-8")
        assert 'gm:leave:' in src, "Handler должен использовать callback_data regexp gm:leave:"
        assert "regexp" in src, "Должен быть декоратор с regexp"

    def test_handler_uses_manager_leave_running(self) -> None:
        handlers_py = ROOT / "app" / "games" / "handlers.py"
        src = handlers_py.read_text(encoding="utf-8")
        assert "manager.leave_running" in src, "Handler должен вызывать manager.leave_running"

    def test_handler_processes_cancelled_and_removed(self) -> None:
        handlers_py = ROOT / "app" / "games" / "handlers.py"
        src = handlers_py.read_text(encoding="utf-8")
        assert "LeaveResult.CANCELLED" in src, "Должна быть обработка CANCELLED"
        assert "LeaveResult.REMOVED" in src, "Должна быть обработка REMOVED"
        assert "retire_active_messages" in src, "Для CANCELLED должен вызываться retire_active_messages"

    def test_handler_imports_leave_result(self) -> None:
        handlers_py = ROOT / "app" / "games" / "handlers.py"
        src = handlers_py.read_text(encoding="utf-8")
        assert "from app.games.base import LeaveResult" in src, "Должен быть импорт LeaveResult"


# ──────────────────────────────────────────────────────────────
# TestExitButtonInPanel — contract panels.py
# ──────────────────────────────────────────────────────────────
class TestExitButtonInPanel:
    def test_panel_markup_has_leave_button_for_running(self) -> None:
        panels_py = ROOT / "app" / "games" / "panels.py"
        src = panels_py.read_text(encoding="utf-8")
        # Проверяем, что в panel_markup есть кнопка для RUNNING/RECOVERING
        assert "gm:leave:" in src, "В panel_markup должна быть кнопка с callback_data gm:leave:"
        assert "RUNNING" in src and "RECOVERING" in src, "Кнопка должна появляться для RUNNING/RECOVERING"
        assert "GameSessionStatus" in src, "Должен использоваться GameSessionStatus"

    def test_panel_imports_game_session_status(self) -> None:
        panels_py = ROOT / "app" / "games" / "panels.py"
        src = panels_py.read_text(encoding="utf-8")
        assert "from app.games.enums import" in src and "GameSessionStatus" in src, \
            "Должен быть импорт GameSessionStatus"

    def test_leave_button_text(self) -> None:
        panels_py = ROOT / "app" / "games" / "panels.py"
        src = panels_py.read_text(encoding="utf-8")
        assert "Выйти из игры" in src or "➖" in src, "Текст кнопки должен содержать 'Выйти из игры' или эмодзи"