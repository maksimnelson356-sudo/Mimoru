from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _handler(source: str, name: str) -> str:
    body = source.split(f"async def {name}(", 1)[1]
    return body.split("@router.", 1)[0]


def test_navigation_setting_mutations_lock_but_menus_do_not() -> None:
    source = (ROOT / "app/handlers/navigation_fixes.py").read_text(encoding="utf-8")
    assert "def _warning_limit_menu" in source

    for name in (
        "warning_limit_set",
        "contextual_default_mute_set",
        "contextual_antiflood_set",
    ):
        body = _handler(source, name)
        assert "for_update=True" in body
        assert body.index("for_update=True") < body.index("await session.commit()")

    for name in (
        "warning_limit",
        "contextual_setting_num",
        "moderation_logs_with_contextual_back",
    ):
        assert "for_update=True" not in _handler(source, name)


def test_generic_panel_toggle_uses_locked_owner_boundary() -> None:
    source = (ROOT / "app/handlers/panel.py").read_text(encoding="utf-8")
    helper = source.split("async def owned_group(", 1)[1].split("@router.message", 1)[0]
    assert "for_update: bool = False" in helper
    assert "query = query.with_for_update()" in helper

    body = _handler(source, "toggle_setting")
    assert "for_update=True" in body
    assert body.index("for_update=True") < body.index("setattr(group.settings") < body.index("await session.commit()")
    assert "for_update=True" not in _handler(source, "group_section")


def test_navigation_fixes_precedes_later_panel_setters() -> None:
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    includes = source.split("dp.include_routers(", 1)[1]
    navigation = includes.index("\n        navigation_fixes.router,")
    control_center = includes.index("\n        control_center.router,")
    panel = includes.index("\n        panel.router,")
    assert navigation < control_center
    assert navigation < panel
