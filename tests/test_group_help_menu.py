from pathlib import Path

from app import group_help_full
from app.handlers.group_action_aliases import (
    ADMIN_ROSTER_ALIASES,
    REPORT_ALIASES,
    SELF_PROFILE_ALIASES,
)


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_group_help_is_registered_before_legacy_group_help_handlers() -> None:
    main = _source("app/main.py")
    preferences = _source("app/handlers/fun_preferences.py")
    assert main.index("fun_preferences.router") < main.index("common.router")
    assert main.index("fun_preferences.router") < main.index("fun_help.router")
    assert "router.include_router(group_help_full.router)" in preferences


def test_public_help_entry_phrases_and_slash_commands_are_wired() -> None:
    source = _source("app/group_help_full.py")
    for phrase in ("помощь", "команды", "все команды", "админ команды", "все админ команды"):
        assert phrase in source
    assert 'Command("help")' in source
    assert 'Command("commands")' in source
    assert 'Command("comands")' in source
    assert "grouphelpfull:" in source


def test_help_pages_fit_telegram_message_limit() -> None:
    pages = (
        group_help_full._home_text(), group_help_full._members_text(), group_help_full._member_alias_text(),
        group_help_full._moderation_text(), group_help_full._admin_lists_text(), group_help_full._protection_text(),
        group_help_full._management_text(), group_help_full._joins_text(), group_help_full._content_text(),
        group_help_full._admin_extra_text(), group_help_full._roles_text(),
    )
    assert all(0 < len(page) < 4096 for page in pages)


def test_member_alias_help_is_sourced_from_live_alias_sets() -> None:
    text = group_help_full._member_alias_text()
    for phrase in SELF_PROFILE_ALIASES | REPORT_ALIASES | ADMIN_ROSTER_ALIASES:
        assert phrase in text
    for phrase in ("жалоба", "пожаловаться", "сдать нарушителя", "настучать наверх", "тут ситуация"):
        assert phrase in text


def test_help_keeps_core_admin_workflows_visible() -> None:
    moderation = group_help_full._moderation_text()
    protection = group_help_full._protection_text()
    management = group_help_full._management_text()
    joins = group_help_full._joins_text()
    content = group_help_full._content_text()
    for phrase in ("пред", "мут 10м", "бан", "инфо", "история"):
        assert phrase in moderation
    for phrase in ("антифлуд", "антирейд", "карантин", "массовый спам", "упоминания"):
        assert phrase in protection
    for phrase in ("локдаун", "ночной режим", "запланировать", "журнал", "заметка"):
        assert phrase in management
    for phrase in ("создать ссылку", "заявки авто", "одобрить заявку"):
        assert phrase in joins
    for phrase in ("изменить правила", "добавить триггер", "добавить подписку", "разрешить ссылку"):
        assert phrase in content


def test_games_slash_command_is_reserved_for_real_games() -> None:
    preferences = _source("app/handlers/fun_preferences.py")
    game_handlers = _source("app/games/handlers.py")
    help_source = _source("app/handlers/fun_help.py")
    assert 'Command("games")' in game_handlers
    assert 'Command("games")' not in preferences
    assert "router.include_router(game_handlers.router)" in preferences
    assert "fun_help.entertainment_help(message)" not in game_handlers
    assert 'OPEN_WORDS = {"развлечения", "развлекательные команды"}' in help_source
