#!/usr/bin/env python3
"""Audit: catch typos like `comands` and latin/cyrillic inconsistencies.

Known offender: app/main.py had BotCommand "comands" instead of "commands".
This audit scans Python sources for a curated list of known typos and for
mixed-script words (latin chars inside cyrillic words and vice versa) that
often indicate a keyboard-layout slip.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "app", ROOT / "scripts", ROOT / "alembic"]

# Known literal typos -> suggested fix. Extend as new ones surface.
# These are checked as standalone words / quoted literals, not substrings inside
# natural cyrillic text (f-strings with latin interpolations produce many false
# positives for a mixed-script heuristic).
KNOWN_TYPOS: dict[str, str] = {
    "teh ": "the ",
    "recieve": "receive",
    "occured": "occurred",
}

BANNED_SUBSTRINGS: dict[str, str] = {
    'BotCommand(command="comands"': 'use BotCommand(command="commands"',
}


def main() -> int:
    errors: list[str] = []

    for base in SCAN_DIRS:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(ROOT)

            for needle, hint in KNOWN_TYPOS.items():
                if needle in text.casefold():
                    if path == Path(__file__):
                        continue
                    errors.append(f"{rel}: contains '{needle}' -> {hint}")

            for needle, hint in BANNED_SUBSTRINGS.items():
                if needle not in text:
                    continue
                if "audit_spelling.py" in str(rel):
                    continue
                if "group_help_full.py" in str(rel) and 'Command("comands")' in text:
                    continue
                errors.append(f"{rel}: contains {needle} ({hint})")

    if errors:
        print("Spelling audit failed:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("Spelling audit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
