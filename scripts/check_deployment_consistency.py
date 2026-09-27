from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def normalized_requirement(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def read_requirements(path: Path, seen: set[Path] | None = None) -> list[str]:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return []
    seen.add(path)
    values: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            values.extend(read_requirements(path.parent / line[3:].strip(), seen))
            continue
        values.append(line)
    return values


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def main() -> int:
    errors: list[str] = []

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    runtime = [normalized_requirement(x) for x in project.get("dependencies", [])]
    dev = [normalized_requirement(x) for x in project.get("optional-dependencies", {}).get("dev", [])]
    req_runtime = [normalized_requirement(x) for x in read_requirements(ROOT / "requirements.txt")]
    req_dev = [normalized_requirement(x) for x in read_requirements(ROOT / "requirements-dev.txt")]

    if runtime != req_runtime:
        errors.append("requirements.txt differs from project.dependencies in pyproject.toml")

    missing_dev = sorted(set(dev) - set(req_dev))
    if missing_dev:
        errors.append("requirements-dev.txt misses dev dependencies: " + ", ".join(missing_dev))
    missing_runtime_in_dev = sorted(set(runtime) - set(req_dev))
    if missing_runtime_in_dev:
        errors.append("requirements-dev.txt misses runtime dependencies: " + ", ".join(missing_runtime_in_dev))

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    if "FROM python:3.12" not in dockerfile:
        errors.append("Dockerfile Python version does not match requires-python >=3.12")
    if "pip install --no-cache-dir -r requirements.txt" not in dockerfile:
        errors.append("Dockerfile must install requirements.txt")
    if not re.search(r"^USER\s+bot\s*$", dockerfile, re.MULTILINE):
        errors.append("Dockerfile must run the application as non-root user 'bot'")

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for required_fragment in (
        "alembic upgrade head",
        "python -m app.preflight",
        "python -m app.main",
        "/readyz",
        "condition: service_healthy",
    ):
        if required_fragment not in compose:
            errors.append(f"docker-compose.yml misses required fragment: {required_fragment}")

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    if "python-version: '3.12'" not in ci and 'python-version: "3.12"' not in ci:
        errors.append("CI Python version must be 3.12")
    if "pip install -r requirements-dev.txt" not in ci:
        errors.append("CI must install requirements-dev.txt")
    if "./scripts/check.sh" not in ci:
        errors.append("CI must run scripts/check.sh")

    env = read_env(ROOT / ".env.example")
    required_env = {
        "BOT_TOKEN",
        "MIMORU_ENV",
        "SERVICE_OWNER_IDS",
        "DATABASE_URL",
        "REDIS_URL",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "HEALTH_HOST",
        "HEALTH_PORT",
    }
    missing_env = sorted(required_env - env.keys())
    if missing_env:
        errors.append(".env.example misses variables: " + ", ".join(missing_env))
    if env.get("MIMORU_ENV") != "production":
        errors.append(".env.example MIMORU_ENV must be production")

    database_url = env.get("DATABASE_URL", "")
    parsed = urlparse(database_url.replace("postgresql+asyncpg", "postgresql", 1))
    if parsed.hostname != "postgres":
        errors.append(".env.example DATABASE_URL must use Compose hostname 'postgres'")
    if parsed.username != env.get("POSTGRES_USER"):
        errors.append("DATABASE_URL username differs from POSTGRES_USER")
    if parsed.password != env.get("POSTGRES_PASSWORD"):
        errors.append("DATABASE_URL password differs from POSTGRES_PASSWORD")
    if parsed.path.lstrip("/") != env.get("POSTGRES_DB"):
        errors.append("DATABASE_URL database differs from POSTGRES_DB")
    if not env.get("REDIS_URL", "").startswith("redis://redis:"):
        errors.append(".env.example REDIS_URL must use Compose hostname 'redis'")

    backup = (ROOT / "scripts_backup.sh").read_text(encoding="utf-8")
    for variable in ("POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB", "BACKUP_RETENTION_DAYS"):
        if variable not in backup:
            errors.append(f"backup script does not reference {variable}")

    if errors:
        print("Deployment consistency check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Deployment consistency OK: dependencies, Docker, Compose, CI and .env agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
