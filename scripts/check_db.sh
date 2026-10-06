#!/usr/bin/env sh
# Database-backed verification against a real PostgreSQL instance.
#
# scripts/check.sh deliberately runs without a database: the fast gate must stay
# runnable anywhere. This script covers what mocks cannot prove - that the whole
# migration chain actually applies, that the resulting schema matches the ORM,
# and that the constraints and locking primitives the workers rely on behave as
# expected on a real server.
#
# It never skips silently. Without a reachable database it fails loudly, so a
# green run always means the database contract was actually exercised.
set -eu
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ОШИБКА: DATABASE_URL не задан. Нужен живой PostgreSQL, например:" >&2
  echo "  docker compose -f docker-compose.test.yml up -d postgres" >&2
  exit 1
fi

echo "==> Ожидание готовности PostgreSQL..."
python - <<'PY'
import asyncio
import os
import sys

import asyncpg

dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def wait_for_database() -> int:
    last_error: Exception | None = None
    for _ in range(30):
        try:
            connection = await asyncpg.connect(dsn, timeout=5)
        except Exception as error:  # noqa: BLE001 - any failure means "not ready yet"
            last_error = error
            await asyncio.sleep(1)
            continue
        await connection.close()
        print("PostgreSQL доступен.")
        return 0
    print(f"ОШИБКА: PostgreSQL недоступен после 30 попыток: {last_error}", file=sys.stderr)
    return 1


raise SystemExit(asyncio.run(wait_for_database()))
PY

echo "==> Расширение alembic_version.version_num до VARCHAR(255)..."
python -m scripts.ensure_version_column

echo "==> Применение всей цепочки миграций на пустой базе..."
alembic upgrade head

echo "==> Проверка обратимости последней миграции (downgrade + upgrade)..."
# Downgrade paths used to run nowhere in CI. Round-trip the head step so an
# object created but never dropped, or dropped by a broken downgrade, fails here
# instead of the first time somebody needs to roll back.
alembic downgrade head-1
alembic upgrade head

echo "==> Preflight против живого PostgreSQL и Redis..."
python -m app.preflight

echo "==> Сверка живой схемы с ORM..."
# Run as a module: a bare script path puts scripts/ on sys.path, not the
# repository root, so `import app` would fail.
python -m scripts.check_live_schema

echo "==> Тесты против живой базы..."
pytest -q -m db
