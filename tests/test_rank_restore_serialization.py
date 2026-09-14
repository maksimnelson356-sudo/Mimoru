from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _restore_body() -> str:
    source = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    return source.split("async def restore_ranked_admins_after_mute(bot: Bot) -> None:", 1)[1].split(
        "async def ad_market_background_loop", 1
    )[0]


def test_rank_restore_scans_ids_not_stale_assignment_objects() -> None:
    body = _restore_body()
    scan = body.split("for assignment_id in candidate_ids:", 1)[0]
    assert "select(RankAssignment.id).where(" in scan
    assert "select(RankAssignment).where(" not in scan


def test_rank_restore_locks_group_then_assignment_and_revalidates() -> None:
    body = _restore_body()
    loop = body.index("for assignment_id in candidate_ids:")
    group_lock = body.index("select(Group).where(Group.id == group_id).with_for_update()", loop)
    assignment_lock = body.index("select(RankAssignment)", group_lock)
    assignment_for_update = body.index(".with_for_update()", assignment_lock)
    active_recheck = body.index("not assignment.active", assignment_for_update)
    restore_recheck = body.index("not assignment.restore_after_mute", active_recheck)
    rank_recheck = body.index("assignment.rank_code not in ADMIN_RANKS", restore_recheck)
    assert loop < group_lock < assignment_lock < assignment_for_update
    assert assignment_for_update < active_recheck < restore_recheck < rank_recheck


def test_fresh_mute_check_precedes_telegram_restore_under_group_lock() -> None:
    body = _restore_body()
    group_lock = body.index("select(Group).where(Group.id == group_id).with_for_update()")
    mute_check = body.index("still_muted = await session.scalar", group_lock)
    same_group = body.index("Punishment.group_id == assignment.group_id", mute_check)
    same_user = body.index("Punishment.user_telegram_id == assignment.user_telegram_id", same_group)
    mute_kind = body.index('Punishment.kind == "mute"', same_user)
    active = body.index("Punishment.active.is_(True)", mute_kind)
    guard = body.index("if still_muted:", active)
    guard_continue = body.index("continue", guard)
    telegram_restore = body.index("await restore_telegram_rank(bot, group, assignment)", guard_continue)
    clear_flag = body.index("assignment.restore_after_mute = False", telegram_restore)
    commit = body.index("await session.commit()", clear_flag)
    assert group_lock < mute_check < same_group < same_user < mute_kind < active
    assert active < guard < guard_continue < telegram_restore < clear_flag < commit


def test_failed_rank_restore_keeps_recovery_flag_for_retry() -> None:
    body = _restore_body()
    restore_call = body.index("if await restore_telegram_rank(bot, group, assignment):")
    success_flag = body.index("assignment.restore_after_mute = False", restore_call)
    success_commit = body.index("await session.commit()", success_flag)
    failure = body.index("else:", success_commit)
    assert "assignment.restore_after_mute = False" not in body[failure:]
    assert "await session.commit()" not in body[failure:]


def test_production_background_loop_reaches_rank_restore() -> None:
    tasks = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    loop = tasks.split("async def ad_market_background_loop(", 1)[1]
    assert "await restore_ranked_admins_after_mute(bot)" in loop

    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "ad_market_task = asyncio.create_task(" in main
    assert "ad_market_background_loop(bot, stop_event)" in main


def test_live_moderation_uses_same_group_serialization_boundary() -> None:
    moderation = (ROOT / "app/services/moderation.py").read_text(encoding="utf-8")
    execute = moderation.split("async def execute(", 1)[1]
    assert "select(Group).where(Group.id == group_id).with_for_update()" in execute
