"""Audit: callback handlers that never call callback.answer() -> spinner hangs."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Map: function name -> does its body call .answer() (or a *_answer helper)?
answers_direct: set[str] = set()
func_nodes: dict[str, ast.AsyncFunctionDef] = {}
issues: list[str] = []
checked = 0

for path in sorted((ROOT / "app").rglob("*.py")):
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        func_nodes.setdefault(node.name, node)
        calls_answer = any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "answer")
                or (isinstance(n.func, ast.Name) and n.func.id.endswith("_answer"))
            )
            for n in ast.walk(node)
        )
        if calls_answer:
            answers_direct.add(node.name)


def answers(name: str, depth: int = 0) -> bool:
    if name in answers_direct:
        return True
    node = func_nodes.get(name)
    if node is None or depth > 2:
        return False
    # delegation: calls other local functions passing callback
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and any(
                isinstance(a, ast.Name) and a.id == "callback" for a in n.args
            )
            and answers(n.func.id, depth + 1)
        ):
            return True
    return False


for path in sorted((ROOT / "app").rglob("*.py")):
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not any(
            "callback_query" in ast.unparse(d) for d in node.decorator_list
        ):
            continue
        checked += 1
        if not answers(node.name):
            issues.append(f"{rel}:{node.lineno} {node.name}")

print(f"callback handlers checked: {checked}")
if issues:
    print("Handlers WITHOUT callback.answer() anywhere in their call chain:")
    for i in issues:
        print(f"  {i}")
else:
    print("All callback handlers answer (directly or via delegation).")

