"""Собрать trailing emoji, убрать их из шаблонов, обновить ACTION_EMOJI.

Запуск из корня проекта:
    python scripts/apply_emoji.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.action_templates import ACTION_TEMPLATES

TEMPLATES_PATH = PROJECT_ROOT / "app" / "action_templates.py"
FRIENDLY_PATH = PROJECT_ROOT / "app" / "game_friendly_results.py"

EMOJI_RANGES = (
    (0x1F000, 0x1FFFF),
    (0x2600, 0x27BF),
    (0x2190, 0x21FF),
    (0x2300, 0x23FF),
)
VS16 = "\ufe0f"


def _is_emoji_token(token: str) -> bool:
    return any(
        any(lo <= ord(c) <= hi for lo, hi in EMOJI_RANGES)
        for c in token
    )


def _strip_trailing_emoji(line: str) -> str:
    """Убирает trailing emoji из строки шаблона (формат: ..." или ...",)."""
    # Разбить на "текст" + опциональная запятая
    m = re.match(r'^(\s*)(.*?)(,?)(\s*)$', line)
    if not m:
        return line
    indent, body, comma, tail = m.groups()

    # Убрать закрывающие кавычки из body
    if not (body.endswith('"') or body.endswith("'")):
        return line
    quote = body[-1]
    inner = body[:-1]

    # Убрать trailing emoji + пробелы внутри кавычек
    inner_clean = re.sub(
        rf"(\s*[^\s]*?)(?:\s*[{re.escape(VS16)}])*\s*$",
        "",
        inner,
    )

    # Более простой и надёжный способ: разбить по пробелам
    parts = inner.rsplit(" ", 1)
    while len(parts) == 2 and _is_emoji_token(parts[1].rstrip(VS16)):
        inner = parts[0]
        parts = inner.rsplit(" ", 1)

    return f"{indent}{inner}{quote}{comma}{tail}"


def collect_emojis() -> dict[str, str]:
    result: dict[str, str] = {}
    for action, variants in ACTION_TEMPLATES.items():
        for variant in variants:
            tokens = variant.strip().split()
            if not tokens:
                continue
            last = tokens[-1]
            if _is_emoji_token(last):
                result[action] = last
                break
    return result


def strip_templates() -> int:
    backup = TEMPLATES_PATH.with_suffix(".py.bak")
    shutil.copy2(TEMPLATES_PATH, backup)
    lines = TEMPLATES_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    for i, line in enumerate(lines):
        if '"' not in line and "'" not in line:
            continue
        # Только строки, содержащие шаблон (есть {actor})
        if "{actor}" not in line and "{target" not in line:
            continue
        new_line = _strip_trailing_emoji(line)
        if new_line != line:
            lines[i] = new_line
            changed += 1
    TEMPLATES_PATH.write_text("".join(lines), encoding="utf-8")
    return changed


def main() -> None:
    emojis = collect_emojis()
    print(f"Собрано emoji: {len(emojis)}")

    # Сохранить JSON
    out_json = PROJECT_ROOT / "scripts" / "_collected_emoji.json"
    out_json.write_text(
        json.dumps(emojis, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Сохранено: {out_json}")

    # Показать без emoji (если есть)
    without = [a for a in ACTION_TEMPLATES if a not in emojis]
    if without:
        print(f"Без emoji: {len(without)}")
        for a in without:
            print(f"  {a}")

    # Убрать trailing emoji из шаблонов
    changed = strip_templates()
    print(f"Обработано строк в action_templates.py: {changed}")
    print(f"Бэкап: {TEMPLATES_PATH.with_suffix('.py.bak')}")


if __name__ == "__main__":
    main()