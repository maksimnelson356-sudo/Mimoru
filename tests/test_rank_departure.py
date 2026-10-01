"""Rank lifecycle when a person leaves a group, or a group leaves Mimoru.

Telegram silently drops administrator rights the moment a member leaves, but nothing
dropped Mimoru's own RankAssignment — that stale row kept granting panel access to
people who were no longer in the chat, and silently resurrected the whole previous
team when a group was reconnected. These tests pin the retired-rank behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import ModerationLog
from app.db.rank_models import RankAssignmentEvent
from app.services.rank_departure import (
    LEAVE_EVENT,
    RETURN_EVENT,
    SYSTEM_ACTOR_ID,
    deactivate_all_ranks,
    deactivate_rank_on_leave,
    note_member_return,
    rank_label,
)
from app.services.ranks import CHAT_ADMIN, HELPER, RANK_LABELS

USER_ID = 4_200_001


def group(group_id: int = 7):
    return SimpleNamespace(id=group_id)


def assignment(
    *,
    rank_code: str = HELPER,
    user_id: int = USER_ID,
    active: bool = True,
):
    return SimpleNamespace(
        rank_code=rank_code,
        user_telegram_id=user_id,
        active=active,
        telegram_admin_managed=True,
        restore_after_mute=True,
    )


def mock_session(*, scalar=None, rows=None):
    session = MagicMock()
    session.scalar = AsyncMock(return_value=scalar)
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: rows or []))
    session.add = MagicMock()
    session.delete = MagicMock()
    return session


def added(session) -> list:
    return [call.args[0] for call in session.add.call_args_list]


def events_added(session) -> list[RankAssignmentEvent]:
    return [obj for obj in added(session) if isinstance(obj, RankAssignmentEvent)]


def logs_added(session) -> list[ModerationLog]:
    return [obj for obj in added(session) if isinstance(obj, ModerationLog)]


@pytest.mark.asyncio
async def test_leaving_member_retires_the_bot_side_rank():
    row = assignment()
    session = mock_session(scalar=row)

    removed = await deactivate_rank_on_leave(session, group(), USER_ID)

    assert removed == HELPER
    assert row.active is False
    assert row.telegram_admin_managed is False
    assert row.restore_after_mute is False
    # History matters: the row is retired, never deleted.
    session.delete.assert_not_called()

    events = events_added(session)
    assert len(events) == 1
    assert events[0].action == LEAVE_EVENT
    assert events[0].old_rank_code == HELPER
    assert events[0].new_rank_code is None
    assert events[0].actor_telegram_id == SYSTEM_ACTOR_ID
    assert events[0].target_telegram_id == USER_ID

    logs = logs_added(session)
    assert len(logs) == 1
    assert logs[0].action == "rank_removed_by_leave"
    assert logs[0].actor_telegram_id == SYSTEM_ACTOR_ID
    assert logs[0].metadata_json == {"rank_code": HELPER}


@pytest.mark.asyncio
async def test_leaving_member_without_rank_changes_nothing():
    session = mock_session(scalar=None)

    assert await deactivate_rank_on_leave(session, group(), USER_ID) is None

    session.add.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_retires_every_active_rank_once():
    rows = [
        assignment(user_id=111_111),
        assignment(rank_code=CHAT_ADMIN, user_id=222_222),
    ]
    session = mock_session(rows=rows)

    assert await deactivate_all_ranks(session, group()) == 2

    assert [row.active for row in rows] == [False, False]
    assert [row.telegram_admin_managed for row in rows] == [False, False]
    assert [row.restore_after_mute for row in rows] == [False, False]
    assert len(events_added(session)) == 2
    assert all(event.action == LEAVE_EVENT for event in events_added(session))

    logs = logs_added(session)
    assert len(logs) == 1
    assert logs[0].action == "rank_removed_by_leave"
    assert logs[0].metadata_json == {"removed": 2}


@pytest.mark.asyncio
async def test_reconnect_without_staff_is_a_no_op():
    session = mock_session(rows=[])

    assert await deactivate_all_ranks(session, group()) == 0

    session.add.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_return_is_only_audited_when_there_was_a_previous_rank():
    session = mock_session()
    await note_member_return(session, group(), USER_ID, previous_rank=None)
    session.add.assert_not_called()

    await note_member_return(session, group(), USER_ID, previous_rank=CHAT_ADMIN)
    events = events_added(session)
    assert len(events) == 1
    assert events[0].action == RETURN_EVENT
    assert events[0].old_rank_code is None
    assert events[0].new_rank_code is None
    assert events[0].details == {"previous_rank_code": CHAT_ADMIN}


def test_rank_label_never_shows_a_bare_code():
    assert rank_label(None) == "без ранга"
    assert rank_label(HELPER) == RANK_LABELS[HELPER]
    assert rank_label("future_rank") == "future_rank"


# --- wiring contracts -------------------------------------------------------

MEMBERS_SOURCE = Path("app/handlers/members.py").read_text(encoding="utf-8")
DISCONNECT_SOURCE = Path("app/services/group_disconnects.py").read_text(encoding="utf-8")
ONBOARDING_SOURCE = Path("app/handlers/group_onboarding_flow.py").read_text(encoding="utf-8")


def test_leave_and_return_are_wired_into_chat_member_updates():
    assert "deactivate_rank_on_leave(session, group, target.id)" in MEMBERS_SOURCE
    assert "note_member_return(session, group, target.id" in MEMBERS_SOURCE
    assert "not present and was_present" in MEMBERS_SOURCE
    assert "present and not was_present" in MEMBERS_SOURCE
    # A disconnected group has no live staff state and must stay silent.
    assert "if not group.is_active:" in MEMBERS_SOURCE


def test_rank_notices_go_to_the_owner_and_never_print_raw_ids():
    assert "if owner_notice and group.owner_telegram_id:" in MEMBERS_SOURCE
    blocks = re.findall(r"owner_notice = \((.*?)\n            \)", MEMBERS_SOURCE, re.DOTALL)
    assert len(blocks) == 2, "expected one notice for leaving and one for returning"
    for block in blocks:
        assert "public_user_token(target.id)" in block
        assert not re.search(r"\{target\.id\}|\{user_id\}", block)


def test_disconnect_and_reconnect_paths_use_the_shared_service():
    assert "await deactivate_all_ranks(session, group)" in DISCONNECT_SOURCE
    # Reconnect starts from a clean slate: the pending disconnect intent is dropped
    # and the old team is not pushed back into Telegram admin rights.
    assert "delete(GroupDisconnectIntent)" in ONBOARDING_SOURCE
    assert "group.is_active = True" in ONBOARDING_SOURCE
    # The owner is asked to re-promote the bot; nobody on the old team is
    # re-appointed automatically anywhere in this flow.
    assert "_admin_promotion_markup" in ONBOARDING_SOURCE
    assert "RankAssignment" not in ONBOARDING_SOURCE
