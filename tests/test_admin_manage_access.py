"""Panel admins manage the group; strict money/role paths stay owner-only.

Two drift bugs motivated this:

1. Group commands called ``can_manage_group`` without the session, so the rank
   branch was unreachable and admins were refused with «Изменять настройки может
   только владелец группы» — even for reading the rules.
2. FSM submit gates re-checked ownership with a stricter helper than the entry
   gate: the form opened for an admin and then rejected their typed input with
   «Доступ к группе потерян».
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus

from app.services import access as access_module
from app.services import rank_access
from app.services.access import ADMIN_RANKS_FOR_PANEL, can_manage_group

ROOT = Path(__file__).resolve().parents[1]

OWNER_ID = 555
OTHER_ID = 424_242_421


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        access_module,
        "get_settings",
        lambda: SimpleNamespace(service_owner_ids=frozenset()),
    )


def group() -> SimpleNamespace:
    return SimpleNamespace(id=7, owner_telegram_id=OWNER_ID, telegram_chat_id=-100123)


def actor(code: str | None):
    async def fake(bot, session, group, user_id):
        return SimpleNamespace(code=code) if code else None

    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(ADMIN_RANKS_FOR_PANEL))
async def test_panel_admin_rank_may_manage(monkeypatch, code: str) -> None:
    monkeypatch.setattr(rank_access, "get_actor_rank_with_access", actor(code))

    assert await can_manage_group(object(), group(), OTHER_ID, MagicMock()) is True


@pytest.mark.asyncio
async def test_non_panel_rank_may_not_manage(monkeypatch) -> None:
    monkeypatch.setattr(rank_access, "get_actor_rank_with_access", actor("helper"))

    assert await can_manage_group(object(), group(), OTHER_ID, MagicMock()) is False


@pytest.mark.asyncio
async def test_without_session_no_rank_lookup_happens(monkeypatch) -> None:
    async def explode(bot, session, group, user_id):
        raise AssertionError("rank lookup must not run without a session")

    monkeypatch.setattr(rank_access, "get_actor_rank_with_access", explode)

    assert await can_manage_group(object(), group(), OTHER_ID) is False


@pytest.mark.asyncio
async def test_owner_keeps_the_telegram_admin_requirement() -> None:
    admin_member = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=admin_member))
    assert await can_manage_group(bot, group(), OWNER_ID) is True

    plain_member = SimpleNamespace(status=ChatMemberStatus.MEMBER)
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=plain_member))
    assert await can_manage_group(bot, group(), OWNER_ID) is False


# --- wiring contracts -------------------------------------------------------


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_group_commands_authorize_with_the_session() -> None:
    features = _read("app/handlers/features.py")
    assert "can_manage_group(bot, group, message.from_user.id, session)" in features
    assert "Изменять настройки может владелец или администратор группы." in features

    group_src = _read("app/handlers/group.py")
    broadened = group_src.count("can_manage_group(bot, group, message.from_user.id, session)")
    assert broadened == 10
    # Role assignment/removal/listing stays owner-only: session is omitted on
    # purpose so no rank lookup ever happens there.
    strict = group_src.count("can_manage_group(bot, group, message.from_user.id):")
    assert strict == 3
    assert "Изменять настройки может владелец или администратор группы." in group_src
    assert "Назначать роли может только владелец группы." in group_src
    assert "Снимать роли может только владелец группы." in group_src


MANAGE_SCREENS = (
    "app/handlers/control_center.py",
    "app/handlers/reason_admin.py",
    "app/handlers/member_center.py",
    "app/handlers/panel.py",
    "app/handlers/dashboard.py",
    "app/handlers/automation.py",
    "app/handlers/deleted_accounts.py",
)


@pytest.mark.parametrize("path", MANAGE_SCREENS)
def test_write_gate_accepts_what_the_entry_gate_opens(path: str) -> None:
    """«Доступ к группе потерян» regression guard.

    Every management screen an admin can open must re-check input with the same
    subject set: owner / service owner / panel admin ranks.
    """
    source = _read(path)
    rest = source[source.index("async def owned_group") :]
    boundaries = [
        index
        for marker in ("\nasync def ", "\n@router.", "\ndef ")
        if (index := rest.find(marker, 1)) >= 0
    ]
    helper = rest[: min(boundaries)] if boundaries else rest[:600]
    assert "owner_or_admin_clause(user_id)" in helper
    assert "if not is_service_owner(" in helper or "if not access_service_owner(" in helper
    if not path.endswith("dashboard.py"):
        assert "with_for_update" in helper


def test_money_paths_stay_owner_only() -> None:
    access = _read("app/services/access.py")
    money = access.split("async def owned_group(", 1)[1].split("async def accessible_group", 1)[0]
    assert "owner_or_admin_clause(user_id)" not in money
    assert "query = query.where(Group.owner_telegram_id == user_id)" in money

    plan = _read("app/handlers/plan_catalog.py")
    assert "from app.services.access import accessible_group, is_service_owner, owned_group" in plan
