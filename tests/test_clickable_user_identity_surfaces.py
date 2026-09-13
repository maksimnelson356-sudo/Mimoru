from pathlib import Path
from types import SimpleNamespace

import pytest

from app.game_friendly_results import _tg_name
from app.services.public_identity import public_user_token
from app.services.user_refs import user_label


ROOT = Path(__file__).resolve().parents[1]


def test_game_mentions_prefer_telegram_display_name_over_username() -> None:
    user = SimpleNamespace(full_name="Иван Петров", username="ivan_petrov")
    assert _tg_name(user) == "Иван Петров"


def test_game_mentions_fall_back_to_username_only_without_display_name() -> None:
    user = SimpleNamespace(full_name="", username="ivan_petrov")
    assert _tg_name(user) == "@ivan_petrov"


@pytest.mark.asyncio
async def test_shared_user_label_is_internal_clickable_identity_token() -> None:
    assert await user_label(None, 123456789) == public_user_token(123456789)  # type: ignore[arg-type]


def test_member_and_group_surfaces_use_central_identity_tokens() -> None:
    profile = (ROOT / "app/handlers/member_profile_v2.py").read_text(encoding="utf-8")
    aliases = (ROOT / "app/handlers/group_action_aliases.py").read_text(encoding="utf-8")
    commands = (ROOT / "app/handlers/group_commands.py").read_text(encoding="utf-8")

    assert "public_user_token(user_id)" in profile
    assert "entities=await _name_entity" not in profile
    assert "return public_user_token(user_id)" in aliases
    assert "Кто пожаловался: {public_user_token(reporter_id)}" in commands
    assert "На кого: {public_user_token(target_id)}" in commands
    assert "Объект: Сообщение №{message_id}" in commands


def test_user_ids_stay_in_callback_and_storage_but_not_visible_labels() -> None:
    commands = (ROOT / "app/handlers/group_commands.py").read_text(encoding="utf-8")
    assert "target_name=public_user_token(target_id)" in commands
    assert "moderator_name=public_user_token(message.from_user.id)" in commands
    # black разбил return на многострочный — проверяем аргументы
    assert "target_id, public_user_token(target_id)" in commands
