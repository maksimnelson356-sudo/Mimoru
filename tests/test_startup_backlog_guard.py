from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_startup_backlog_recovers_only_fresh_critical_group_commands() -> None:
    source = (ROOT / "app/services/startup_backlog.py").read_text(encoding="utf-8")

    assert "CRITICAL_MODERATION_TTL_SECONDS = 600" in source
    assert '_CRITICAL_COMMANDS = {"бан", "мут", "пред"}' in source
    assert '_GROUP_TYPES = {"group", "supergroup"}' in source
    assert "update.message" in source
    assert "update.callback_query" not in source.split("def _critical_group_message", 1)[1].split(
        "async def _queue_recovery_notice", 1
    )[0]
    assert "0 <= age <= CRITICAL_MODERATION_TTL_SECONDS" in source


def test_startup_backlog_discards_stale_updates_and_deduplicates_notices() -> None:
    source = (ROOT / "app/services/startup_backlog.py").read_text(encoding="utf-8")

    assert "await redis.sadd(RECOVERY_NOTICE_USERS_KEY, user_id)" in source
    assert "await redis.spop(RECOVERY_NOTICE_USERS_KEY)" in source
    assert "await asyncio.wait_for(stop_event.wait(), timeout=0.2)" in source
    assert '"Некоторые действия, отправленные во время недоступности бота, не были выполнены.' in source


def test_critical_backlog_updates_are_claimed_before_dispatch() -> None:
    source = (ROOT / "app/services/startup_backlog.py").read_text(encoding="utf-8")

    claim = source.index("if not await _claim_critical(redis, update.update_id):")
    dispatch = source.index("await dispatcher.feed_update(bot, update)")
    assert claim < dispatch
    assert "nx=True" in source
    assert "ex=86400" in source


def test_startup_drain_runs_before_polling_and_notice_sender_is_background_task() -> None:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")

    drain = main.index("await drain_startup_backlog(")
    polling = main.index("await dp.start_polling(")
    assert drain < polling
    assert "send_recovery_notices(bot, redis, stop_event)" in main
    assert 'name="recovery-notices"' in main
    assert "await stop_task(recovery_notice_task, timeout=10.0)" in main


def test_bot_container_restarts_after_process_failure() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    bot_section = compose.split("  bot:\n", 1)[1].split("\n  postgres:\n", 1)[0]

    assert "restart: on-failure:5" in bot_section
