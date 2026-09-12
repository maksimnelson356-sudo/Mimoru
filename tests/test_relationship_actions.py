"""Тесты для #3c: relationship actions с подтверждением.

Проверяют contract-стилем, что:
- 4 slug-маппинга для RELATIONSHIP_ACTIONS определены;
- friendly_fun_action БОЛЬШЕ не обрабатывает RELATIONSHIP_ACTIONS;
- friendly_relationship_action обрабатывает RELATIONSHIP_ACTIONS;
- новый handler отправляет prompt с inline-кнопками fsrel:...:yes/no;
- callback friendly_relationship_answer проверяет ownership (target only);
- есть TTL — 5 минут;
- при yes — рендерится финальный ACTION_TEMPLATES;
- при no — сообщение "отклонено".
"""

from __future__ import annotations

from pathlib import Path

from app.entertainment_contracts import ENTERTAINMENT_ACTIONS, RELATIONSHIP_ACTIONS
from app.game_friendly_results import (
    RELATIONSHIP_ACTION_LABELS,
    RELATIONSHIP_ACTION_SLUGS,
    RELATIONSHIP_CONFIRM_TTL_SECONDS,
    RELATIONSHIP_SLUG_TO_ACTION,
    _relationship_markup,
)


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── Константы ────────────────────────────────────────────────────────────


class TestSlugMapping:
    def test_all_relationship_actions_have_slugs(self) -> None:
        missing = [a for a in RELATIONSHIP_ACTIONS if a not in RELATIONSHIP_ACTION_SLUGS]
        assert not missing, f"Нет slug для: {missing}"

    def test_slugs_are_ascii(self) -> None:
        for slug in RELATIONSHIP_ACTION_SLUGS.values():
            assert slug.isascii(), f"Slug должен быть ASCII: {slug!r}"

    def test_slug_to_action_is_inverse(self) -> None:
        for action, slug in RELATIONSHIP_ACTION_SLUGS.items():
            assert RELATIONSHIP_SLUG_TO_ACTION[slug] == action

    def test_labels_exist_for_all_actions(self) -> None:
        for action in RELATIONSHIP_ACTIONS:
            assert action in RELATIONSHIP_ACTION_LABELS

    def test_ttl_is_5_minutes(self) -> None:
        assert RELATIONSHIP_CONFIRM_TTL_SECONDS == 5 * 60


# ── Разделение фильтров ──────────────────────────────────────────────────


class TestFilterSeparation:
    def test_friendly_fun_action_only_entertainment(self) -> None:
        source = _source("app/game_friendly_results.py")
        # Декоратор friendly_fun_action не должен включать RELATIONSHIP_ACTIONS
        block = source.split("async def friendly_fun_action", 1)[0]
        last_decorator = block.strip().splitlines()[-1]
        assert "RELATIONSHIP_ACTIONS" not in last_decorator
        assert "ENTERTAINMENT_ACTIONS" in last_decorator

    def test_relationship_handler_registered(self) -> None:
        source = _source("app/game_friendly_results.py")
        assert "async def friendly_relationship_action(" in source
        # Декоратор фильтрует по RELATIONSHIP_ACTIONS
        block = source.split("async def friendly_relationship_action", 1)[0]
        last_decorator = block.strip().splitlines()[-1]
        assert "RELATIONSHIP_ACTIONS" in last_decorator


# ── Markup ───────────────────────────────────────────────────────────────


class TestRelationshipMarkup:
    def test_markup_has_two_buttons(self) -> None:
        markup = _relationship_markup("posoritsya", 1, 2, 3)
        row = markup.inline_keyboard[0]
        assert len(row) == 2

    def test_markup_uses_yes_no_callbacks(self) -> None:
        markup = _relationship_markup("posoritsya", 1, 2, 3)
        row = markup.inline_keyboard[0]
        assert row[0].callback_data == "fsrel:posoritsya:1:2:3:yes"
        assert row[1].callback_data == "fsrel:posoritsya:1:2:3:no"


# ── Handler: callback ────────────────────────────────────────────────────


class TestCallbackHandler:
    def test_callback_regexp_present(self) -> None:
        source = _source("app/game_friendly_results.py")
        assert r"^fsrel:(posoritsya|porugatsya|podratsya|pomiritsya)" in source

    def test_ownership_check(self) -> None:
        """Только target может нажать."""
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_answer", 1)[1].split(
            "@router", 1
        )[0]
        assert "callback.from_user.id != target_id" in block
        assert "FOREIGN_BUTTON_NOTICE" in block

    def test_ttl_check_present(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_answer", 1)[1].split(
            "@router", 1
        )[0]
        assert "RELATIONSHIP_CONFIRM_TTL_SECONDS" in block
        assert "expired" in block

    def test_pending_event_lookup_with_for_update(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_answer", 1)[1].split(
            "@router", 1
        )[0]
        assert 'GameEvent.outcome == "pending"' in block
        assert ".with_for_update()" in block

    def test_yes_branch_renders_action_template(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_answer", 1)[1].split(
            "@router", 1
        )[0]
        assert "ACTION_TEMPLATES.get(action" in block
        assert 'event.outcome = "accepted"' in block

    def test_no_branch_rejects(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_answer", 1)[1].split(
            "@router", 1
        )[0]
        assert 'event.outcome = "rejected"' in block
        assert "Отклонено" in block

    def test_missing_event_answered(self) -> None:
        """Если события уже нет — пользователю сообщается."""
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_answer", 1)[1].split(
            "@router", 1
        )[0]
        assert "уже ответили" in block


# ── Handler: message ─────────────────────────────────────────────────────


class TestMessageHandler:
    def test_message_handler_creates_pending_event(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_action", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert 'event_type="relationship_action"' in block
        assert 'outcome="pending"' in block

    def test_message_handler_uses_cooldown(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_action", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "_check_cooldown" in block

    def test_message_handler_self_target_guard(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_action", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "actor.id == target.id" in block

    def test_message_handler_bot_target_guard(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_action", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "target.is_bot" in block

    def test_message_handler_replies_with_markup(self) -> None:
        source = _source("app/game_friendly_results.py")
        block = source.split("async def friendly_relationship_action", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "_relationship_markup(slug" in block
        assert "message.reply" in block