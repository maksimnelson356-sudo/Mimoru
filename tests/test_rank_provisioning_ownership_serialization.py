from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _service() -> str:
    return (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")


def test_live_rank_intent_locks_group_then_intent_before_authorization() -> None:
    source = _service()
    start = source.index("async def _execute_live_intent")
    end = source.index("async def provision_assignment", start)
    body = source[start:end]

    group_lock = body.index("select(Group).where(Group.id == group_id).with_for_update()")
    intent_lock = body.index("select(RankProvisioningIntent)", group_lock)
    remove_auth = body.index("await can_remove_assignment(")
    assign_auth = body.index("await can_assign_rank(")
    first_mutation = min(
        body.index("await promote_admin("),
        body.index("await demote_telegram_admin("),
        body.index("assignment = await _finalize_intent("),
    )

    assert group_lock < intent_lock < remove_auth < first_mutation
    assert group_lock < intent_lock < assign_auth < first_mutation


def test_stale_rank_actor_drops_intent_before_side_effect() -> None:
    source = _service()
    start = source.index("if not allowed:", source.index("async def _execute_live_intent"))
    end = source.index('if intent.telegram_action == "promote":', start)
    body = source[start:end]

    assert "await _drop_intent(session, intent)" in body
    assert "promote_chat_member" not in body
    assert "demote_telegram_admin" not in body
    assert "_finalize_intent" not in body


def test_durable_intent_commit_precedes_live_execution() -> None:
    source = _service()
    create_start = source.index("async def _create_intent")
    create_end = source.index("async def _finalize_intent", create_start)
    create_body = source[create_start:create_end]
    assert "await session.commit()" in create_body

    provision_start = source.index("async def provision_assignment")
    provision_end = source.index("async def remove_assignment", provision_start)
    provision = source[provision_start:provision_end]
    assert provision.index("intent = await _create_intent(") < provision.index(
        "return await _execute_live_intent(bot, session, intent_id=intent.id)"
    )

    remove_start = source.index("async def remove_assignment")
    remove_end = source.index("def _telegram_rights_match", remove_start)
    remove = source[remove_start:remove_end]
    assert remove.index("intent = await _create_intent(") < remove.index(
        "await _execute_live_intent(bot, session, intent_id=intent.id)"
    )


def test_bot_only_live_intents_still_reauthorize_before_finalize() -> None:
    source = _service()
    start = source.index("async def _execute_live_intent")
    end = source.index("async def provision_assignment", start)
    body = source[start:end]

    assert 'elif intent.telegram_action != "none":' in body
    assert "await can_assign_rank(" in body
    assert "await can_remove_assignment(" in body
    assert "assignment = await _finalize_intent(session, intent)" in body


def test_rank_recovery_remains_non_replaying_for_telegram_mutations() -> None:
    source = _service()
    recovery = source[source.index("async def recover_rank_provisioning_intents"):]

    assert "await bot.get_chat_member" in recovery
    assert "promote_chat_member" not in recovery
    assert "demote_telegram_admin" not in recovery


def test_live_rank_handlers_use_hardened_service() -> None:
    handlers = (ROOT / "app/handlers/rank_provisioning_handlers.py").read_text(encoding="utf-8")
    assert "from app.services.rank_provisioning import" in handlers
    assert "provision_assignment" in handlers
    assert "remove_assignment" in handlers
    assert handlers.count("await provision_assignment(") == 2
    assert handlers.count("await remove_assignment(") == 1
