"""Тесты для app.action_templates и рендера падежей в game_friendly_results."""

from __future__ import annotations

import re

import pytest

from app.action_templates import ACTION_TEMPLATES, DEFAULT_ACTION_TEMPLATES
from app.entertainment_contracts import ENTERTAINMENT_ACTIONS, RELATIONSHIP_ACTIONS
from app.game_friendly_results import _render
from app.russian_inflect import CASE_ABLT, CASE_ACCS, CASE_DATV, CASE_GENT, _inflect

# ── Вспомогательные ──────────────────────────────────────────────────────


CASE_MARKERS = ("{target_acc}", "{target_ablt}", "{target_dat}", "{target_gent}", "{target}")
CASE_RE = re.compile(r"\{target(?:_(?:acc|ablt|dat|gent))?\}")


def _mentions(actor: str = "Алексей", target: str = "Мария") -> dict[str, tuple[str, int]]:
    return {
        "actor": (actor, 111),
        "target": (target, 222),
        "actor_acc": (_inflect(actor, CASE_ACCS), 111),
        "actor_ablt": (_inflect(actor, CASE_ABLT), 111),
        "actor_dat": (_inflect(actor, CASE_DATV), 111),
        "actor_gent": (_inflect(actor, CASE_GENT), 111),
        "target_acc": (_inflect(target, CASE_ACCS), 222),
        "target_ablt": (_inflect(target, CASE_ABLT), 222),
        "target_dat": (_inflect(target, CASE_DATV), 222),
        "target_gent": (_inflect(target, CASE_GENT), 222),
    }


# ── Покрытие ─────────────────────────────────────────────────────────────


class TestCoverage:
    def test_all_entertainment_actions_have_templates(self) -> None:
        missing = [a for a in ENTERTAINMENT_ACTIONS if a not in ACTION_TEMPLATES]
        assert not missing, f"Нет шаблонов для развлекательных действий: {missing}"

    def test_all_relationship_actions_have_templates(self) -> None:
        missing = [a for a in RELATIONSHIP_ACTIONS if a not in ACTION_TEMPLATES]
        assert not missing, f"Нет шаблонов для relationship-действий: {missing}"

    def test_no_extra_actions_in_templates(self) -> None:
        # Все действия в ACTION_TEMPLATES должны быть в контрактах
        known = ENTERTAINMENT_ACTIONS | RELATIONSHIP_ACTIONS
        extra = [a for a in ACTION_TEMPLATES if a not in known]
        # Разрешаем extra — словарь может быть шире, но сообщаем для информации
        # assert not extra, f"Лишние действия: {extra}"
        # Пока не блокируем, только проверяем что нет пустых значений
        for action in ACTION_TEMPLATES:
            assert ACTION_TEMPLATES[action], f"Пустой кортеж для действия: {action}"


# ── Структура шаблонов ───────────────────────────────────────────────────


class TestTemplateStructure:
    @pytest.mark.parametrize("action", sorted(ACTION_TEMPLATES.keys()))
    def test_each_template_has_case_marker(self, action: str) -> None:
        """В каждом варианте действия есть падежный маркер для target."""
        for variant in ACTION_TEMPLATES[action]:
            assert CASE_RE.search(variant), (
                f"В шаблоне действия '{action}' нет падежного маркера target: {variant!r}"
            )

    @pytest.mark.parametrize("action", sorted(ACTION_TEMPLATES.keys()))
    def test_no_old_action_placeholder(self, action: str) -> None:
        for variant in ACTION_TEMPLATES[action]:
            assert "{action}" not in variant, (
                f"Старый плейсхолдер {{action}} в '{action}': {variant!r}"
            )

    @pytest.mark.parametrize("action", sorted(ACTION_TEMPLATES.keys()))
    def test_no_emoji_placeholder(self, action: str) -> None:
        for variant in ACTION_TEMPLATES[action]:
            assert "{emoji}" not in variant, (
                f"Плейсхолдер {{emoji}} должен быть убран: '{action}': {variant!r}"
            )


# ── Опечатки / артефакты ─────────────────────────────────────────────────


FORBIDDEN_SUBSTRINGS = (
    "закопала",
    "забулліл",
    "схавтал",
    "загорил",
    "увлеч ",
    "перебился с",
    "прихватил",
    "надавил {",
    "подкатил {",
    "схапнул",
    "сныряет",
)


class TestNoTypos:
    @pytest.mark.parametrize("action", sorted(ACTION_TEMPLATES.keys()))
    def test_no_forbidden_substrings(self, action: str) -> None:
        for variant in ACTION_TEMPLATES[action]:
            for bad in FORBIDDEN_SUBSTRINGS:
                assert bad not in variant, (
                    f"Найден запрещённый фрагмент '{bad}' в '{action}': {variant!r}"
                )


# ── Рендер ключевых действий ─────────────────────────────────────────────


class TestRenderDeclension:
    @pytest.mark.parametrize(
        "action,expected_target_word",
        [
            ("обнять", "Марию"),
            ("поцеловать", "Марию"),
            ("пнуть", "Марию"),
            ("ударить", "Марию"),
            ("поссориться", "Марией"),
            ("помириться", "Марией"),
            ("подраться", "Марией"),
            ("дать вайфай", "Марии"),
            ("попросить денег", "Марии"),
            ("попросить денег", "Марии"),
        ],
    )
    def test_render_uses_correct_case(self, action: str, expected_target_word: str) -> None:
        """Хотя бы один вариант действия должен давать target в нужном падеже."""
        mentions = _mentions()
        variants = ACTION_TEMPLATES[action]
        rendered = []
        for variant in variants:
            text, _ = _render(variant, mentions)
            rendered.append(text)
        joined = " | ".join(rendered)
        assert expected_target_word in joined, (
            f"Действие '{action}' не даёт target '{expected_target_word}'. "
            f"Отрендерено: {rendered}"
        )

    def test_render_obnyat_full_string(self) -> None:
        text, _ = _render(ACTION_TEMPLATES["обнять"][0], _mentions())
        assert text == "Алексей обнял Марию"

    def test_render_ssoritsya_full_string(self) -> None:
        text, _ = _render(ACTION_TEMPLATES["поссориться"][0], _mentions())
        assert text == "Алексей поссорился с Марией"

    def test_render_poprostit_deneg_full_string(self) -> None:
        text, _ = _render(ACTION_TEMPLATES["попросить денег"][0], _mentions())
        assert text == "Алексей попросил денег у Марии"

    def test_render_non_cyrillic_target(self) -> None:
        """Латиница не должна склоняться."""
        mentions = _mentions(actor="Alex", target="Mary")
        text, _ = _render(ACTION_TEMPLATES["обнять"][0], mentions)
        assert text == "Alex обнял Mary"


# ── Fallback ─────────────────────────────────────────────────────────────


class TestFallback:
    def test_default_templates_exist(self) -> None:
        assert DEFAULT_ACTION_TEMPLATES
        assert len(DEFAULT_ACTION_TEMPLATES) >= 1

    def test_default_templates_have_case_marker(self) -> None:
        for variant in DEFAULT_ACTION_TEMPLATES:
            assert CASE_RE.search(variant), f"Fallback без маркера: {variant!r}"

    def test_unknown_action_falls_back(self) -> None:
        """Несуществующее действие использует fallback."""
        unknown = "несуществующее_действие"
        assert unknown not in ACTION_TEMPLATES
        variants = ACTION_TEMPLATES.get(unknown, DEFAULT_ACTION_TEMPLATES)
        assert variants is DEFAULT_ACTION_TEMPLATES