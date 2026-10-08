#!/usr/bin/env bash
set -euo pipefail

# Simple server health validation workflow after deployment

echo "Starting deployment health checks..."

echo "1) Docker Compose config"
docker compose config

echo "2) Bring up services (build if needed)"
docker compose up -d --build

echo "3) Running container status"
docker compose ps

# Bot container specific checks

echo "4) Bot container logs (last 200)"
docker compose logs --tail=200 bot


echo "5) Apply migrations inside container and prerequisites"
docker compose exec bot alembic upgrade head || true

echo "6) Run preflight inside container"
docker compose exec bot python -m app.preflight || true

echo "7) Health checks (localhost:8080)"
if command -v curl >/dev/null 2>&1; then
  curl -sS http://127.0.0.1:8080/healthz && echo
  curl -sS http://127.0.0.1:8080/readyz || true && echo
else
  echo "curl not found, skipping HTTP health checks"
fi

echo "Health checks finished."
