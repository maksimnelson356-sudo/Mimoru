from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_contextual_back_routes_are_kept_for_nested_group_screens() -> None:
    source = (ROOT / "app/handlers/navigation_fixes.py").read_text(encoding="utf-8")
    assert 'callback_data=f"group_section:{group_id}:settings"' in source
    # Check for back button to moderation - text and callback_data on separate lines
    assert 'text="◀ К модерации"' in source
    assert 'callback_data=f"group_section:{group.id}:moderation"' in source
    # Check for member card back button
    assert '"◀️ Назад к карточке"' in source
    # Check that callback_data patterns are present
    assert 'callback_data=f"group_section:{group.id}:moderation"' in source


def test_text_form_cancel_never_leaves_cancelled_fsm_active() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    marker = "and _callback_is_cancel_notice(event, state_data_before)"
    assert marker in source
    marker_pos = source.index(marker)
    clear_pos = source.index("await state.clear()", marker_pos)
    handler_pos = source.index("result = await handler(event, data)")
    assert marker_pos < clear_pos < handler_pos


def test_advertising_text_forms_return_to_their_editor() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    assert 'name.startswith("GlobalPostForm:")' in source
    assert 'return f"gpost:editor:{item_id}"' in source
    assert 'name.startswith("RequiredListingForm:")' in source
    assert 'return f"reqlist:group:{group_id}"' in source
    assert 'name.startswith("RequiredDealForm:")' in source
    assert 'return f"reqmarket:{listing_id}"' in source