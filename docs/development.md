Development notes for Mimoru bot

- Local startup steps:
  - Copy .env.example to .env and fill secrets (BOT_TOKEN, DB, Redis as needed for local tests).
  - Run docker-compose -f docker-compose.yml up -d to start bot, DB, Redis (if you want to integrate with docker).
  - Or run preflight to validate configuration: python app/preflight.py (adjust path if needed).

- Health checks:
  - /healthz and /readyz endpoints exposed by the HealthServer in app/health.py.
  - ReadyZ depends on DB connectivity and Redis ping; use via http client to confirm readiness.

- How to run tests:
  - pip install -e .[dev]
  - pytest -q
  - Use markers to skip DB tests when not configured: pytest -m "not db" for fast gate.
