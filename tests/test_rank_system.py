from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetChatMember, PromoteChatMember

from app.services.ranks import (
    ADMIN_RANKS,
    CHAT_ADMIN,
    CHIEF_ADMIN,
    DEPUTY_OWNER,
    HELPER,
    MAJOR,
    RANK_LABELS,
    RANK_LEVELS,
    UNTOUCHABLE,
    VOICE_ADMIN,
    ActorRank,
    assignable_ranks,
    demote_telegram_admin,
    telegram_rights_for_rank,
)


def test_rank_hierarchy_is_strict() -> None:
    assert RANK_LEVELS[DEPUTY_OWNER] > RANK_LEVELS[CHIEF_ADMIN]
    assert RANK_LEVELS[CHIEF_ADMIN] > RANK_LEVELS[CHAT_ADMIN]
    assert RANK_LEVELS[CHAT_ADMIN] > RANK_LEVELS[VOICE_ADMIN]
    assert RANK_LEVELS[VOICE_ADMIN] > RANK_LEVELS[HELPER]
    assert RANK_LEVELS[HELPER] > RANK_LEVELS[MAJOR]
    assert RANK_LEVELS[MAJOR] > RANK_LEVELS[UNTOUCHABLE]
    assert RANK_LEVELS[UNTOUCHABLE] == 0


def test_assignment_scope_matches_hierarchy() -> None:
    deputy = ActorRank(DEPUTY_OWNER, RANK_LEVELS[DEPUTY_OWNER], None)
    chief = ActorRank(CHIEF_ADMIN, RANK_LEVELS[CHIEF_ADMIN], None)
    chat = ActorRank(CHAT_ADMIN, RANK_LEVELS[CHAT_ADMIN], None)

    assert DEPUTY_OWNER not in assignable_ranks(deputy)
    assert CHIEF_ADMIN in assignable_ranks(deputy)
    assert MAJOR in assignable_ranks(deputy)
    assert CHAT_ADMIN in assignable_ranks(chief)
    assert MAJOR in assignable_ranks(chief)
    assert CHIEF_ADMIN not in assignable_ranks(chief)
    assert assignable_ranks(chat) == (HELPER, MAJOR)


def test_deputy_and_chief_cannot_change_group_info() -> None:
    for rank in (DEPUTY_OWNER, CHIEF_ADMIN):
        rights = telegram_rights_for_rank(rank)
        assert rights["can_change_info"] is False
        assert rights["can_promote_members"] is True


def test_telegram_visible_and_internal_roles_are_separated() -> None:
    assert ADMIN_RANKS == {DEPUTY_OWNER, CHIEF_ADMIN, CHAT_ADMIN, MAJOR}
    assert VOICE_ADMIN not in ADMIN_RANKS
    assert HELPER not in ADMIN_RANKS
    assert UNTOUCHABLE not in ADMIN_RANKS

    chat = telegram_rights_for_rank(CHAT_ADMIN)
    major = telegram_rights_for_rank(MAJOR)
    voice = telegram_rights_for_rank(VOICE_ADMIN)
    assert chat["can_delete_messages"] is True
    assert chat["can_restrict_members"] is True
    assert chat["can_promote_members"] is False
    assert major["can_manage_chat"] is True
    assert major["can_change_info"] is False
    assert major["can_delete_messages"] is False
    assert major["can_invite_users"] is False
    assert major["can_restrict_members"] is False
    assert major["can_promote_members"] is False
    assert major["can_manage_video_chats"] is False
    assert voice["can_manage_video_chats"] is False
    assert voice["can_delete_messages"] is False
    assert voice["can_restrict_members"] is False


def test_user_facing_rank_labels_exist() -> None:
    assert RANK_LABELS == {
        DEPUTY_OWNER: "Зам. владельца",
        CHIEF_ADMIN: "Глав. админ",
        CHAT_ADMIN: "Администратор чата",
        VOICE_ADMIN: "Администратор голосовых чатов",
        HELPER: "Помощник",
        MAJOR: "Мажор",
        UNTOUCHABLE: "Недотрога",
    }


def test_untouchable_is_enforced_before_group_handlers() -> None:
    source = open("app/middlewares.py", encoding="utf-8").read()
    assignment = source.index("untouchable_exists = (")
    active = source.index("RankAssignment.active.is_(True)", assignment)
    rank = source.index("RankAssignment.rank_code == UNTOUCHABLE", assignment)
    gate = source.index("and untouchable", assignment)
    early_return = source.index("return None", gate)
    handler = source.index("result = await handler(event, data)", early_return)
    assert assignment < active < gate < early_return < handler
    assert assignment < rank < gate


# --- demote_telegram_admin: a stale bot-side flag must not block removal -----


def _group() -> SimpleNamespace:
    return SimpleNamespace(id=7, telegram_chat_id=-100123)


@pytest.mark.asyncio
async def test_demote_clears_the_custom_admin_title() -> None:
    """A removed admin must not keep the Mimoru tag in the Telegram admin list."""
    admin = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=admin),
        promote_chat_member=AsyncMock(),
    )
    assert await demote_telegram_admin(bot, _group(), 123) is True
    kwargs = bot.promote_chat_member.await_args.kwargs
    assert kwargs["custom_title"] == ""


def test_custom_admin_title_reaches_telegram_rights() -> None:
    # Without a stored tag the key is absent, so promote_chat_member never resets
    # a title Telegram set outside Mimoru.
    assert telegram_rights_for_rank(CHIEF_ADMIN).get("custom_title") is None
    assert telegram_rights_for_rank(CHAT_ADMIN, "Модератор")["custom_title"] == "Модератор"


def test_admin_title_is_stored_and_cleared_with_the_rank() -> None:
    model = open("app/db/rank_models.py", encoding="utf-8").read()
    assert 'custom_title: Mapped[str | None] = mapped_column(String(16), nullable=True)' in model

    roles = open("app/handlers/telegram_roles.py", encoding="utf-8").read()
    assert "assignment.custom_title = custom_title if rank_code in ADMIN_RANKS else None" in roles
    assert "assignment.custom_title = title" in roles
    assert "assignment.custom_title = None" in roles


def test_admin_title_is_owner_and_deputy_only() -> None:
    body = open("app/handlers/telegram_roles.py", encoding="utf-8").read()
    guard = body.split("async def can_set_admin_title(", 1)[1].split("\n\n\n", 1)[0]
    assert "is_service_owner(actor_id)" in guard
    assert "group.owner_telegram_id == actor_id" in guard
    assert "actor.code == DEPUTY_OWNER" in guard
    assert "assignment.rank_code not in ADMIN_RANKS" in guard


@pytest.mark.asyncio
async def test_demote_accepts_when_there_is_nothing_to_demote() -> None:
    """Regression guard: removal used to fail with «Не удалось снять
    Telegram-права администратора» when the person had already lost their admin
    rights outside the bot — Telegram strips them on leave, and the group owner
    can demote manually at any time.
    """
    gone = TelegramForbiddenError(
        method=GetChatMember(chat_id=-100123, user_id=123),
        message="bot is not a member of the group chat",
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=gone))
    assert await demote_telegram_admin(bot, _group(), 123) is True

    # A plain member (or a kicked one) has nothing left to demote either, and no
    # promote call is ever made for them.
    for status in (ChatMemberStatus.MEMBER, ChatMemberStatus.KICKED):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status))
        )
        assert await demote_telegram_admin(bot, _group(), 123) is True
        bot.get_chat_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_demote_still_fails_when_telegram_refuses_a_live_admin() -> None:
    admin = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=admin),
        promote_chat_member=AsyncMock(
            side_effect=TelegramForbiddenError(
                method=PromoteChatMember(chat_id=-100123, user_id=123),
                message="not enough rights",
            )
        ),
    )
    assert await demote_telegram_admin(bot, _group(), 123) is False
    bot.promote_chat_member.assert_awaited_once()


def test_manual_moderation_checks_target_rank() -> None:
    source = open("app/services/moderation.py", encoding="utf-8").read()
    assert "await can_moderate_target(session, group, moderator_id, target_id)" in source
