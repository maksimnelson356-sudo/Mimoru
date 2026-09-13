import asyncio
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods.base import TelegramMethod
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.keyboards import panel as panel_keyboards
from app.keyboards.home import (
    automation_menu,
    channels_admin_menu,
    content_menu,
    group_health_menu,
    home_menu,
    members_menu,
    moderation_menu,
    operations_menu,
    protection_menu,
    service_menu,
    settings_detail_menu,
    settings_menu,
)

panel_keyboards.main_menu = home_menu
panel_keyboards.service_menu = service_menu
panel_keyboards.protection_menu = protection_menu
panel_keyboards.moderation_menu = moderation_menu
panel_keyboards.members_menu = members_menu
panel_keyboards.content_menu = content_menu
panel_keyboards.settings_menu = settings_menu
panel_keyboards.settings_detail_menu = settings_detail_menu
panel_keyboards.channels_admin_menu = channels_admin_menu
panel_keyboards.automation_menu = automation_menu
panel_keyboards.operations_menu = operations_menu
panel_keyboards.group_health_menu = group_health_menu

from app.handlers import (
    ad_invoice_safety,
    ad_legacy_payment_guard,
    ad_market_atomic,
    ad_market_v3,
    ad_navigation,
    admin_access_mode,
    advanced,
    audit,
    automation,
    billing,
    campaign_spam,
    client_management,
    common,
    complaint_actions,
    contextual_back,
    control_center,
    dashboard,
    deferred_bans,
    deleted_accounts,
    edit_protection,
    features,
    fun_bot_guard,
    fun_commands,
    fun_extras,
    fun_help,
    fun_preferences,
    fun_social,
    fun_stats,
    group,
    group_action_aliases,
    group_commands,
    group_directory,
    group_lookup,
    group_onboarding_flow,
    group_shortcuts,
    group_stats_v2,
    hardening,
    home_panel,
    input_safety,
    invite_operation_guard,
    join_requests,
    join_review_guard,
    kick_retirement,
    member_center,
    member_navigation,
    member_profile_v2,
    members,
    mentions,
    mimoru_identity,
    moderation_command_modes,
    moderation_durable_guard,
    navigation,
    navigation_fixes,
    onboarding,
    operations,
    operations_center,
    panel,
    permission_modes,
    plan_catalog,
    plan_directory,
    plan_legacy_redirect,
    promo_redemption,
    protection,
    quarantine,
    rank_legacy_guard,
    rank_policy_fix,
    rank_provisioning_handlers,
    rank_text_commands,
    reason_admin,
    required_direct,
    safety,
    sender_chats,
    service_admin,
    service_broadcast,
    service_group_access,
    service_management,
    service_management_fixes,
    service_owner_directory,
    setup_legacy_redirect,
    slow_mode,
    telegram_roles,
    wizard_navigation,
)
from app.health import HealthServer
from app.middlewares import DatabaseMiddleware
from app.middlewares_dedup import MessageDeduplicationMiddleware
from app.middlewares_group_mutation import GroupMutationLockMiddleware
from app.middlewares_performance import SlowUpdateLoggingMiddleware
from app.middlewares_rank_access import RankAccessModeMiddleware
from app.middlewares_rank_safety import (
    RankMutationLockMiddleware,
    SensitiveGroupAliasAccessMiddleware,
)
from app.reply_safety import CancelledReplyMiddleware
from app.services.activity_tracking import track_outgoing_group_result
from app.services.ad_market_schema import ensure_ad_market_schema
from app.services.background_leader import leader_background_loop
from app.services.chat_permission_transitions import recover_chat_permission_transitions
from app.services.join_request_transitions import (
    recover_invite_operations,
    recover_join_request_reviews,
)
from app.services.moderation_operation_schema import ensure_moderation_operation_schema
from app.services.moderation_operations import recover_moderation_operation_intents
from app.services.public_identity import replace_public_group_id_labels
from app.services.rank_provisioning import recover_rank_provisioning_intents
from app.services.runtime import stop_task
from app.services.runtime_incident import (
    RuntimeTracker,
    RuntimeUpdateCounterMiddleware,
    notify_runtime_incident,
)
from app.services.startup_backlog import drain_startup_backlog, send_recovery_notices
from app.services.ui import clean_ui_text
from app.tasks_ad_market import ad_market_background_loop

_PLAIN_TEXT_FIELDS = (
    "text",
    "caption",
    "title",
    "description",
    "explanation",
    "question",
)
_RETIRED_KICK_CALLBACK_PREFIXES = ("reason_action:", "member_punish:", "role_perm:")
_IDEMPOTENT_EDIT_METHODS = {
    "EditMessageText",
    "EditMessageCaption",
    "EditMessageMedia",
    "EditMessageReplyMarkup",
}


def _is_retired_kick_button(button: Any) -> bool:
    callback_data = getattr(button, "callback_data", None)
    return bool(
        isinstance(callback_data, str)
        and callback_data.endswith(":kick")
        and callback_data.startswith(_RETIRED_KICK_CALLBACK_PREFIXES)
    )


def _plain_reply_markup(markup: Any) -> Any:
    if markup is None or not hasattr(markup, "model_copy"):
        return markup
    if hasattr(markup, "inline_keyboard"):
        rows = []
        for row in markup.inline_keyboard:
            cleaned_row = [
                (
                    button.model_copy(update={"text": clean_ui_text(button.text)})
                    if isinstance(getattr(button, "text", None), str)
                    else button
                )
                for button in row
                if not _is_retired_kick_button(button)
            ]
            if cleaned_row:
                rows.append(cleaned_row)
        return markup.model_copy(update={"inline_keyboard": rows})
    if hasattr(markup, "keyboard"):
        rows = [
            [
                (
                    button.model_copy(update={"text": clean_ui_text(button.text)})
                    if isinstance(getattr(button, "text", None), str)
                    else button
                )
                for button in row
            ]
            for row in markup.keyboard
        ]
        return markup.model_copy(update={"keyboard": rows})
    return markup


def _plain_method(method: TelegramMethod[Any]) -> TelegramMethod[Any]:
    updates: dict[str, Any] = {}
    for field in _PLAIN_TEXT_FIELDS:
        value = getattr(method, field, None)
        if isinstance(value, str):
            updates[field] = clean_ui_text(value)
    reply_markup = getattr(method, "reply_markup", None)
    if reply_markup is not None:
        updates["reply_markup"] = _plain_reply_markup(reply_markup)
    if hasattr(method, "parse_mode"):
        updates["parse_mode"] = None
    if hasattr(method, "caption_parse_mode"):
        updates["caption_parse_mode"] = None
    return method.model_copy(update=updates) if updates else method


def _is_idempotent_edit_error(
    method: TelegramMethod[Any], exc: TelegramBadRequest
) -> bool:
    return (
        type(method).__name__ in _IDEMPOTENT_EDIT_METHODS
        and "message is not modified" in str(exc).casefold()
    )


class PlainTextBot(Bot):
    async def __call__(
        self, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        plain_method = _plain_method(method)
        plain_method = await replace_public_group_id_labels(self, plain_method)
        try:
            result = await super().__call__(
                plain_method, request_timeout=request_timeout
            )
        except TelegramBadRequest as exc:
            if _is_idempotent_edit_error(plain_method, exc):
                return True
            raise
        await track_outgoing_group_result(type(plain_method).__name__, result)
        return result

    async def send_message(self, *args, **kwargs):
        text = kwargs.get("text")
        if text is None and len(args) >= 2 and isinstance(args[1], str):
            mutable = list(args)
            mutable[1] = clean_ui_text(args[1])
            args = tuple(mutable)
        elif isinstance(text, str):
            kwargs["text"] = clean_ui_text(text)
        kwargs.pop("parse_mode", None)
        return await super().send_message(*args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        text = kwargs.get("text")
        if text is None and args and isinstance(args[0], str):
            mutable = list(args)
            mutable[0] = clean_ui_text(args[0])
            args = tuple(mutable)
        elif isinstance(text, str):
            kwargs["text"] = clean_ui_text(text)
        kwargs.pop("parse_mode", None)
        return await super().edit_message_text(*args, **kwargs)


async def configure_bot(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="help", description="Помощь по боту Mimoru"),
        ]
    )
    await bot.set_my_commands(
        [
            BotCommand(command="games", description="Игры"),
            BotCommand(command="report", description="Пожаловаться"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="comands", description="Список команд"),
            BotCommand(command="oftop", description="Связь с владельцем бота"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.delete_webhook(drop_pending_updates=False)


async def main() -> None:
    configure_logging()
    log = structlog.get_logger()
    settings = get_settings()
    bot = PlainTextBot(settings.bot_token)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    runtime_tracker = RuntimeTracker(redis)
    health = HealthServer(redis, settings.health_host, settings.health_port)
    dp = Dispatcher(redis=redis)
    runtime_counter_middleware = RuntimeUpdateCounterMiddleware(runtime_tracker)
    slow_update_middleware = SlowUpdateLoggingMiddleware()
    dedup_middleware = MessageDeduplicationMiddleware(redis)
    cancelled_reply_middleware = CancelledReplyMiddleware(redis)
    db_middleware = DatabaseMiddleware()
    rank_access_middleware = RankAccessModeMiddleware()
    group_mutation_lock_middleware = GroupMutationLockMiddleware()
    sensitive_alias_access_middleware = SensitiveGroupAliasAccessMiddleware()
    rank_mutation_lock_middleware = RankMutationLockMiddleware()
    # ВАЖНО: dp.*.outer_middleware использует LIFO (см. MiddlewareManager.wrap_middlewares).
    # Последний зарегистрированный = первый в цепочке. Поэтому порядок регистрации
    # обратный желаемому порядку выполнения.
    #
    # Желаемый порядок для Message:
    #   slow_update -> runtime_counter -> dedup -> cancelled_reply -> db -> handler
    #
    # Желаемый порядок для CallbackQuery:
    #   slow_update -> runtime_counter -> dedup -> db -> handler

    # 1. Самые внешние update-уровня (регистрируются последними → вызываются первыми)
    dp.update.outer_middleware(runtime_counter_middleware)
    dp.update.outer_middleware(slow_update_middleware)

    # 2. Message (LIFO: сначала внутренние, потом внешние)
    dp.message.outer_middleware(db_middleware)  # внутренний
    dp.message.outer_middleware(cancelled_reply_middleware)
    dp.message.outer_middleware(dedup_middleware)  # внешний

    # 3. CallbackQuery (LIFO)
    dp.callback_query.outer_middleware(db_middleware)  # внутренний
    dp.callback_query.outer_middleware(dedup_middleware)  # внешний

    # 4. Остальные типы — только db_middleware, без dedup
    dp.edited_message.outer_middleware(db_middleware)
    dp.pre_checkout_query.outer_middleware(db_middleware)
    dp.chat_join_request.outer_middleware(db_middleware)
    dp.chat_member.outer_middleware(db_middleware)
    dp.my_chat_member.outer_middleware(db_middleware)
    for rank_router in (admin_access_mode.router, telegram_roles.router):
        rank_router.callback_query.middleware(rank_access_middleware)
        rank_router.message.middleware(rank_access_middleware)
    group.router.message.middleware(group_mutation_lock_middleware)
    group_action_aliases.router.message.middleware(sensitive_alias_access_middleware)
    telegram_roles.router.callback_query.middleware(rank_mutation_lock_middleware)
    telegram_roles.router.message.middleware(rank_mutation_lock_middleware)
    dp.include_routers(
        fun_preferences.router,
        fun_extras.router,
        group_onboarding_flow.router,
        mimoru_identity.router,
        member_profile_v2.router,
        group_stats_v2.router,
        deferred_bans.router,
        rank_provisioning_handlers.router,
        rank_text_commands.router,
        group_action_aliases.router,
        group_shortcuts.router,
        common.router,
        group_lookup.router,
        member_navigation.router,
        service_owner_directory.router,
        contextual_back.router,
        group_directory.router,
        plan_directory.router,
        ad_invoice_safety.router,
        ad_market_atomic.router,
        ad_market_v3.router,
        ad_navigation.router,
        home_panel.router,
        plan_catalog.router,
        promo_redemption.router,
        plan_legacy_redirect.router,
        navigation.router,
        wizard_navigation.router,
        setup_legacy_redirect.router,
        navigation_fixes.router,
        input_safety.router,
        automation.router,
        operations_center.router,
        ad_legacy_payment_guard.router,
        billing.router,
        kick_retirement.router,
        moderation_command_modes.router,
        moderation_durable_guard.router,
        reason_admin.router,
        group_commands.router,
        complaint_actions.router,
        fun_help.router,
        fun_bot_guard.router,
        fun_stats.router,
        fun_social.router,
        fun_commands.router,
        rank_legacy_guard.router,
        rank_policy_fix.router,
        admin_access_mode.router,
        telegram_roles.router,
        control_center.router,
        onboarding.router,
        deleted_accounts.router,
        member_center.router,
        panel.router,
        service_management_fixes.router,
        service_group_access.router,
        service_management.router,
        service_admin.router,
        dashboard.router,
        required_direct.router,
        group.router,
        audit.router,
        hardening.router,
        safety.router,
        quarantine.router,
        slow_mode.router,
        campaign_spam.router,
        edit_protection.router,
        invite_operation_guard.router,
        join_review_guard.router,
        join_requests.router,
        mentions.router,
        sender_chats.router,
        members.router,
        operations.router,
        permission_modes.router,
        advanced.router,
        features.router,
        service_broadcast.router,
        client_management.router,
        protection.router,
    )
    allowed_updates = dp.resolve_used_update_types()
    stop_event = asyncio.Event()
    task = None
    ad_market_task = None
    recovery_notice_task = None
    heartbeat_task = None
    clean_shutdown = False
    try:
        incident = await runtime_tracker.inspect_previous_run()
        await runtime_tracker.mark_started()
        heartbeat_task = asyncio.create_task(
            runtime_tracker.heartbeat_loop(stop_event),
            name="runtime-heartbeat",
        )
        await ensure_ad_market_schema()
        await ensure_moderation_operation_schema()
        await recover_rank_provisioning_intents(bot)
        await recover_chat_permission_transitions(bot)
        await recover_join_request_reviews(bot)
        await recover_invite_operations()
        await recover_moderation_operation_intents(bot)
        await configure_bot(bot)
        backlog_stats = await drain_startup_backlog(bot, dp, redis, allowed_updates=allowed_updates)
        await notify_runtime_incident(bot, settings.service_owner_ids, incident, backlog_stats)
        await health.start()
        task = asyncio.create_task(leader_background_loop(bot, redis, stop_event), name="background-loop")
        ad_market_task = asyncio.create_task(ad_market_background_loop(bot, stop_event), name="ad-market-background-loop")
        recovery_notice_task = asyncio.create_task(send_recovery_notices(bot, redis, stop_event), name="recovery-notices")
        me = await bot.get_me()
        health.set_ready(True)
        log.info("bot_started", bot_id=me.id, username=me.username)
        await dp.start_polling(bot, allowed_updates=allowed_updates)
        clean_shutdown = True
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        clean_shutdown = True
        raise
    except BaseException as exc:
        try:
            await runtime_tracker.record_fatal(exc)
        except Exception:
            log.exception("runtime_fatal_record_failed")
        raise
    finally:
        stop_event.set()
        await stop_task(recovery_notice_task, timeout=10.0)
        await stop_task(task, timeout=10.0)
        await stop_task(ad_market_task, timeout=10.0)
        await stop_task(heartbeat_task, timeout=10.0)
        if clean_shutdown:
            try:
                await runtime_tracker.mark_clean_shutdown()
            except Exception:
                log.exception("runtime_clean_shutdown_record_failed")
        await health.close()
        await redis.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
