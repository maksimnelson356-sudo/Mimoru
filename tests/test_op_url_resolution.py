"""Regression tests for OP subscription URL resolution (broken -100 chat_id as URL)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── resolve_channel_url ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_channel_url_at_username() -> None:
    """@username channels resolve directly to https://t.me/<username>."""
    from app.services.required_resources import resolve_channel_url

    bot = AsyncMock()
    result = await resolve_channel_url(bot, "@mychannel")
    assert result == "https://t.me/mychannel"
    bot.get_chat.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_channel_url_numeric_chat_id_with_username() -> None:
    """A numeric chat_id resolves via bot.get_chat when the chat has a username."""
    from app.services.required_resources import resolve_channel_url

    bot = AsyncMock()
    mock_chat = MagicMock()
    mock_chat.username = "mygroup"
    mock_chat.invite_link = None
    bot.get_chat.return_value = mock_chat

    result = await resolve_channel_url(bot, "-1001234567890")
    assert result == "https://t.me/mygroup"
    bot.get_chat.assert_called_once_with("-1001234567890")


@pytest.mark.asyncio
async def test_resolve_channel_url_numeric_chat_id_no_username_with_invite() -> None:
    """A private chat with no username but with invite_link uses the invite link."""
    from app.services.required_resources import resolve_channel_url

    bot = AsyncMock()
    mock_chat = MagicMock()
    mock_chat.username = None
    mock_chat.invite_link = "https://t.me/+abcDEF123"
    bot.get_chat.return_value = mock_chat

    result = await resolve_channel_url(bot, "-1001234567890")
    assert result == "https://t.me/+abcDEF123"


@pytest.mark.asyncio
async def test_resolve_channel_url_numeric_no_username_no_invite() -> None:
    """A private chat with no username and no invite link returns None."""
    from app.services.required_resources import resolve_channel_url

    bot = AsyncMock()
    mock_chat = MagicMock()
    mock_chat.username = None
    mock_chat.invite_link = None
    bot.get_chat.return_value = mock_chat

    result = await resolve_channel_url(bot, "-1001234567890")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_channel_url_get_chat_fails() -> None:
    """If bot.get_chat raises, the function returns None (no broken URL)."""
    from app.services.required_resources import resolve_channel_url

    from aiogram.exceptions import TelegramBadRequest

    bot = AsyncMock()
    bot.get_chat.side_effect = TelegramBadRequest(method="getChat", message="chat not found")

    result = await resolve_channel_url(bot, "-1001234567890")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_channel_url_invite_link_fast_path() -> None:
    """Invite links are returned directly without API call."""
    from app.services.required_resources import resolve_channel_url

    bot = AsyncMock()
    result = await resolve_channel_url(bot, "https://t.me/+abcDEF123")
    assert result == "https://t.me/+abcDEF123"
    bot.get_chat.assert_not_called()


# ── validate_invite_link ─────────────────────────────────────────────────────


def test_validate_invite_link_valid() -> None:
    from app.services.required_resources import validate_invite_link

    assert validate_invite_link("https://t.me/+abcDEF123") == "https://t.me/+abcDEF123"
    assert validate_invite_link("t.me/+abcDEF123") == "https://t.me/+abcDEF123"


def test_validate_invite_link_invalid() -> None:
    from app.services.required_resources import validate_invite_link

    assert validate_invite_link("https://t.me/channel") is None
    assert validate_invite_link("https://t.me/+") is None
    assert validate_invite_link("https://example.com/+abc") is None
    assert validate_invite_link("") is None


# ── normalize_public_telegram_resource rejects numeric IDs ───────────────────


def test_normalize_rejects_numeric_chat_id() -> None:
    """Numeric chat_ids like -100... must never be accepted as @username."""
    from app.services.required_resources import normalize_public_telegram_resource

    assert normalize_public_telegram_resource("-1001234567890") is None
    assert normalize_public_telegram_resource("123456789") is None
    assert normalize_public_telegram_resource("-1002316753526") is None


# ── verification_keyboard uses resolve_channel_url ───────────────────────────


def test_verification_keyboard_is_async() -> None:
    """verification_keyboard must be async to call resolve_channel_url."""
    source = _read("app/handlers/members.py")
    assert "async def verification_keyboard(" in source


def test_verification_keyboard_imports_resolve_channel_url() -> None:
    """members.py must import resolve_channel_url."""
    source = _read("app/handlers/members.py")
    assert "from app.services.required_resources import resolve_channel_url" in source


def test_verification_keyboard_uses_resolve_channel_url() -> None:
    """verification_keyboard must call resolve_channel_url for each channel."""
    source = _read("app/handlers/members.py")
    assert "await resolve_channel_url(bot, channel)" in source


def test_verification_keyboard_fallback_noop_button() -> None:
    """When URL is None, a noop callback button is shown instead of a broken URL."""
    source = _read("app/handlers/members.py")
    assert 'callback_data=f"noop:ch:{channel}"' in source


def test_verification_keyboard_never_builds_tme_from_chat_id() -> None:
    """The old code built t.me URLs from channel strings directly. This must be gone."""
    source = _read("app/handlers/members.py")
    assert 'f"https://t.me/{username}"' not in source or "resolve_channel_url" in source


def test_welcome_passes_bot_to_verification_keyboard() -> None:
    """The welcome handler must pass bot= to verification_keyboard."""
    source = _read("app/handlers/members.py")
    assert "await verification_keyboard(" in source
    assert "bot=bot)" in source


def test_restrict_existing_passes_bot_to_verification_keyboard() -> None:
    """restrict_existing_unsubscribed_members must pass bot= to verification_keyboard."""
    source = _read("app/handlers/required_direct.py")
    assert "await verification_keyboard(" in source
    assert "bot=bot)" in source


# ── Resource picker resolves username ────────────────────────────────────────


def test_resource_picker_resolves_username() -> None:
    """The resource picker must resolve group username via bot.get_chat."""
    source = _read("app/handlers/ad_market_v3.py")
    assert "await bot.get_chat(group.telegram_chat_id)" in source
    assert "resolved_username" in source


def test_resource_picker_rejects_no_username_no_invite() -> None:
    """The picker must reject groups without username or invite link."""
    source = _read("app/handlers/ad_market_v3.py")
    assert "не имеет публичного @username" in source


def test_resource_picker_uses_username_or_invite() -> None:
    """The picker must use @username if available, then invite_link."""
    source = _read("app/handlers/ad_market_v3.py")
    assert 'target = f"@{resolved_username}"' in source
    assert "invite_link" in source


def test_resource_picker_does_not_store_raw_chat_id() -> None:
    """The picker must NOT store raw numeric chat_id as target_resource."""
    source = _read("app/handlers/ad_market_v3.py")
    # The old code was: target = str(group.telegram_chat_id)
    # Now it must go through resolution
    assert "target = f\"@{resolved_username}\"" in source


# ── Manual input accepts invite links ────────────────────────────────────────


def test_manual_target_accepts_invite_links() -> None:
    """The manual target FSM handler must accept invite links."""
    v3_source = _read("app/handlers/ad_market_v3.py")
    atomic_source = _read("app/handlers/ad_market_atomic.py")
    assert "validate_invite_link(message.text" in v3_source
    assert "validate_invite_link(message.text" in atomic_source


def test_manual_target_imports_validate_invite_link() -> None:
    """Both v3 and atomic handlers must import validate_invite_link."""
    v3_source = _read("app/handlers/ad_market_v3.py")
    atomic_source = _read("app/handlers/ad_market_atomic.py")
    assert "validate_invite_link" in v3_source
    assert "validate_invite_link" in atomic_source


def test_manual_target_mentions_invite_in_prompt() -> None:
    """The manual input prompt must mention invite links as an option."""
    v3_source = _read("app/handlers/ad_market_v3.py")
    assert "invite-ссылку" in v3_source


# ── chat_id still used for membership check (not broken) ─────────────────────


def test_is_subscribed_uses_channel_string() -> None:
    """is_subscribed must still accept channel strings for get_chat_member."""
    source = _read("app/handlers/members.py")
    assert "bot.get_chat_member(channel, user_id)" in source


def test_required_channels_returns_channel_username() -> None:
    """required_channels must still return channel_username from RequiredChannel."""
    source = _read("app/handlers/members.py")
    assert "RequiredChannel.channel_username" in source


# ── All OP flows covered ─────────────────────────────────────────────────────


def test_direct_connect_triggers_restrict_with_bot() -> None:
    """Direct command flow must pass bot to restrict task."""
    source = _read("app/handlers/required_direct.py")
    assert "restrict_existing_unsubscribed_members(" in source


def test_atomic_deal_decision_enqueues_restrict() -> None:
    """Atomic marketplace flow must enqueue a durable restriction intent."""
    source = _read("app/handlers/ad_market_atomic.py")
    assert 'dedupe_key=f"ad:{deal.id}"' in source
    assert "asyncio.create_task(" not in source
