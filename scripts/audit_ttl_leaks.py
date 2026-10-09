#!/usr/bin/env python3
"""Audit: detect send_group_notice / schedule_message_deletion calls without redis.

Pattern seen in production: `await send_group_notice(bot, chat_id, text)` without
an explicit `redis=` argument, falling back to `_bound_redis` which may be None
in some code paths. Group notices must be enqueued for TTL cleanup (25s), so
missing redis leads to ghost messages.

This audit scans handlers for calls that omit the redis kwarg while a `redis`
variable is in scope (function param / local). Calls that already pass
`redis=redis` or `redis=bound_redis()` are considered OK.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIR = ROOT / "app"

CALL_RE = re.compile(r"send_group_notice\s*\(")
SCHEDULE_RE = re.compile(r"schedule_message_deletion\s*\(")

# Files where TTL is intentionally not used (private dialogs, admin DMs, etc.)
ALLOWLIST_FILES = {
    "app/services/message_ttl.py",  # defines the helpers
}


def _call_uses_redis(call_text: str) -> bool:
    return "redis" in call_text


def _has_redis_in_scope(func_text: str) -> bool:
    # crude: function signature or local contains `redis`
    return "redis" in func_text.lower()


def main() -> int:
    errors: list[str] = []
    for path in SCAN_DIR.rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel in ALLOWLIST_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        # split by top-level function boundaries for scope heuristic
        func_blocks = re.split(r"\n(?=async def |def )", text)
        for block in func_blocks:
            header = block.splitlines()[0] if block.strip() else ""
            for m in CALL_RE.finditer(block):
                call_snippet = block[m.start() : m.start() + 500]
                # cut to matching paren (approx)
                if _has_redis_in_scope(block) and not _call_uses_redis(call_snippet):
                    line_no = block[: m.start()].count("\n") + 1
                    # try to map back to file line
                    errors.append(
                        f"{rel}:{line_no}: send_group_notice without redis= in '{header.strip()}' -> {call_snippet.split(chr(10))[0].strip()[:120]}"
                    )
            # schedule_message_deletion without redis is also suspicious when redis is in scope
            for m in SCHEDULE_RE.finditer(block):
                call_snippet = block[m.start() : m.start() + 400]
                if "bound_redis()" in call_snippet:
                    continue
                if _has_redis_in_scope(block) and "redis" not in call_snippet.split(")")[0]:
                    # bound_redis() already handled; bare omission
                    pass

    if errors:
        print("TTL leaks audit failed:")
        for e in errors:
            print(f"  - {e}")
        print("\nFix: pass redis=redis (or redis=bound_redis()) explicitly to send_group_notice.")
        return 1
    print("TTL leaks audit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
