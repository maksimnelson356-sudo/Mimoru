from pathlib import Path

from app.services.moderation import ModerationOutcome


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _ban_branch() -> str:
    source = _source("app/services/moderation.py")
    return source.split('    if action == "ban":', 1)[1].split('    if action == "unban":', 1)[0]


def _purge_source() -> str:
    return _source("app/services/moderation.py").split(
        "async def purge_user_messages", 1
    )[1].split("MUTED = ChatPermissions", 1)[0]


def test_ban_cleanup_runs_after_ban_and_never_before() -> None:
    branch = _ban_branch()

    ban = branch.index("await bot.ban_chat_member")
    cleanup = branch.index("if cleanup:", ban)
    purge = branch.index("await purge_user_messages(", cleanup)
    outcome = branch.index("return _success(", purge)

    assert ban < cleanup < purge < outcome


def test_ban_cleanup_is_batched_and_keeps_telegram_window_margin() -> None:
    purge = _purge_source()
    moderation = _source("app/services/moderation.py")

    # Окно и размер батча обязаны быть константами, иначе правка одной цифры
    # в фоновой уборке user_messages молча ломает очистку.
    assert "CLEANUP_WINDOW_HOURS = 47" in moderation
    assert "CLEANUP_BATCH_SIZE = 100" in moderation
    assert "timedelta(hours=CLEANUP_WINDOW_HOURS)" in purge
    assert "for i in range(0, total, CLEANUP_BATCH_SIZE):" in purge
    assert "UserMessage.created_at > cutoff" in purge
    # Фоновая уборка должна быть не агрессивнее окна очистки.
    assert "timedelta(hours=49)" in _source("app/services/background_leader.py")


def test_deleted_message_rows_leave_the_table_but_failed_ones_stay() -> None:
    purge = _purge_source()

    try_except = purge.index("except (TelegramBadRequest, TelegramForbiddenError)")
    continue_stmt = purge.index("continue", try_except)
    delete_rows = purge.index("sql_delete(UserMessage)", continue_stmt)

    # Провал батча идёт через continue: его строки остаются для повторного бана.
    assert "failed_batches += 1" in purge[:delete_rows]
    assert continue_stmt < delete_rows
    assert "UserMessage.message_id.in_(batch)" in purge
    assert "deleted += len(batch)" in purge[continue_stmt:delete_rows]


def test_failed_cleanup_batch_is_logged_and_marked_incomplete() -> None:
    branch = _ban_branch()

    incomplete = branch.index("cleanup_incomplete = bool(")
    warning = branch.index('"ban_cleanup_incomplete"', incomplete)

    assert incomplete < warning
    assert "Очистка сообщений выполнена не полностью" in branch


def test_incomplete_cleanup_keeps_ban_successful_and_surfaces_it() -> None:
    source = _source("app/services/moderation.py")
    outcome_block = source.split("class ModerationOutcome(str):", 1)[1].split(
        "def log_action", 1
    )[0]

    for field in ("cleanup_requested", "cleanup_deleted", "cleanup_incomplete"):
        assert f"{field}: bool" in outcome_block or f"{field}: int" in outcome_block

    base = ModerationOutcome("ok", success=True, commit=True, public_notice=True)
    assert base.cleanup_incomplete is False
    assert base.cleanup_requested is False
    assert base.cleanup_deleted == 0

    partial = ModerationOutcome(
        "ok",
        success=True,
        commit=True,
        public_notice=True,
        cleanup_requested=True,
        cleanup_deleted=3,
        cleanup_incomplete=True,
        cleanup_notice="⚠️ Очистка сообщений выполнена не полностью: удалено 3 из 5.",
    )
    assert partial.success is True
    assert partial.commit is True
    assert partial.cleanup_incomplete is True
    # Текст предупреждения доступен отдельно, чтобы вызывающий код не разбирал
    # уведомление по строкам.
    assert partial.cleanup_notice.startswith("⚠️")


def test_durable_ban_keeps_both_cleanup_sources() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def _durable_ban_execute", 1)[1]

    assert "cleanup = cleanup or bool(data.get(\"cleanup\"))" in body
    assert "cleanup=cleanup," in body
    # Параметр обработчика не должен затираться payload-значением.
    assert "cleanup = bool(data.get(\"cleanup\"))\n" not in body


def test_clean_button_forwards_cleanup_into_execute() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def durable_ban_clean", 1)[1].split(
        "async def durable_ban_cancel", 1
    )[0]

    assert "_durable_ban_execute(callback, bot, session, redis, cleanup=True)" in body
    assert 'callback_data=f"modreason:clean:{token}:{raw_reason_id}"' in guard


def test_reason_selection_forwards_pending_cleanup_flag() -> None:
    reasons = _source("app/handlers/reason_admin.py")
    body = reasons.split("async def moderation_reason_selected", 1)[1].split(
        "async def moderation_reason_cancel", 1
    )[0]

    assert 'cleanup=bool(data.get("cleanup"))' in body
    assert "cleanup=False," not in body


def test_pending_payloads_declare_cleanup_explicitly() -> None:
    member_center = _source("app/handlers/member_center.py")
    panel_payload = member_center.split('payload = {', 1)[1].split("}", 1)[0]
    assert '"cleanup": False' in panel_payload

    group = _source("app/handlers/group.py")
    group_payload = group.split("payload = {", 1)[1].split("}", 1)[0]
    assert '"cleanup": False' in group_payload

    command_modes = _source("app/handlers/moderation_command_modes.py")
    buttons_payload = command_modes.split("async def _open_buttons", 1)[1].split(
        "await redis.setex", 1
    )[0]
    assert '"cleanup": cleanup' in buttons_payload
    # Отдельной команды «банчистка» больше нет: очистку выбирают кнопкой.
    assert "банчистка" not in command_modes


def test_ban_always_reaches_the_three_button_dialog() -> None:
    """Бан и «Бан + очистка» необратимы, поэтому выбор обязателен везде."""
    guard = _source("app/handlers/moderation_durable_guard.py")
    reason_action = guard.split("async def durable_reason_action", 1)[1].split(
        "@router.message(", 1
    )[0]

    assert "if action == \"ban\":\n" in reason_action
    # Диалог не должен зависеть от origin: иначе в группе модератор молча
    # получает обычный бан вместо явного выбора.
    assert 'origin != "group"' not in reason_action
    for label in (
        'text="🔴 Бан"',
        'text="🔴🗑 Бан + очистка"',
        'text="❌ Отмена"',
    ):
        assert label in reason_action


def test_group_ban_command_cannot_skip_the_dialog() -> None:
    command_modes = _source("app/handlers/moderation_command_modes.py")

    direct = command_modes.split("use_direct = mode in", 1)[1]
    # Текстовый режим не должен отправлять бан напрямую в execute().
    assert 'if action == "ban":\n        use_direct = False' in direct
    # Ни один путь execute() из команд не выставляет очистку по команде.
    assert "cleanup=cleanup_requested" not in command_modes
    assert "cleanup_requested" not in command_modes


def test_recovery_replays_cleanup_and_keeps_intent_payload() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    operations = _source("app/services/moderation_operations.py")

    payload = guard.split("payload: dict = {", 1)[1].split("}", 1)[0]
    assert '"cleanup": bool(cleanup)' in payload

    ban_branch = operations.split('if action == "ban":', 1)[1].split(
        'elif action == "mute":', 1
    )[0]
    condition = ban_branch.index('if payload.get("cleanup")')
    purge = ban_branch.index("await purge_user_messages(", condition)
    deleted = ban_branch.index('"moderation_intent_cleanup_deleted"', purge)
    incomplete = ban_branch.index('"moderation_intent_cleanup_incomplete"', deleted)

    assert condition < purge < deleted < incomplete


def test_intent_is_created_before_the_pending_token_is_consumed() -> None:
    """Отказ по занятости не должен сжигать одноразовый токен."""
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def _durable_ban_execute", 1)[1]

    consume = body.index('await redis.delete(f"mimoru:modpending:{token}")')
    intent = body.index("intent_id = await _create_guard_intent(", consume)
    refused = body.index("if intent_id is None:", intent)
    restore = body.index("PENDING_BAN_TTL_SECONDS, raw", refused)

    assert consume < intent < refused < restore
    assert "PENDING_BAN_TTL_SECONDS = 600" in guard


def test_complaint_ban_surfaces_partial_cleanup() -> None:
    complaints = _source("app/handlers/complaint_actions.py")
    ban_body = complaints.split("async def complaint_ban_confirm(", 1)[1].split(
        "@router.callback_query(F.data.regexp(r\"^complaint:ban:clean", 1
    )[0]

    detect = ban_body.index('getattr(result, "cleanup_incomplete", False)')
    notice = ban_body.index("result.cleanup_notice", detect)
    answer = ban_body.index("очистка выполнена не полностью", notice)

    assert detect < notice < answer
    # Жалобная очистка должна использовать ту же реализацию, что и бан,
    # иначе расхождение вернётся в учёте удалённых сообщений.
    assert "purge_user_messages" in complaints


def test_durable_ban_answers_partial_cleanup_to_moderator() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def _durable_ban_execute", 1)[1]

    detect = body.index('getattr(result, "cleanup_incomplete", False)')
    answer = body.index("Забанен, очистка сообщений выполнена не полностью.", detect)
    alert = body.index("show_alert=not result.success or cleanup_incomplete", answer)

    assert detect < answer < alert


def test_durable_ban_cancel_is_limited_to_the_command_author() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def durable_ban_cancel", 1)[1].split(
        "async def _durable_ban_execute", 1
    )[0]

    assert "raw = await redis.get" in body
    assert 'data.get("moderator_id", 0)' in body
    assert "Отменить может только автор команды." in body
    assert body.index("moderator_id") < body.index("await redis.delete")


def test_durable_ban_revalidates_reason_action_at_confirmation() -> None:
    guard = _source("app/handlers/moderation_durable_guard.py")
    body = guard.split("async def _durable_ban_execute", 1)[1]

    reason_check = body.index("reason = await reason_admin.get_reason")
    action_check = body.index('"ban" not in normalize_actions(reason.actions)')
    assert reason_check < action_check
