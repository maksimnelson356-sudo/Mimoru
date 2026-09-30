from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Свободные каталоги редакторов/агентов (.omo, .commandcode) не часть проекта —
# их временные дампы могут быть в любой кодировке и не должны ломать репозиторий.
SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", ".ruff_cache", ".mypy_cache", ".venv", "venv", ".omo", ".commandcode"}
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json",
    ".sh", ".env", ".example", ".dockerignore", ".gitignore",
}

errors: list[str] = []
warnings: list[str] = []
all_files: list[Path] = []
python_files: list[Path] = []
function_count = 0
class_count = 0
async_function_count = 0


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_text_candidate(path: Path) -> bool:
    if path.name in {"Dockerfile", "Makefile", "VERSION"}:
        return True
    if path.name.startswith(".env"):
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


for path in ROOT.rglob("*"):
    if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
        continue
    all_files.append(path)
    if path.suffix == ".py":
        python_files.append(path)

# Repository-wide text sanity checks.
conflict_re = re.compile(r"^(<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
private_key_re = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
telegram_token_re = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")

for path in all_files:
    if not is_text_candidate(path):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"{rel(path)}: text-like file is not valid UTF-8")
        continue
    if conflict_re.search(text):
        errors.append(f"{rel(path)}: unresolved merge conflict marker")
    if private_key_re.search(text):
        errors.append(f"{rel(path)}: private key material committed")
    if path.name != ".env.example" and telegram_token_re.search(text):
        errors.append(f"{rel(path)}: value resembling a Telegram bot token committed")
    if path.suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"{rel(path)}:{exc.lineno}: invalid JSON: {exc.msg}")

# Python AST audit: every Python file, every function/class, local imports, dangerous calls.
local_modules: set[str] = set()
for path in python_files:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts:
        for i in range(1, len(parts) + 1):
            local_modules.add(".".join(parts[:i]))

blocking_calls = {"time.sleep", "requests.get", "requests.post", "requests.put", "requests.delete", "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}
dangerous_calls = {"eval", "exec", "os.system", "pickle.loads", "pickle.load", "marshal.loads"}


def call_name(node: ast.Call) -> str:
    obj = node.func
    parts: list[str] = []
    while isinstance(obj, ast.Attribute):
        parts.append(obj.attr)
        obj = obj.value
    if isinstance(obj, ast.Name):
        parts.append(obj.id)
    return ".".join(reversed(parts))


class AuditVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.async_depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        global async_function_count, function_count
        async_function_count += 1
        function_count += 1
        self.async_depth += 1
        self.generic_visit(node)
        self.async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        global function_count
        function_count += 1
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        global class_count
        class_count += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = call_name(node)
        if name in dangerous_calls:
            errors.append(f"{rel(self.path)}:{node.lineno}: dangerous call {name}()")
        if name.startswith("subprocess."):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    errors.append(f"{rel(self.path)}:{node.lineno}: subprocess with shell=True")
        if self.async_depth and name in blocking_calls:
            errors.append(f"{rel(self.path)}:{node.lineno}: blocking {name}() inside async function")
        self.generic_visit(node)


for path in python_files:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=rel(path))
    except SyntaxError as exc:
        errors.append(f"{rel(path)}:{exc.lineno}: syntax error: {exc.msg}")
        continue

    # Duplicate top-level definitions are nearly always accidental shadowing.
    names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for name, count in Counter(names).items():
        if count > 1:
            errors.append(f"{rel(path)}: duplicate top-level definition {name!r} ({count} times)")

    # Validate absolute local imports without importing application code or requiring runtime env vars.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module
            if module.startswith(("app", "scripts", "tests", "alembic")):
                if module not in local_modules and not any(m.startswith(module + ".") for m in local_modules):
                    errors.append(f"{rel(path)}:{node.lineno}: unresolved local import {module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module.startswith(("app.", "scripts.", "tests.", "alembic.")):
                    if module not in local_modules and not any(m.startswith(module + ".") for m in local_modules):
                        errors.append(f"{rel(path)}:{node.lineno}: unresolved local import {module}")

    AuditVisitor(path).visit(tree)

# Telegram callback_data hard limit is 64 bytes. Literal callbacks must always fit.
callback_literal = re.compile(r"callback_data\s*=\s*([\"'])(.*?)\1")
for path in python_files:
    text = path.read_text(encoding="utf-8")
    for match in callback_literal.finditer(text):
        value = match.group(2)
        if len(value.encode("utf-8")) > 64:
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{rel(path)}:{line}: callback_data literal exceeds Telegram 64-byte limit")

# Every migration file must define both upgrade and downgrade, even if downgrade intentionally raises.
versions = ROOT / "alembic" / "versions"
if versions.exists():
    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel(path))
        defs = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = {"upgrade", "downgrade"} - defs
        if missing:
            errors.append(f"{rel(path)}: migration missing {', '.join(sorted(missing))}")

# Essential production files should never disappear silently.
required_paths = [
    "app/main.py", "app/middlewares.py", "app/db/models.py", "app/db/session.py",
    "Dockerfile", "docker-compose.yml", "requirements.txt", "requirements-dev.txt",
    ".github/workflows/ci.yml", "scripts/check.sh", "scripts/deploy.sh",
]
for raw in required_paths:
    if not (ROOT / raw).exists():
        errors.append(f"missing essential file: {raw}")

print(
    "Full codebase inventory: "
    f"{len(all_files)} files, {len(python_files)} Python files, "
    f"{function_count} functions ({async_function_count} async), {class_count} classes"
)
if warnings:
    for item in warnings:
        print("WARN", item)
if errors:
    for item in errors:
        print("ERROR", item)
    raise SystemExit(f"Codebase integrity audit failed with {len(errors)} error(s)")
print("Full codebase integrity audit: OK")
