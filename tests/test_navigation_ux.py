from pathlib import Path

from app.keyboards.home import cancel_input_menu, content_menu, group_home_menu, home_menu, reply_cancel_menu
from app.services.people import trust_label
from app.services.ui import clean_ui_text, panel_header


ROOT = Path(__file__).resolve().parents[1]


def _button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_home_menu_is_self_explanatory_and_has_no_global_stats():
    texts = _button_texts(home_menu())
    assert "🏠 Управлять моими группами" in texts
    assert "📊 Моя статистика" not in texts
    assert "💎 Тарифы и подписка" in texts
    assert "💬 Поддержка" in texts
    assert "ℹ️ О боте Mimoru" in texts
    assert "❓ Как пользоваться" in texts


def test_group_menu_has_statistics_ads_direct_diagnostics_and_disconnect():
    markup = group_home_menu(7)
    texts = _button_texts(markup)
    callbacks = _callbacks(markup)
    assert "📊 Статистика группы" in texts
    assert "📢 Реклама группы" in texts
    assert "🩺 Диагностика" in texts
    assert "⛔ Отключить группу" in texts
    assert "health_direct:7" in callbacks
    assert "ads:placement:7" in callbacks
    assert "group_disconnect:7" in callbacks
    assert "◀️ Назад к моим группам" in texts


def test_required_subscription_is_not_in_content_menu_and_is_reachable_from_group_ads():
    texts = _button_texts(content_menu(7))
    callbacks = _callbacks(content_menu(7))
    assert "🚫 Запрещённые слова и фразы" in texts
    assert not any("Обязатель" in text for text in texts)
    assert "channels:7" not in callbacks
    market = (ROOT / "app/handlers/ad_market_v3.py").read_text(encoding="utf-8")
    assert 'callback_data=f"channels:{group_id}"' in market
    assert 'text="◀️ К рекламе группы"' in (ROOT / "app/keyboards/home.py").read_text(encoding="utf-8")


def test_every_fsm_form_gets_contextual_cancel_and_cleanup():
    middleware = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    assert "state_before = await state.get_state()" in middleware
    assert "state_after = await state.get_state()" in middleware
    assert "_cancel_callback(" in middleware
    assert "cancel_input_menu(cancel_callback)" in middleware
    assert 'name.endswith(":support_new")' in middleware
    assert 'return "panel:support"' in middleware
    assert 'name.endswith(":word_add")' in middleware
    assert 'return f"words:{group_id}"' in middleware
    assert 'name.endswith(":channel_add")' in middleware
    assert 'return f"channels:{group_id}"' in middleware
    assert _button_texts(cancel_input_menu("panel:support")) == ["✖️ Отменить ввод"]
    assert _callbacks(cancel_input_menu("panel:support")) == ["panel:support"]


def test_cancel_is_not_exposed_as_bot_command():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    common = (ROOT / "app/handlers/common.py").read_text(encoding="utf-8")
    assert 'BotCommand(command="cancel"' not in main
    assert 'Command("cancel")' not in common


def test_obsolete_advertising_handlers_are_not_registered():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    for legacy in (
        "ad_approvals",
        "ad_input",
        "ad_market_dashboard",
        "ad_post_market",
        "ad_required_market",
        "ad_required_safety",
        "advertising.router",
    ):
        assert legacy not in main
    assert "ad_market_v3.router" in main
    assert "ad_navigation.router" in main
    assert "required_direct.router" in main


def test_legacy_force_reply_guard_remains_for_old_messages():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    safety = (ROOT / "app/handlers/input_safety.py").read_text(encoding="utf-8")
    middleware = (ROOT / "app/reply_safety.py").read_text(encoding="utf-8")
    assert 'F.data.regexp(r"^reply_cancel:\\d+$")' in safety
    assert "delete_message" in safety
    assert "redis.setex" in safety
    assert "CancelledReplyMiddleware(redis)" in main
    assert 'event.chat.type == "private"' in middleware
    callback = reply_cancel_menu(123).inline_keyboard[0][0]
    assert callback.callback_data == "reply_cancel:123"


def test_all_statistics_routes_are_group_scoped():
    home_panel = (ROOT / "app/handlers/home_panel.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "app/handlers/dashboard.py").read_text(encoding="utf-8")
    assert 'F.data == "panel:my_stats"' in home_panel
    assert 'callback_data=f"group_section:{group.id}:analytics"' in home_panel
    assert "DailyStat.group_id == group.id" in dashboard


def test_add_group_requests_required_admin_rights():
    directory = (ROOT / "app/handlers/group_directory.py").read_text(encoding="utf-8")
    assert 'text="➕ Добавить Mimoru администратором"' in directory
    assert "delete_messages" in directory
    assert "restrict_members" in directory
    assert "invite_users" in directory
    assert "manage_chat" in directory
    assert "promote_members" in directory
    assert "manage_video_chats" in directory
    assert '?startgroup&admin={GROUP_ADMIN_RIGHTS}' in directory


def test_existing_telegram_admins_are_synced_and_labeled():
    sync = (ROOT / "app/services/telegram_admins.py").read_text(encoding="utf-8")
    directory = (ROOT / "app/handlers/group_directory.py").read_text(encoding="utf-8")
    assert "get_chat_administrators" in sync
    assert "track_group_member" in sync
    assert "row.trust_status = role_code" in sync
    assert "TELEGRAM_OWNER" in sync
    assert "TELEGRAM_ADMIN" in sync
    assert "sync_telegram_administrators(bot, session, group)" in directory
    assert trust_label("telegram_owner") == "👑 Владелец Telegram-группы"
    assert trust_label("telegram_admin") == "🛡 Администратор Telegram"


def test_telegram_admin_sync_imports_single_managed_internal_rank():
    sync = (ROOT / "app/services/telegram_admins.py").read_text(encoding="utf-8")
    model = (ROOT / "app/db/rank_models.py").read_text(encoding="utf-8")
    assert "GroupModerator" not in sync
    assert "RankAssignment" in sync
    assert "rank_code=CHAT_ADMIN" in sync
    assert "telegram_admin_managed=True" in sync
    assert 'action="import_telegram_admin"' in sync
    assert 'UniqueConstraint("group_id", "user_telegram_id"' in model


def test_group_onboarding_moves_configuration_to_private_chat_and_handles_removal():
    flow = (ROOT / "app/handlers/group_onboarding_flow.py").read_text(encoding="utf-8")
    shortcuts = (ROOT / "app/handlers/group_shortcuts.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "@router.my_chat_member()" in flow
    assert 'F.text.casefold() == "подключить"' in flow
    assert "import_existing_admin_ranks" in flow
    assert 'start=group_{group_id}' in flow
    assert "setup_start_menu(group.id)" in flow
    assert "old_status == ChatMemberStatus.ADMINISTRATOR and new_status == ChatMemberStatus.MEMBER" in flow
    assert "new_status in INACTIVE_BOT_STATUSES" in flow
    assert "group.is_active = False" in flow
    assert 'F.data.regexp(r"^group_disconnect:\\d+$")' in shortcuts
    assert "await bot.leave_chat(group.telegram_chat_id)" in shortcuts
    router_block = main.split("dp.include_routers(", 1)[1]
    assert router_block.index("group_onboarding_flow.router") < router_block.index("common.router")
    assert router_block.index("group_onboarding_flow.router") < router_block.index("group.router")


def test_plan_flow_has_group_picker_detail_invoice_and_back_paths():
    catalog = (ROOT / "app/handlers/plan_catalog.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'F.data == "panel:plans"' in catalog
    assert 'callback_data=f"plans_choose_group:{plan_code}"' in catalog
    assert 'F.data.regexp(r"^plan_checkout:\\d+:(standard|pro):(catalog|group)$")' in catalog
    assert "answer_invoice(" in catalog
    assert "pay=True" in catalog
    assert 'text="◀️ Назад к описанию тарифа"' in catalog
    router_block = main.split("dp.include_routers(", 1)[1]
    assert router_block.index("plan_catalog.router") < router_block.index("billing.router")
    assert router_block.index("plan_catalog.router") < router_block.index("dashboard.router")


def test_diagnostics_has_fewer_steps_and_contextual_back():
    navigation = (ROOT / "app/handlers/navigation.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    home = (ROOT / "app/keyboards/home.py").read_text(encoding="utf-8")
    assert 'callback_data=f"health_direct:{group_id}"' in home
    assert 'callback_data=f"health_from_ops:{group_id}"' in home
    assert 'back_callback = f"ops:{group_id}" if source == "ops" else f"group:{group_id}"' in navigation
    assert "panel_keyboards.operations_menu = operations_menu" in main


def test_legacy_keyboard_users_get_guided_replacements():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    for replacement in (
        "panel_keyboards.main_menu = home_menu",
        "panel_keyboards.content_menu = content_menu",
        "panel_keyboards.settings_menu = settings_menu",
        "panel_keyboards.channels_admin_menu = channels_admin_menu",
        "panel_keyboards.operations_menu = operations_menu",
    ):
        assert replacement in main
    router_block = main.split("dp.include_routers(", 1)[1]
    assert router_block.index("navigation.router") < router_block.index("panel.router")


def test_standard_check_includes_callback_coverage():
    check = (ROOT / "scripts/check.sh").read_text(encoding="utf-8")
    assert "python scripts/check_callback_coverage.py" in check


def test_every_telegram_shortcut_is_guarded_at_bot_call_boundary():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "class PlainTextBot(Bot)" in main
    assert "async def __call__(" in main
    assert "_plain_method(method)" in main
    assert "bot = PlainTextBot(settings.bot_token)" in main
    for field in ("text", "caption", "title", "description", "explanation", "question"):
        assert f'"{field}"' in main
    assert 'updates["parse_mode"] = None' in main
    assert 'updates["caption_parse_mode"] = None' in main
    assert "_plain_reply_markup" in main


def test_ui_text_does_not_leak_any_html_like_markup():
    assert panel_header("Главное меню") == "🟣 Mimoru · Главное меню"
    cases = {
        "<b>Жирный</b>": "Жирный",
        "<code>команда</code>": "команда",
        '<a href="https://example.com">ссылка</a>': "ссылка",
        "&lt;b&gt;текст&lt;/b&gt;": "текст",
        "&amp;lt;code&amp;gt;текст&amp;lt;/code&amp;gt;": "текст",
    }
    for source, expected in cases.items():
        cleaned = clean_ui_text(source)
        assert cleaned == expected
        assert "<" not in cleaned
        assert ">" not in cleaned
