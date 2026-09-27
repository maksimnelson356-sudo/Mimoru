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
