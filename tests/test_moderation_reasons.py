from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_default_reasons_cover_expected_actions():
    source = (ROOT / "app/services/moderation_reasons.py").read_text(encoding="utf-8")
    for text in [
        "Флуд",
        "Оскорбление участников",
        "Спам",
        "Реклама",
        "Без пошлостей",
        "Нарушение правил",
    ]:
        assert text in source
    for action in ["warn", "mute", "ban"]:
        assert f'"{action}"' in source
    assert '"kick"' not in source


def test_new_default_reason_is_backfilled_for_existing_groups():
    migration = ROOT / "alembic/versions/0053_default_reason_without_obscenity.py"
    assert migration.exists()
    source = migration.read_text(encoding="utf-8")
    assert 'revision = "0053_default_reason_without_obscenity"' in source
    assert 'down_revision = "0052_warning_expiry_hours"' in source
    assert "Без пошлостей" in source
    assert "NOT EXISTS" in source


def test_reason_migration_and_model_exist():
    models = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
    migration = ROOT / "alembic/versions/0024_moderation_reasons.py"
    assert "class ModerationReason" in models
    assert "moderation_reasons_initialized" in models
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision = "0023_ad_order_payments"' in text
    assert '"moderation_reasons"' in text


def test_group_moderation_commands_use_reason_picker():
    source = (ROOT / "app/handlers/group.py").read_text(encoding="utf-8")
    assert 'command.action in {"warn", "mute", "kick", "ban"}' in source
    assert "moderation_reason_picker" in source
    assert "mimoru:modpending:" in source


def test_reason_callback_rechecks_actor_permissions_and_consumes_action():
    source = (ROOT / "app/handlers/reason_admin.py").read_text(encoding="utf-8")
    assert 'data["moderator_id"]' in source
    assert "can_moderate" in source
    assert "await redis.delete(key)" in source
    assert "reason.active" in source


def test_reason_picker_callback_format_is_compact():
    source = (ROOT / "app/keyboards/panel.py").read_text(encoding="utf-8")
    assert 'callback_data=f"modreason:{token}:{r.id}"' in source
    assert 'callback_data=f"modcancel:{token}"' in source


def test_main_menu_has_clear_primary_sections():
    source = (ROOT / "app/keyboards/panel.py").read_text(encoding="utf-8")
    for expected in ["Мои группы", "Аналитика", "Реклама", "Подписка", "Панель Mimoru"]:
        assert expected in source


def test_changed_python_files_parse():
    for rel in [
        "app/db/models.py",
        "app/services/moderation_reasons.py",
        "app/keyboards/panel.py",
        "app/handlers/reason_admin.py",
        "app/handlers/group.py",
        "app/handlers/panel.py",
    ]:
        ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
