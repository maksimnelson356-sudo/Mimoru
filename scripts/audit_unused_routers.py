#!/usr/bin/env python3
"""Audit: routers registered in app/main.py that are never triggered.

Heuristic:
  * Parse `dp.include_routers(...)` in app/main.py to get the ordered list.
  * For each `something.router`, check that at least one handler in that
    module has a filter (F.*, Command, regexp, etc.) or that the module is
    a known shim (empty router kept for wiring compatibility).
  * Flag routers whose file contains no `@router.` handler decorators at all.

This is intentionally lenient — it only catches truly dead wiring (empty
shims that are no longer needed, or modules with no handlers).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app/main.py"

ALLOWLIST_EMPTY = {
    "fun_social",
    "fun_preferences",
    "fun_commands",
}

ROUTER_RE = re.compile(r"^\s*([\w_]+)\.router,?\s*$", re.MULTILINE)
HANDLER_RE = re.compile(r"@router\.(message|callback_query|chat_member|my_chat_member|edited_message|pre_checkout_query|chat_join_request)")


def main() -> int:
    text = MAIN.read_text(encoding="utf-8")
    block_m = re.search(r"dp\.include_routers\((.*?)\)", text, re.DOTALL)
    if not block_m:
        print("Unused routers audit: could not find dp.include_routers in app/main.py")
        return 1
    block = block_m.group(1)
    routers = ROUTER_RE.findall(block)
    errors: list[str] = []
    for name in routers:
        if name in ALLOWLIST_EMPTY:
            continue
        # Map router name to file: e.g. group_commands -> app/handlers/group_commands.py
        # Some routers live outside handlers (e.g. in app/games).
        candidates = list((ROOT / "app").rglob(f"{name}.py"))
        if not candidates:
            # name may be like `dashboard` -> app/handlers/dashboard.py etc.
            continue
        # If any candidate has handlers, consider it OK.
        has_handler = False
        for cand in candidates:
            if HANDLER_RE.search(cand.read_text(encoding="utf-8")):
                has_handler = True
                break
        if not has_handler:
            errors.append(f"{name}.router has no @router.* handlers (dead wiring?)")

    if errors:
        print("Unused routers audit failed:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("Unused routers audit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
