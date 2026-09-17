from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_browse_and_confirm_reauthorize_source_group() -> None:
    code = (ROOT / "app/handlers/operations_center.py").read_text(encoding="utf-8")

    assert "async def authorized_snapshot" in code
    assert "for_update" in code

    # snapshot_open must call authorized_snapshot without requesting a lock
    assert "snapshot_open" in code
    assert "authorized_snapshot(session, sid, callback.from_user.id" in code

    # snapshot_confirm must call authorized_snapshot with for_update=True
    assert "snapshot_confirm" in code
    assert "for_update=True" in code

    assert code.count("await authorized_snapshot(") >= 2


def test_final_apply_uses_locked_source_and_target_reauthorization() -> None:
    handler = (ROOT / "app/handlers/operations_center.py").read_text(encoding="utf-8")
    service = (ROOT / "app/services/operations_center.py").read_text(encoding="utf-8")

    start = handler.index("async def snapshot_apply")
    end = handler.index("@router.my_chat_member()", start)
    body = handler[start:end]
    assert "await apply_snapshot_authorized(" in body
    assert "snapshot_id=int(sid)" in body
    assert "target_group_id=int(gid)" in body
    assert "actor_id=callback.from_user.id" in body
    assert "await authorized_snapshot(" not in body
    assert "await owned_group(" not in body

    hardened = service.split("async def apply_snapshot_authorized", 1)[1]
    assert ".with_for_update()" in hardened
    assert "source.owner_telegram_id != actor_id" in hardened
    assert "target.owner_telegram_id != actor_id" in hardened
    assert hardened.index(".with_for_update()") < hardened.index(
        "await apply_snapshot(session, snapshot, target, actor_id)"
    )
