from pathlib import Path

from app.services.operations_rules import SAFE_SETTING_FIELDS, diagnostics_score


ROOT = Path(__file__).resolve().parents[1]


def test_diagnostics_score_full():
    assert diagnostics_score({"reachable":True,"is_admin":True,"delete":True,"restrict":True,"invite":True,"manage_chat":True}) == 100


def test_diagnostics_score_partial():
    assert diagnostics_score({"reachable":True,"is_admin":True,"delete":False,"restrict":False,"invite":False,"manage_chat":False}) == 40


def test_snapshot_safe_fields_excludes_runtime_state():
    assert "lockdown_enabled" not in SAFE_SETTING_FIELDS
    assert "last_report_date" not in SAFE_SETTING_FIELDS
    assert "audit_chat_id" not in SAFE_SETTING_FIELDS


def test_snapshot_restore_locks_and_revalidates_before_mutation() -> None:
    source = (ROOT / "app/services/operations_center.py").read_text(encoding="utf-8")
    hardened = source.split("async def apply_snapshot_authorized", 1)[1]

    snapshot_read = hardened.index("session.get(GroupConfigSnapshot, snapshot_id)")
    group_lock = hardened.index(".with_for_update()")
    active_check = hardened.index("not source.is_active or not target.is_active")
    service_owner = hardened.index("is_service_owner(actor_id)")
    source_owner = hardened.index("source.owner_telegram_id != actor_id")
    target_owner = hardened.index("target.owner_telegram_id != actor_id")
    apply = hardened.index("await apply_snapshot(session, snapshot, target, actor_id)")
    commit = hardened.index("await session.commit()")

    assert snapshot_read < group_lock < active_check < service_owner
    assert service_owner < source_owner < target_owner < apply < commit
    assert ".order_by(Group.id)" in hardened


def test_snapshot_apply_callback_uses_atomic_authorization_service() -> None:
    source = (ROOT / "app/handlers/operations_center.py").read_text(encoding="utf-8")
    body = source.split("async def snapshot_apply", 1)[1].split(
        "@router.my_chat_member", 1
    )[0]

    assert "await apply_snapshot_authorized(" in body
    assert "snapshot_id=int(sid)" in body
    assert "target_group_id=int(gid)" in body
    assert "actor_id=callback.from_user.id" in body
    assert "authorized_snapshot(" not in body
    assert "owned_group(" not in body
    assert "await session.commit()" not in body
    assert "if target is None:" in body

