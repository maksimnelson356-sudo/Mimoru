from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function(source: str, name: str, next_name: str | None = None) -> str:
    body = source.split(f"async def {name}", 1)[1]
    if next_name:
        body = body.split(f"async def {next_name}", 1)[0]
    return body


def test_rank_provisioning_intent_commits_before_telegram_side_effect() -> None:
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    provision = _function(source, "provision_assignment", "remove_assignment")
    execute = _function(source, "_execute_live_intent", "provision_assignment")
    create = _function(source, "_create_intent", "_finalize_intent")

    assert "await session.commit()" in create
    assert provision.index("intent = await _create_intent") < provision.index(
        "return await _execute_live_intent(bot, session, intent_id=intent.id)"
    )
    assert "await promote_admin(" in execute
    assert "await demote_telegram_admin" in execute


def test_rank_removal_claims_before_demoting_telegram_admin() -> None:
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    remove = _function(source, "remove_assignment", "_telegram_rights_match")
    execute = _function(source, "_execute_live_intent", "provision_assignment")

    assert remove.index("intent = await _create_intent") < remove.index(
        "await _execute_live_intent(bot, session, intent_id=intent.id)"
    )
    assert execute.index("await can_remove_assignment(") < execute.index(
        "await demote_telegram_admin"
    )
    assert "assignment = await _finalize_intent(session, intent)" in execute


def test_rank_removal_decides_demotion_from_access_mode_not_just_the_flag() -> None:
    """Регрессия к исходному багу.

    Назначение через «Telegram + Mimoru» поверх уже существующего
    Telegram-администратора идёт через привязку, где
    telegram_admin_managed остаётся false. Снятие ранга опиралось только на
    этот флаг, поэтому telegram_action становился "none", Mimoru писала
    «Ранг снят», а человек оставался администратором группы.
    """
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    remove = _function(source, "remove_assignment", "_telegram_rights_match")

    decision = remove.index("was_telegram_admin = (")
    flag = remove.index("assignment.telegram_admin_managed", decision)
    mode = remove.index("access_mode", decision)
    action = remove.index('telegram_action = "demote" if was_telegram_admin else "none"', decision)

    assert decision < flag < action
    assert decision < mode < action
    assert "TELEGRAM_MODE" in remove[decision:action]


def test_rank_removal_still_leaves_bot_only_assignments_alone() -> None:
    """Ранг «Только внутри Mimoru» не должен трогать права Telegram."""
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    remove = _function(source, "remove_assignment", "_telegram_rights_match")

    decision = remove.index("was_telegram_admin = (")
    action = remove.index("telegram_action =", decision)
    expression = remove[decision:action]
    assert "telegram_admin_managed" in expression
    assert "TELEGRAM_MODE" in expression
    assert 'else "none"' in remove[action:action + 120]


def test_failed_demotion_never_reports_success() -> None:
    """Telegram отказал в снятии прав — ранг обязан остаться в базе."""
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    execute = _function(source, "_execute_live_intent", "provision_assignment")

    branch = execute.index('elif intent.telegram_action == "demote":')
    demote = execute.index("if not await demote_telegram_admin", branch)
    drop = execute.index("await _drop_intent(session, intent)", demote)
    return_error = execute.index("return False,", demote)
    finalize = execute.index("await _finalize_intent(session, intent)", drop)

    assert branch < demote < drop < return_error < finalize
    assert "Не удалось снять Telegram-права администратора" in execute[demote:finalize]


def test_recovery_never_grants_or_revokes_telegram_privileges() -> None:
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    body = source.split("async def recover_rank_provisioning_intents", 1)[1]
    assert "promote_admin" not in body
    assert "promote_chat_member" not in body
    assert "demote_telegram_admin" not in body
    assert "get_chat_member" in body
    assert "_telegram_rights_match" in body


def test_bot_only_recovery_revalidates_actor_before_finalize() -> None:
    source = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    helper = _function(
        source,
        "_actor_can_recover_bot_only_intent",
        "recover_rank_provisioning_intents",
    )
    recovery = source.split("async def recover_rank_provisioning_intents", 1)[1]
    no_telegram = recovery.split('if intent.telegram_action == "none":', 1)[1].split(
        "try:", 1
    )[0]

    assert "await can_assign_rank(" in helper
    assert "await can_remove_assignment(" in helper
    assert "intent.actor_telegram_id" in helper
    assert "intent.desired_rank_code" in helper
    assert "intent.user_telegram_id" in helper
    assert "await _actor_can_recover_bot_only_intent(" in no_telegram
    assert no_telegram.index("await _actor_can_recover_bot_only_intent(") < no_telegram.index(
        "await _finalize_intent(session, intent)"
    )
    assert "await _drop_intent(session, intent)" in no_telegram


def test_safe_rank_router_precedes_legacy_mutation_routers() -> None:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "rank_provisioning_handlers.router" in main
    assert main.index("rank_provisioning_handlers.router") < main.index("rank_text_commands.router")
    assert main.index("rank_provisioning_handlers.router") < main.index("admin_access_mode.router", main.index("dp.include_routers"))
    assert main.index("rank_provisioning_handlers.router") < main.index("telegram_roles.router", main.index("dp.include_routers"))


def test_safe_rank_change_persists_explicit_access_mode() -> None:
    source = (ROOT / "app/handlers/rank_provisioning_handlers.py").read_text(encoding="utf-8")
    assert "mode = TELEGRAM_MODE if rank_code in ADMIN_RANKS else BOT_ONLY_MODE" in source
    service = (ROOT / "app/services/rank_provisioning.py").read_text(encoding="utf-8")
    assert "assignment.access_mode = access_mode" in service
    assert "access_mode=access_mode" in service


def test_rank_provisioning_schema_is_registered() -> None:
    env = (ROOT / "alembic/env.py").read_text(encoding="utf-8")
    schema = (ROOT / "scripts/check_schema_consistency.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic/versions/0040_rank_provisioning_intents.py").read_text(encoding="utf-8")
    assert "rank_provisioning_models" in env
    assert "rank_provisioning_models" in schema
    assert 'revision = "0040_rank_provisioning_intents"' in migration
    assert 'down_revision = "0039_broadcast_delivery_claims"' in migration
