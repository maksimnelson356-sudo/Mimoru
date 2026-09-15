from pathlib import Path

from app.handlers.group_action_aliases import (
    ADMIN_INFO_RANKS,
    BOT_INFO_ALIASES,
    GROUP_STATS_ALIASES,
    LOOKUP_ALIASES,
    SELF_PROFILE_ALIASES,
)


def test_personal_and_group_stats_aliases_are_separate() -> None:
    assert "кто я" in SELF_PROFILE_ALIASES
    assert "моя стата" in SELF_PROFILE_ALIASES
    assert "стата" not in SELF_PROFILE_ALIASES
    assert "стата" in GROUP_STATS_ALIASES
    assert "статистика" in GROUP_STATS_ALIASES
    assert {"топ 10", "топ 20", "топ 30"} <= GROUP_STATS_ALIASES


def test_reply_lookup_phrases_are_contextual() -> None:
    assert {"кто ты", "ты кто", "инфа"} <= LOOKUP_ALIASES
    assert {"кто ты", "ты кто"} <= BOT_INFO_ALIASES


def test_aggregate_information_is_limited_to_real_admin_roles() -> None:
    assert {"owner", "service_owner", "deputy_owner", "chief_admin", "chat_admin", "voice_admin"} == ADMIN_INFO_RANKS
    assert "helper" not in ADMIN_INFO_RANKS
    assert "major" not in ADMIN_INFO_RANKS
    assert "untouchable" not in ADMIN_INFO_RANKS


def test_sensitive_aliases_are_guarded_by_live_rank_access() -> None:
    source = Path("app/middlewares_rank_safety.py").read_text(encoding="utf-8")
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert "class SensitiveGroupAliasAccessMiddleware" in source
    assert "get_actor_rank_with_access(bot, session, group, user.id)" in source
    assert "actor.code not in group_action_aliases.ADMIN_INFO_RANKS" in source
    assert "group_action_aliases.router.message.middleware(sensitive_alias_access_middleware)" in main
    assert "group_action_aliases.GROUP_STATS_ALIASES" in source
    assert "group_action_aliases.ALL_BANS_ALIASES" in source
    assert "group_action_aliases.ALL_MUTES_ALIASES" in source
    assert "group_action_aliases.ALL_WARNINGS_ALIASES" in source
    assert "group_action_aliases.SELF_PROFILE_ALIASES" not in source
    assert "group_action_aliases.REPORT_ALIASES" not in source


def test_activity_counter_rejects_edited_messages() -> None:
    source = Path("app/middlewares.py").read_text(encoding="utf-8")
    assert "event.edit_date is not None" in source
    assert "index_elements=[DailyStat.group_id, DailyStat.user_telegram_id, DailyStat.date]" in source


def test_alias_handler_uses_injected_bot_for_complaints() -> None:
    source = Path("app/handlers/group_action_aliases.py").read_text(encoding="utf-8")
    assert "async def readable_group_actions" in source
    assert "message: Message" in source
    assert "bot: Bot" in source
    assert "session: AsyncSession" in source
    assert "await group_complaint(message, bot, session)" in source
    assert "message.bot" not in source
