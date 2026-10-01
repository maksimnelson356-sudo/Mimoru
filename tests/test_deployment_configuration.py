from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_example_declares_production_runtime() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MIMORU_ENV=production" in env


def test_deploy_rejects_untracked_files() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    assert 'git status --porcelain' in deploy
    assert '--untracked-files=no' not in deploy


def test_deployment_consistency_checks_production_env() -> None:
    checker = (ROOT / "scripts/check_deployment_consistency.py").read_text(
        encoding="utf-8"
    )
    assert '"MIMORU_ENV"' in checker
    assert 'MIMORU_ENV must be production' in checker


def test_ci_verifies_migrations_against_a_real_postgres() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "./scripts/check_db.sh" in ci
    assert "postgres:17-alpine" in ci
    assert "redis:7-alpine" in ci


def test_fast_gate_stays_runnable_without_a_database() -> None:
    check = (ROOT / "scripts/check.sh").read_text(encoding="utf-8")
    assert 'pytest -q -m "not db"' in check
    assert "check_db.sh" not in check


def test_database_check_fails_loudly_instead_of_skipping() -> None:
    check_db = (ROOT / "scripts/check_db.sh").read_text(encoding="utf-8")
    assert "DATABASE_URL" in check_db
    assert "exit 1" in check_db
    for stage in (
        "scripts.ensure_version_column",
        "alembic upgrade head",
        "python -m app.preflight",
        'pytest -q -m db',
    ):
        assert stage in check_db
