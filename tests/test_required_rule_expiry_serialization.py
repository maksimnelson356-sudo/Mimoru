from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_expiry_reacquires_group_then_rule_and_rechecks_deadline() -> None:
    source = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    body = source.split("async def expire_direct_required_rules() -> None:", 1)[1].split(
        "async def restore_ranked_admins_after_mute", 1
    )[0]

    candidate_scan = body.index("select(DirectRequiredRule.id, DirectRequiredRule.group_id)")
    group_lock = body.index("select(Group)", candidate_scan)
    group_for_update = body.index(".with_for_update()", group_lock)
    rule_lock = body.index("select(DirectRequiredRule)", group_for_update)
    rule_for_update = body.index(".with_for_update()", rule_lock)
    recheck = body.index("rule.expires_at <= now", rule_for_update)
    mutation = body.index("rule.active = False", recheck)
    channel_mutation = body.index("channel.active = False", mutation)
    commit = body.index("await session.commit()", channel_mutation)

    assert candidate_scan < group_lock < group_for_update < rule_lock < rule_for_update
    assert rule_for_update < recheck < mutation < channel_mutation < commit


def test_expiry_scan_does_not_materialize_stale_rule_objects() -> None:
    source = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    body = source.split("async def expire_direct_required_rules() -> None:", 1)[1].split(
        "async def restore_ranked_admins_after_mute", 1
    )[0]
    scan = body.split("for rule_id, group_id in candidates:", 1)[0]

    assert "select(DirectRequiredRule.id, DirectRequiredRule.group_id)" in scan
    assert "session.scalars(" not in scan
    assert "select(DirectRequiredRule).where(" not in scan


def test_owner_renewal_uses_group_lock_before_rule_update() -> None:
    source = (ROOT / "app/handlers/required_direct.py").read_text(encoding="utf-8")
    body = source.split("async def direct_required_connect(", 1)[1].split("@router.message", 1)[0]

    group_lock = body.index("for_update=True")
    rule_lookup = body.index("select(DirectRequiredRule)")
    renewal = body.index("rule.expires_at = expires_at")
    commit = body.index("await session.commit()")
    assert group_lock < rule_lookup < renewal < commit


def test_ad_market_loop_reaches_expiry_worker() -> None:
    source = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    loop = source.split("async def ad_market_background_loop(", 1)[1]
    assert "await expire_direct_required_rules()" in loop

    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "ad_market_task = asyncio.create_task(" in main
    assert "ad_market_background_loop(bot, stop_event)" in main
