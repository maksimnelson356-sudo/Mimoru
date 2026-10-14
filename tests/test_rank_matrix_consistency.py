"""Rank matrix consistency: the drift class that a new rank silently reintroduces.

Adding a rank touched five hardcoded places by hand (ADMIN_RANKS,
two info-rank sets, the help text, the roles hierarchy line). Nothing
checked that these stay in sync, so the next rank would reintroduce the
same drift. These tests make the matrix self-verifying.
"""

from pathlib import Path

from app.services.ranks import (
    ADMIN_RANKS,
    CHAT_ADMIN,
    CHAT_VOICE_ADMIN,
    CHIEF_ADMIN,
    DEPUTY_OWNER,
    HELPER,
    MAJOR,
    PERMISSION_LABELS,
    RANK_CODES,
    RANK_LABELS,
    RANK_LEVELS,
    ROLE_CEILINGS,
    UNTOUCHABLE,
    VOICE_ADMIN,
    telegram_rights_for_rank,
)


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_every_rank_is_declared_in_every_registry() -> None:
    assert set(RANK_CODES) == set(RANK_LABELS) == set(RANK_LEVELS)
    assert set(RANK_CODES) == set(ROLE_CEILINGS)
    assert len(RANK_CODES) == len(set(RANK_CODES))


def test_rank_codes_fit_the_database_column() -> None:
    model = _source("app/db/rank_models.py")
    assert 'rank_code: Mapped[str] = mapped_column(String(32), index=True)' in model
    for code in RANK_CODES:
        assert len(code) <= 32


def test_every_ceiling_permission_is_a_known_permission() -> None:
    unknown = {
        rank: sorted(ceiling - set(PERMISSION_LABELS))
        for rank, ceiling in ROLE_CEILINGS.items()
        if ceiling - set(PERMISSION_LABELS)
    }
    assert unknown == {}


def test_every_permission_is_reachable_by_at_least_one_rank() -> None:
    reachable = set().union(*ROLE_CEILINGS.values())
    assert set(PERMISSION_LABELS) == reachable


def test_chat_voice_admin_is_consistent_in_every_handwritten_registry() -> None:
    # Два захардкоженных набора, которые обязаны знать о новом ранге.
    stats = _source("app/handlers/group_stats_v2.py")
    assert "chat_voice_admin" in stats

    aliases = _source("app/handlers/group_action_aliases.py")
    info_ranks = aliases.split("ADMIN_INFO_RANKS = {", 1)[1].split("}", 1)[0]
    assert '"chat_voice_admin"' in info_ranks

    # Наборы не должны расходиться между собой.
    stats_ranks = stats.split("ADMIN_RANKS = {", 1)[1].split("}", 1)[0]
    assert '"chat_voice_admin"' in stats_ranks


def test_chat_voice_admin_cannot_outrank_the_ranks_above_it() -> None:
    assert RANK_LEVELS[CHAT_ADMIN] > RANK_LEVELS[CHAT_VOICE_ADMIN]
    assert RANK_LEVELS[CHAT_VOICE_ADMIN] > RANK_LEVELS[VOICE_ADMIN]
    # Права, которые позволяют обойти иерархию, ранг выше получить не может.
    for forbidden in ("ban", "unban", "mute", "kick", "promote_admins"):
        assert forbidden not in ROLE_CEILINGS[CHAT_VOICE_ADMIN]


def test_telegram_rights_of_real_admins_match_their_rank_purpose() -> None:
    voice_chat_admins = {
        rank for rank in ADMIN_RANKS
        if telegram_rights_for_rank(rank)["can_manage_video_chats"]
    }
    assert voice_chat_admins == {DEPUTY_OWNER, CHIEF_ADMIN, CHAT_VOICE_ADMIN}

    # Никто, кроме зама и главного админа, не назначает администраторов.
    promoters = {
        rank for rank in ADMIN_RANKS
        if telegram_rights_for_rank(rank)["can_promote_members"]
    }
    assert promoters == {DEPUTY_OWNER, CHIEF_ADMIN}


def test_internal_ranks_receive_no_telegram_privileges() -> None:
    for rank in (VOICE_ADMIN, HELPER, MAJOR, UNTOUCHABLE):
        if rank in ADMIN_RANKS:
            continue
        rights = telegram_rights_for_rank(rank)
        assert rights["can_delete_messages"] is False
        assert rights["can_restrict_members"] is False
        assert rights["can_manage_video_chats"] is False


def test_help_and_hierarchy_texts_mention_every_admin_rank() -> None:
    roles = _source("app/group_help_full.py").split("def _roles_text", 1)[1]
    for rank in ADMIN_RANKS:
        assert RANK_LABELS[rank] in roles

    # Строка иерархии собрана из нескольких строковых литералов, поэтому берём
    # окно от маркера до закрывающей скобки, а не режем текст по кавычкам.
    roles_source = _source("app/handlers/telegram_roles.py")
    hierarchy = roles_source.split("Иерархия:", 1)[1].split("\n    )", 1)[0]
    # Мажор в иерархию админов не входит: это декоративный ранг, и его отсутствие
    # здесь намеренное, а не расхождение текста с каноническим лейблом.
    for rank in ADMIN_RANKS - {MAJOR}:
        assert RANK_LABELS[rank] in hierarchy
    # Старое сокращённое имя не должно возвращаться.
    assert "Администратор войса" not in roles_source