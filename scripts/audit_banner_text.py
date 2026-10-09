#!/usr/bin/env python3
"""Audit: flag overly verbose banner headers like "🛑 Подтверждение бана".

History: moderation_durable_guard showed:
  "🛑 Подтверждение бана\\n\\n👤 Нарушитель: ...\\n\\nВыберите обычный бан..."
which was trimmed to just "👤 {name}" with action buttons. This audit keeps
new verbose banners from creeping back in.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "app"]

BANNED_PHRASES = [
    "Подтверждение бана",
    "Подтверждение мута",
    "Подтверждение кика",
    "Выберите обычный бан",
    "Выберите обычный мут",
    "сохранённых сообщений пользователя",
]

# Any handler text block longer than this that contains both an emoji header
# and the word "Выберите" is flagged as a verbose banner.
VERBOSE_RE = re.compile(r"[🛑⚠️🔴].{0,40}Выберите.{0,80}бан", re.IGNORECASE | re.DOTALL)


def main() -> int:
    errors: list[str] = []
    for base in SCAN_DIRS:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(ROOT)
            for phrase in BANNED_PHRASES:
                if phrase in text:
                    # allow the audit itself to mention the phrase
                    if path == Path(__file__):
                        continue
                    line_no = text.index(phrase)
                    line_no = text[:line_no].count("\n") + 1
                    errors.append(f"{rel}:{line_no}: banned banner phrase '{phrase}'")
            if VERBOSE_RE.search(text):
                # avoid double-reporting the same file
                if not any(str(rel) in e for e in errors):
                    errors.append(f"{rel}: verbose banner pattern (emoji header + 'Выберите ... бан')")

    if errors:
        print("Banner text audit failed:")
        for e in errors:
            print(f"  - {e}")
        print("\nFix: keep ban/mute prompts minimal — e.g. just '👤 {name}' with buttons.")
        return 1
    print("Banner text audit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
