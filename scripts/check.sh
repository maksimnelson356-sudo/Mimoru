#!/usr/bin/env sh
set -eu
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
python -m compileall -q app alembic tests
ruff check app tests scripts alembic --select E9,F63,F7,F82
pytest -q -m "not db"
python scripts/check_migrations.py
python scripts/check_schema_consistency.py
python scripts/check_router_registration.py
python scripts/check_deployment_consistency.py
python scripts/check_security_baseline.py
python scripts/check_operational_resilience.py
python scripts/check_functionality_surface.py
python scripts/check_callback_coverage.py
python scripts/check_release_consistency.py
python scripts/audit_navigation_buttons.py
python scripts/audit_all_buttons.py
python scripts/audit_fsm_states.py
python scripts/check_codebase_integrity.py
python scripts/audit_handler_contracts.py
python scripts/audit_spelling.py
python scripts/audit_owner_vs_deputy.py
python scripts/audit_ttl_leaks.py
python scripts/audit_banner_text.py
python scripts/audit_unused_routers.py
