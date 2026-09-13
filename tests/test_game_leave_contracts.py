from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _handle_leave_block(source: str) -> str:
    """Возвращает блок метода handle_leave из исходника."""
    block = source.split("async def handle_leave", 1)[1]
    # Отрезаем до следующего async def на том же уровне (с отступом 4)
    for marker in ["\n    async def ", "\n    def ", "\n    @"]:
        if marker in block:
            block = block.split(marker, 1)[0]
            break
    return block


SIMPLE_GAMES = [
    ("arena", "app/games/arena/game.py"),
    ("battleship", "app/games/battleship/game.py"),
    ("cards", "app/games/cards/game.py"),
    ("crocodile", "app/games/crocodile/game.py"),
    ("detective", "app/games/detective/game.py"),
    ("quiz", "app/games/quiz/game.py"),
    ("roulette", "app/games/roulette/game.py"),
    ("words", "app/games/words/game.py"),
]


def test_simple_games_import_leave_result():
    for name, path in SIMPLE_GAMES:
        source = read(path)
        assert "from app.games.base import BaseGame, LeaveResult" in source, f"{name}: нет импорта LeaveResult"


def test_simple_games_have_handle_leave():
    for name, path in SIMPLE_GAMES:
        source = read(path)
        assert "async def handle_leave" in source, f"{name}: нет handle_leave"
        block = _handle_leave_block(source)
        assert "LeaveResult.REMOVED" in block, f"{name}: handle_leave не возвращает REMOVED"
        
def test_mafia_import_leave_result():
    source = read("app/games/mafia/game.py")
    assert "from app.games.base import BaseGame, LeaveResult" in source


def test_mafia_handle_leave_logic():
    source = read("app/games/mafia/game.py")
    block = _handle_leave_block(source)
    assert 'player.role == "mafia"' in block, "mafia: нет проверки role == mafia"
    assert "LeaveResult.CANCELLED" in block, "mafia: не возвращает CANCELLED"
    assert "winner(" in block, "mafia: не вызывает winner()"
    assert "finish_game(" in block, "mafia: не вызывает finish_game()"
    assert "LeaveResult.REMOVED" in block, "mafia: не возвращает REMOVED для civilian"


def test_spy_import_leave_result():
    source = read("app/games/spy/game.py")
    assert "from app.games.base import BaseGame, LeaveResult" in source


def test_spy_handle_leave_logic():
    source = read("app/games/spy/game.py")
    block = _handle_leave_block(source)
    assert 'player.role == "spy"' in block, "spy: нет проверки role == spy"
    assert "LeaveResult.CANCELLED" in block, "spy: не возвращает CANCELLED"
    assert 'locals_count < 2' in block, "spy: нет проверки locals < 2"
    assert 'self._finish(session, game, "spy")' in block, "spy: не вызывает _finish с 'spy'"
    assert "LeaveResult.REMOVED" in block, "spy: не возвращает REMOVED для local"