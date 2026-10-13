"""Снятие ранга обязано забрать права администратора в самой группе.

Регрессия к исходному багу: назначение в режиме «Telegram + Mimoru» поверх
уже существующего Telegram-администратора идёт через привязку, где
telegram_admin_managed остаётся false. Снятие ранга опиралось только на этот
флаг, поэтому обращения к группе не было вовсе: Mimuru писала «Ранг снят», а
человек продолжал числиться администратором.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import PromoteChatMember
from aiogram.types import ChatPermissions

from app.services import rank_provisioning as provisioning


def _assignment(**overrides):
    base = dict(
        id=23,
        group_id=7,
        user_telegram_id=8755815740,
        rank_code="chat_admin",
        access_mode="telegram",
        telegram_admin_managed=False,
        permissions={},
        active=True,
        helper_for_telegram_id=None,
        assigned_by_telegram_id=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _group():
    return SimpleNamespace(id=7, telegram_chat_id=-100123, is_active=True, owner_telegram_id=999)


@pytest.mark.asyncio
async def test_removal_demotes_even_when_the_managed_flag_is_false(monkeypatch) -> None:
    """Ключевой случай: флаг false, но режим телеграмный — снять надо."""
    assignment = _assignment(telegram_admin_managed=False)
    group = _group()

    monkeypatch.setattr(
        provisioning, "can_remove_assignment", AsyncMock(return_value=(True, ""))
    )

    captured: dict = {}

    async def fake_create_intent(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=1)

    async def fake_execute(_bot, _session, *, intent_id):
        return True, "", SimpleNamespace(id=assignment.id)

    monkeypatch.setattr(provisioning, "_create_intent", fake_create_intent)
    monkeypatch.setattr(provisioning, "_execute_live_intent", fake_execute)

    ok, error = await provisioning.remove_assignment(
        AsyncMock(), AsyncMock(), group, 1, assignment
    )

    assert ok is True
    assert error == ""
    assert captured["telegram_action"] == "demote"
    assert captured["payload"]["demoted_telegram_admin"] is True


@pytest.mark.asyncio
async def test_removal_leaves_bot_only_assignment_without_telegram_effect(
    monkeypatch,
) -> None:
    """Ранг «Только внутри Mimoru» не должен дёргать Telegram."""
    assignment = _assignment(access_mode="bot_only", rank_code="helper")
    group = _group()

    monkeypatch.setattr(
        provisioning, "can_remove_assignment", AsyncMock(return_value=(True, ""))
    )

    captured: dict = {}

    async def fake_create_intent(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=2)

    monkeypatch.setattr(provisioning, "_create_intent", fake_create_intent)
    monkeypatch.setattr(
        provisioning, "_execute_live_intent", AsyncMock(return_value=(True, "", None))
    )

    ok, _ = await provisioning.remove_assignment(AsyncMock(), AsyncMock(), group, 1, assignment)

    assert ok is True
    assert captured["telegram_action"] == "none"
    assert captured["payload"]["demoted_telegram_admin"] is False


@pytest.mark.asyncio
async def test_refused_demotion_is_reported_instead_of_claiming_success() -> None:
    """Telegram отклонил снятие прав — ранг обязан остаться, а не исчезнуть."""
    from app.services.ranks import demote_telegram_admin

    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
        ),
        promote_chat_member=AsyncMock(
            side_effect=TelegramBadRequest(
                method=PromoteChatMember(
                    chat_id=-100123,
                    user_id=8755815740,
                    permissions=ChatPermissions(),
                ),
                message="Bad Request: CHAT_ADMIN_REQUIRED",
            )
        ),
    )

    assert await demote_telegram_admin(bot, _group(), 8755815740) is False