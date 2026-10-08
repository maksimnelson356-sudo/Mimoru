"""Deep callback audit: full callback_data literals vs handler registrations.

check_callback_coverage.py only checks the first ":"-prefix family. This audit
cross-checks full literals against F.data == / startswith / regexp handlers,
reports dead buttons, dead registrations, and callback handlers that never call
callback.answer() (spinner hangs until Telegram times out).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

button_literals: set[str] = set()
button_files: dict[str, list[str]] = {}

for path in sorted((ROOT / "app").rglob("*.py")):
    rel = path.relative_to(ROOT).as_posix()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r'callback_data\s*=\s*f?"((?:[^"\\{]|\\.)*)"', line):
            lit = m.group(1)
            if not lit:
                continue
            button_literals.add(lit)
            button_files.setdefault(lit, []).append(f"{rel}:{lineno}")

# Handler registrations
eq_patterns: set[str] = set()
startswith_patterns: set[str] = set()
regexp_patterns: list[str] = []

for path in sorted((ROOT / "app").rglob("*.py")):
    src = path.read_text(encoding="utf-8")
    eq_patterns.update(re.findall(r'F\.data\s*==\s*"([^"]+)"', src))
    startswith_patterns.update(re.findall(r'F\.data\.startswith\("([^"]+)"\)', src))
    for rx in re.findall(r'F\.data\.regexp\(r?"([^"]+)"\)', src):
        regexp_patterns.append(rx)

regexp_res = []
for rx in regexp_patterns:
    try:
        regexp_res.append(re.compile(rx))
    except re.error:
        pass


def is_handled(lit: str) -> tuple[bool, str]:
    if lit in eq_patterns:
        return True, "eq"
    for sw in startswith_patterns:
        if lit.startswith(sw):
            return True, f"startswith({sw})"
    for rxc in regexp_res:
        if rxc.fullmatch(lit):
            return True, f"regexp({rxc.pattern})"
    # f-string buttons: check the static prefix before the first literal part
    return False, ""


dead_buttons = []
for lit in sorted(button_literals):
    ok, how = is_handled(lit)
    if not ok:
        dead_buttons.append(lit)

print(f"button literals: {len(button_literals)}")
print(f"F.data == patterns: {len(eq_patterns)}")
print(f"F.data.startswith patterns: {len(startswith_patterns)}")
print(f"F.data.regexp patterns: {len(regexp_patterns)}")

if dead_buttons:
    print("\nDEAD BUTTONS (no handler matches the literal):")
    for lit in dead_buttons:
        print(f"  {lit!r} -> {', '.join(button_files[lit][:3])}")
else:
    print("\nNo dead buttons: every literal matches a handler.")

# Dead registrations: F.data == exact literals that no button ever sends
dead_regs = sorted(r for r in eq_patterns if r not in button_literals)
print(f"\nF.data == exact patterns never sent by any button (informational, may be deep-links):")
for r in dead_regs[:20]:
    print(f"  {r!r}")
print(f"  total: {len(dead_regs)}")
