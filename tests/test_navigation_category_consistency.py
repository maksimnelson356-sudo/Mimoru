from pathlib import Path
from types import SimpleNamespace

from app.keyboards.home import automation_menu, group_health_menu, members_menu, protection_menu


ROOT = Path(__file__).resolve().parents[1]


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _callbacks(markup):
    return [button.callback_data for button in _buttons(markup) if button.callback_data]


def _texts(markup):
    return [button.text for button in _buttons(markup)]


def _router_names() -> list[str]:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    block = main.split("dp.include_routers(", 1)[1].split(")", 1)[0]
    return [line.strip().rstrip(",") for line in block.splitlines() if line.strip().endswith("router,")]


def _group():
    settings = SimpleNamespace(
        antiflood_enabled=True,
        repeats_enabled=True,
        links_enabled=False,
        caps_enabled=False,
        captcha_enabled=True,
        newcomer_quarantine_enabled=False,
        edit_protection_enabled=True,
        mention_filter_enabled=True,
        sender_chat_filter_enabled=True,
        anti_raid_enabled=True,
        automation_enabled=True,
        deleted_cleanup_schedule="off",
        warning_expire_hours=24,
        warnings_limit=3,
    )
    return SimpleNamespace(id=7, settings=settings)


def test_protection_does_not_jump_to_content_on_back():
    markup = protection_menu(_group())
    assert "words:7" not in _callbacks(markup)
    assert not any("Запрещён" in text for text in _texts(markup))
    assert "group:7" in _callbacks(markup)


def test_members_does_not_duplicate_moderation_sections():
    markup = members_menu(7)
    callbacks = _callbacks(markup)
    texts = _texts(markup)
    assert "roles:7" not in callbacks
    assert "logs:7" not in callbacks
    assert not any("Роли модераторов" in text for text in texts)
    assert not any("Журнал действий" in text for text in texts)
    assert "group:7" in callbacks


def test_automation_warning_limit_returns_to_automation():
    markup = automation_menu(_group())
    assert "automation_warning_limit:7" in _callbacks(markup)
    fixes = (ROOT / "app/handlers/navigation_fixes.py").read_text(encoding="utf-8")
    assert 'callback_data=f"automation:{group_id}"' in fixes
    assert "automation_warning_limit_set" in fixes
    assert 'reply_markup=automation_menu(group)' in fixes


def test_nested_group_screens_return_to_their_actual_parent():
    fixes = (ROOT / "app/handlers/navigation_fixes.py").read_text(encoding="utf-8")
    assert 'F.data.regexp(r"^logs:\\d+$")' in fixes
    assert 'group_section:{group.id}:moderation' in fixes
    assert 'F.data.regexp(r"^members_stats:\\d+$")' in fixes
    assert 'group_section:{group.id}:members' in fixes
    assert 'F.data.regexp(r"^member_history:\\d+:-?\\d+$")' in fixes
    assert 'member_card:{group.id}:{user_id}' in fixes


def test_contextual_numeric_settings_return_to_source_category():
    fixes = (ROOT / "app/handlers/navigation_fixes.py").read_text(encoding="utf-8")
    assert 'F.data.regexp(r"^setting_num:\\d+:(defaultmute|antiflood)$")' in fixes
    assert 'group_section:{group_id}:moderation' in fixes
    assert 'group_section:{group_id}:protection' in fixes
    assert 'reply_markup=moderation_menu(group.id)' in fixes
    assert 'reply_markup=protection_menu(group)' in fixes


def test_setup_cancel_returns_to_settings_instead_of_skipping_to_group():
    fixes = (ROOT / "app/handlers/navigation_fixes.py").read_text(encoding="utf-8")
    assert 'F.data.regexp(r"^setup:\\d+:start$")' in fixes
    assert 'text="✖️ Отмена", callback_data=f"group_section:{group_id}:settings"' in fixes


def test_navigation_fixes_precede_legacy_owners_of_same_callbacks():
    routers = _router_names()
    fixes = routers.index("navigation_fixes.router")
    for legacy in ("control_center.router", "onboarding.router", "member_center.router", "panel.router", "dashboard.router"):
        assert fixes < routers.index(legacy)


def test_health_legacy_back_goes_to_group_not_settings():
    markup = group_health_menu(7)
    callbacks = _callbacks(markup)
    assert "group:7" in callbacks
    assert "group_section:7:settings" not in callbacks


def test_runtime_replaces_all_category_sensitive_legacy_keyboards():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    for replacement in (
        "panel_keyboards.protection_menu = protection_menu",
        "panel_keyboards.members_menu = members_menu",
        "panel_keyboards.content_menu = content_menu",
        "panel_keyboards.settings_menu = settings_menu",
        "panel_keyboards.channels_admin_menu = channels_admin_menu",
        "panel_keyboards.automation_menu = automation_menu",
        "panel_keyboards.operations_menu = operations_menu",
        "panel_keyboards.group_health_menu = group_health_menu",
    ):
        assert replacement in main
        assert main.index(replacement) < main.index("from app.handlers import")


def test_current_advertising_results_have_contextual_back_routes():
    market = (ROOT / "app/handlers/ad_market_v3.py").read_text(encoding="utf-8")
    billing = (ROOT / "app/handlers/billing.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'callback_data="gpost:mine"' in market
    assert 'callback_data="reqdeal:buyer"' in market
    assert 'callback_data="reqdeal:seller"' in market
    assert 'callback_data="ads:home"' in market
    assert 'callback_data=f"plan:{group.id}"' in billing
    router_block = main.split("dp.include_routers(", 1)[1]
    assert router_block.index("ad_market_v3.router") < router_block.index("billing.router")
