#!/usr/bin/env python3
"""Audit: owner-only gates that should also allow deputy_owner.

Background: Deputy Owner (DEPUTY_OWNER, level 90, ROLE_CEILINGS=all) is
documented as having the same rights as the owner. Several administration
and rank-policy screens gated only on `group.owner_telegram_id == user_id`
(plus service_owner), which caused "Нет доступа" for deputy owners.

This audit scans Python handlers for patterns that look like privileged gates
and verifies they delegate to the deputy-aware helper instead of a raw owner
check. The allowlist below enumerates files/lines that are intentionally
owner-only (billing, ownership transfer, etc.) and are not subject to the
deputy rule.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Genuinely owner-only operations — deputy must NOT be granted.
ALLOWLIST_SUBSTRINGS: list[str] = [
    "plan_catalog",
    "billing.py",
    "operations_center.py:106",
    "group_onboarding_flow.py:330",
    "service_owner_directory.py",
    "seller_owner_telegram_id",
    "promo_redemption",
    "services/invite_execution.py",
    "services/repositories.py",
    "telegram_roles.py:150",
    # tests and migrations
    "tests/",
    "alembic/",
]

# Regex for a raw owner check without deputy awareness.
OWNER_CHECK_RE = re.compile(r"group\.owner_telegram_id\s*(==|!=)")
EXCLUDE_FILES = {
    "app/services/ranks.py",
    "app/services/access.py",
    "app/handlers/panel.py",
    "app/handlers/group_onboarding_flow.py",
    "app/handlers/service_owner_directory.py",
    "app/handlers/telegram_roles.py",
    "app/services/invite_execution.py",
}

# Helper that is considered deputy-aware.
DEPUTY_MARKERS = (
    "deputy_owner",
    "DEPUTY_OWNER",
    "can_manage_roles",
    "_owner_group",
    "can_manage_group",
)


def _is_allowlisted(path: Path, line: str) -> bool:
    rel = str(path.relative_to(ROOT))
    hay = f"{rel}:{line}"
    return any(s in hay or s in rel for s in ALLOWLIST_SUBSTRINGS)


def main() -> int:
    errors: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in EXCLUDE_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            if not OWNER_CHECK_RE.search(line):
                continue
            if _is_allowlisted(path, line):
                continue
            # If the surrounding 20 lines mention deputy-aware helpers, assume OK.
            window = "\n".join(text.splitlines()[max(0, i - 20) : i + 20])
            if any(m in window for m in DEPUTY_MARKERS):
                continue
            # Heuristic: gates that also check is_service_owner in the same
            # statement are often the ones that forgot deputy.
            if "is_service_owner" in line or "is_service_owner" in window:
                errors.append(f"{rel}:{i}: owner-only gate without deputy_owner: {line.strip()}")
            elif "Настраивать" in window or "только владелец" in window:
                errors.append(f"{rel}:{i}: owner-only gate without deputy_owner: {line.strip()}")

    if errors:
        print("Owner vs deputy audit failed:")
        for e in errors:
            print(f"  - {e}")
        print("\nFix: delegate to _owner_group / can_manage_roles or check DEPUTY_OWNER explicitly.")
        return 1
    print("Owner vs deputy audit OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
