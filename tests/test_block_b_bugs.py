"""Regression tests for Block B bugs: Back/Cancel keyboard loss and OP enforcement."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── Bug 1: Back/Cancel keyboard preservation ──────────────────────────────


def test_middleware_skips_remove_cancel_notice_on_cancel_button_click() -> None:
    """When user clicks the cancel button, _remove_cancel_notice must NOT fire
    because the destination handler already replaced the message keyboard."""
    source = _read("app/middlewares.py")
    assert "_callback_is_cancel_notice(event, state_data_before)" in source
    assert "not (" in source or "if not (" in source


def test_cancel_callback_has_explicit_block_b_routes() -> None:
    """The _cancel_callback function must handle Block B FSM state names so
    the cancel button routes to the correct parent screen."""
    source = _read("app/middlewares.py")
    assert "def _cancel_callback(" in source
    assert "_cancel_message_id" in source


def test_all_block_b_fsm_inputs_provide_reply_markup() -> None:
    """Every FSM state entry in ad_market_v3.py must set reply_markup via _back()."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "reply_markup=_back(" in handler
    assert 'reply_markup=_back(f"gpost:editor:{item.id}"' in handler
    assert 'reply_markup=_back(f"reqlist:group:{group.id}"' in handler
    assert 'reply_markup=_back(f"reqmarket:{listing.id}"' in handler


def test_global_editor_provides_keyboard_on_return() -> None:
    """The gpost:editor handler must always provide reply_markup."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert '_global_editor_keyboard(item)' in handler
    assert '_global_editor_text(item)' in handler


def test_seller_listing_render_provides_keyboard() -> None:
    """The reqlist:group handler must always provide reply_markup via _render_seller_listing."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "_render_seller_listing(callback, bot, session, group)" in handler
    assert "reply_markup=InlineKeyboardMarkup" in handler


def test_market_detail_provides_keyboard() -> None:
    """The reqmarket: handler must provide reply_markup."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'reply_markup=InlineKeyboardMarkup(inline_keyboard=[' in handler


# ── Bug 2: OP enforcement for existing members ────────────────────────────


def test_restrict_existing_unsubscribed_members_exists() -> None:
    """The restriction function must exist in required_direct.py."""
    source = _read("app/handlers/required_direct.py")
    assert "async def restrict_existing_unsubscribed_members(" in source


def test_restrict_existing_uses_verification_keyboard() -> None:
    """The restriction function must send a verification_keyboard to muted members."""
    source = _read("app/handlers/required_direct.py")
    assert "verification_keyboard(" in source
    assert "restrict_chat_member(" in source


def test_restrict_existing_checks_admin_status() -> None:
    """The restriction function must skip admins/creators."""
    source = _read("app/handlers/required_direct.py")
    assert "is_admin(" in source


def test_restrict_existing_checks_trusted_users() -> None:
    """The restriction function must skip trusted users."""
    source = _read("app/handlers/required_direct.py")
    assert "TrustedUser" in source


def test_restrict_existing_checks_subscription() -> None:
    """The restriction function must skip already-subscribed members."""
    source = _read("app/handlers/required_direct.py")
    assert "is_subscribed(" in source


def test_restrict_existing_creates_captcha_session() -> None:
    """The restriction function must create a Redis captcha session for each restricted member."""
    source = _read("app/handlers/required_direct.py")
    assert 'redis.set(' in source
    assert "captcha:" in source


def test_restrict_existing_uses_own_session() -> None:
    """The restriction function must create its own DB session (fire-and-forget task)."""
    source = _read("app/handlers/required_direct.py")
    assert "SessionFactory()" in source


def test_direct_connect_enqueues_durable_member_restriction() -> None:
    """The direct connect handler must persist a retryable restriction intent."""
    source = _read("app/handlers/required_direct.py")
    assert "enqueue_required_reconcile(" in source
    assert 'dedupe_key=f"direct:{rule.id}:{message.message_id}"' in source
    assert "asyncio.create_task(" not in source


def test_marketplace_accept_enqueues_durable_member_restriction() -> None:
    """The marketplace deal accept handler must persist a retryable intent."""
    source = _read("app/handlers/ad_market_atomic.py")
    assert 'dedupe_key=f"ad:{deal.id}"' in source
    assert "asyncio.create_task(" not in source


def test_marketplace_accept_has_redis_parameter() -> None:
    """The atomic_required_deal_decision handler must accept Redis for restriction task."""
    source = _read("app/handlers/ad_market_atomic.py")
    assert "redis: Redis" in source


def test_direct_connect_has_redis_parameter() -> None:
    """The direct_required_connect handler must accept Redis for restriction task."""
    source = _read("app/handlers/required_direct.py")
    assert "redis: Redis" in source


def test_restrict_existing_handles_rate_limits() -> None:
    """The restriction function must sleep between Telegram API calls to avoid rate limits."""
    source = _read("app/handlers/required_direct.py")
    assert "asyncio.sleep(0.05)" in source


# ── Resource picker: buyer OP target selection ──────────────────────────────


def test_required_deal_start_shows_resource_picker_keyboard() -> None:
    """The reqdeal:start handler must query buyer's groups and show inline buttons."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "Group.owner_telegram_id == callback.from_user.id" in handler
    assert "Group.is_active.is_(True)" in handler
    assert 'callback_data=f"reqdeal:pick:{listing.id}:{group.id}"' in handler
    assert '➕ Ввести @username или ссылку вручную' in handler
    assert '◀️ Назад' in handler
    assert '✖️ Отмена' in handler


def test_required_deal_start_has_empty_groups_message() -> None:
    """When buyer has no groups, the picker must show a helpful message."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "У вас пока нет активных групп Mimoru" in handler


def test_required_deal_pick_handler_exists() -> None:
    """The reqdeal:pick callback handler must exist."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "async def required_deal_pick(" in handler
    assert 'F.data.regexp(r"^reqdeal:pick:\\d+:\\d+$")' in handler


def test_required_deal_pick_validates_ownership() -> None:
    """The pick handler must verify the buyer owns the selected group."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "group.owner_telegram_id != callback.from_user.id" in handler


def test_required_deal_pick_creates_deal() -> None:
    """The pick handler must create a RequiredAdDealRequest with the selected group."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "RequiredAdDealRequest(" in handler
    assert "target_resource=target" in handler
    assert "group.telegram_chat_id" in handler


def test_required_deal_pick_checks_duplicates() -> None:
    """The pick handler must check for pending duplicate deals."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'RequiredAdDealRequest.status == "pending"' in handler
    assert "У вас уже есть ожидающий запрос" in handler


def test_required_deal_pick_sends_seller_notification() -> None:
    """The pick handler must notify the seller with accept/reject buttons."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'reqdeal:accept:{deal.id}' in handler
    assert 'reqdeal:reject:{deal.id}' in handler


def test_required_deal_pick_shows_confirmation() -> None:
    """After successful pick, the buyer must see a confirmation screen."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'panel_header("Запрос отправлен"' in handler
    assert 'reqdeal:buyer' in handler


def test_required_deal_manual_handler_exists() -> None:
    """The reqdeal:manual callback handler must exist for manual input."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "async def required_deal_manual(" in handler
    assert 'F.data.regexp(r"^reqdeal:manual:\\d+$")' in handler


def test_required_deal_manual_sets_fsm_state() -> None:
    """The manual handler must set the FSM state for text input."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "RequiredDealForm.target" in handler
    assert "state.set_state(RequiredDealForm.target)" in handler


def test_required_deal_manual_provides_text_prompt() -> None:
    """The manual handler must show the text prompt with Back button."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert 'Отправьте публичный @username, ссылку t.me/username или invite-ссылку' in handler
    assert "reply_markup=_back(" in handler


def test_required_deal_manual_validates_listing() -> None:
    """The manual handler must validate the listing exists and is active."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "listing is None or not listing.active" in handler
    assert "Объявление недоступно" in handler


def test_required_deal_form_has_manual_target_state() -> None:
    """The RequiredDealForm must have a manual_target state for FSM."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "manual_target = State()" in handler


def test_resource_picker_reuses_existing_target_handler() -> None:
    """The existing RequiredDealForm.target FSM handler must still work for manual input."""
    handler = _read("app/handlers/ad_market_v3.py")
    assert "@router.message(RequiredDealForm.target, F.chat.type == \"private\")" in handler
    assert "normalize_public_telegram_resource(message.text" in handler


def test_pick_handler_uses_bot_parameter() -> None:
    """The pick handler must accept bot: Bot for sending seller notification."""
    handler = _read("app/handlers/ad_market_v3.py")
    pick_section = handler.split("async def required_deal_pick(", 1)[1]
    assert "bot: Bot" in pick_section.split("async def ")[0]


def test_resource_picker_does_not_break_atomic_flow() -> None:
    """The atomic_required_deal_target handler must still handle RequiredDealForm.target."""
    atomic = _read("app/handlers/ad_market_atomic.py")
    assert "@router.message(RequiredDealForm.target, F.chat.type == \"private\")" in atomic
    assert "normalize_public_telegram_resource(message.text" in atomic
