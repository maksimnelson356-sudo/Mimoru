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
        VOICE_ADMIN: "Администратор войска",
        HELPER: "Помощник",
        MAJOR: "Мажёр",
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


def test_manual_moderation_checks_target_rank() -> None:
    source = open("app/services/moderation.py", encoding="utf-8").read()
    assert "await can_moderate_target(session, group, moderator_id, target_id)" in source
