"""Обновить ACTION_EMOJI в app/game_friendly_results.py.

Читает scripts/_collected_emoji.json (98 emoji + 6 вручную).
Обновляет константу ACTION_EMOJI в game_friendly_results.py.

Запуск из корня проекта:
    python scripts/update_action_emoji.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRIENDLY_PATH = PROJECT_ROOT / "app" / "game_friendly_results.py"
COLLECTED_PATH = PROJECT_ROOT / "scripts" / "_collected_emoji.json"

# 6 действий, у которых emoji не были собраны regex'ом
EXTRA_EMOJI: dict[str, str] = {
    "закопать": "⚰️",
    "убить": "☠️",
    "депортировать": "✈️",
    "разбудить": "⏰",
    "отомстить": "⚔️",
    "осудить": "⚖️",
}


def main() -> None:
    collected = json.loads(COLLECTED_PATH.read_text(encoding="utf-8"))
    all_emoji = {**collected, **EXTRA_EMOJI}
    print(f"Всего emoji: {len(all_emoji)}")

    text = FRIENDLY_PATH.read_text(encoding="utf-8")

    start_marker = "ACTION_EMOJI = {"
    start = text.find(start_marker)
    if start == -1:
        raise SystemExit("ACTION_EMOJI не найден")

    end = text.find("\n}", start)
    if end == -1:
        raise SystemExit("Конец ACTION_EMOJI не найден")
    end += 2

    lines = [start_marker]
    for action, emoji in sorted(all_emoji.items()):
        lines.append(f'    "{action}": "{emoji}",')
    lines.append("}")
    new_block = "\n".join(lines)

    new_text = text[:start] + new_block + text[end:]

    backup = FRIENDLY_PATH.with_suffix(".py.bak")
    shutil.copy2(FRIENDLY_PATH, backup)
    print(f"Бэкап: {backup}")

    FRIENDLY_PATH.write_text(new_text, encoding="utf-8")
    print(f"Обновлено: {FRIENDLY_PATH}")
    print(f"Было: {len(text)} байт, стало: {len(new_text)} байт")


if __name__ == "__main__":
    main()