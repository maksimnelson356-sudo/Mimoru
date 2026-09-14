from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _publish_source() -> str:
    source = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    return source.split("async def _publish_global_request", 1)[1].split(
        "async def distribute_global_posts", 1
    )[0]


def test_global_delivery_preserves_durable_claim_then_rechecks_group_before_send() -> None:
    publish = _publish_source()

    candidates = publish.index("select(Group.id)")
    claim = publish.index("claim = await _claim_delivery")
    group_lock = publish.index("select(Group).where(Group.id == group_id).with_for_update()", claim)
    active_recheck = publish.index("not group.is_active", group_lock)
    disabled_failed = publish.index('claim.status = "failed"', active_recheck)
    disabled_commit = publish.index("await session.commit()", disabled_failed)
    first_send = min(
        index for index in (
            publish.find("await bot.send_photo", disabled_commit),
            publish.find("await bot.send_message", disabled_commit),
        )
        if index != -1
    )
    final_commit = publish.index("await session.commit()", first_send)

    assert candidates < claim < group_lock < active_recheck < disabled_failed < disabled_commit < first_send < final_commit
    assert "Группа отключена до доставки" in publish


def test_global_delivery_completion_uses_fresh_active_group_set() -> None:
    publish = _publish_source()

    send = max(publish.index("await bot.send_photo"), publish.index("await bot.send_message"))
    fresh_groups = publish.index("current_groups = list", send)
    fresh_active = publish.index("select(Group).where(Group.is_active.is_(True))", fresh_groups)
    finalize = publish.index("await _finalize_request_if_complete", fresh_active)

    assert send < fresh_groups < fresh_active < finalize
    assert "item, current_groups" in publish[finalize:]


def test_global_ad_worker_and_service_disable_are_production_reachable() -> None:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    tasks = (ROOT / "app/tasks_ad_market.py").read_text(encoding="utf-8")
    service = (ROOT / "app/handlers/service_management.py").read_text(encoding="utf-8")
    fixes = (ROOT / "app/handlers/service_management_fixes.py").read_text(encoding="utf-8")

    routers = main.split("dp.include_routers(", 1)[1].split(")", 1)[0]
    assert routers.index("service_management_fixes.router") < routers.index("service_management.router")
    assert "service_group_action" not in fixes
    assert 'F.data.regexp(r"^service_group_action:\\d+:(enable|disable)$")' in service
    assert 'group.is_active = action == "enable"' in service

    assert "ad_market_task = asyncio.create_task(" in main
    assert "ad_market_background_loop(bot, stop_event)" in main
    assert "async def ad_market_background_loop" in tasks
    assert "await distribute_global_posts(bot)" in tasks
    assert "await _publish_global_request(bot, request_id)" in tasks
