"""Тесты для #2: fallback названий групп в ОП.

Проверяют contract-стилем, что:
- напрямую clean_ui_text(group.title) нигде не вызывается вне хелперов;
- хелперы _group_title_or_placeholder и _resolve_group_title определены;
- в списках (без API) используется _group_title_or_placeholder;
- в деталях (с API) используется _resolve_group_title;
- покупателю не раскрывается telegram_chat_id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.handlers import ad_market_v3


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── Contract-тесты на структуру файла ────────────────────────────────────


class TestNoRawGroupTitleOutsideHelpers:
    """Ни один UI-вызов не должен напрямую тянуть clean_ui_text(group.title)."""

    def test_no_clean_ui_text_group_title(self) -> None:
        source = _source("app/handlers/ad_market_v3.py")
        # Ожидаем 0 вхождений
        assert "clean_ui_text(group.title)" not in source

    def test_no_clean_ui_text_seller_group_title(self) -> None:
        source = _source("app/handlers/ad_market_v3.py")
        assert "clean_ui_text(seller_group.title)" not in source


class TestHelpersDefined:
    def test_group_title_or_placeholder_defined(self) -> None:
        source = _source("app/handlers/ad_market_v3.py")
        assert "def _group_title_or_placeholder(" in source

    def test_resolve_group_title_defined(self) -> None:
        source = _source("app/handlers/ad_market_v3.py")
        assert "async def _resolve_group_title(" in source

    def test_placeholder_titles_constant(self) -> None:
        source = _source("app/handlers/ad_market_v3.py")
        assert "_PLACEHOLDER_TITLES" in source


class TestFastFallbackHelper:
    """_group_title_or_placeholder — БЕЗ вызова Telegram API."""

    def _block(self) -> str:
        source = _source("app/handlers/ad_market_v3.py")
        return source.split("def _group_title_or_placeholder", 1)[1].split(
            "async def _resolve_group_title", 1
        )[0]

    def test_has_for_buyer_param(self) -> None:
        assert "for_buyer" in self._block()

    def test_returns_private_group_for_buyer(self) -> None:
        assert "Приватная группа" in self._block()

    def test_returns_chat_id_for_seller(self) -> None:
        block = self._block()
        assert "Чат {group.telegram_chat_id}" in block or "f\"Чат {" in block

    def test_no_telegram_api_call(self) -> None:
        # В быстром fallback НЕ должно быть реального вызова await bot.get_chat
        # (упоминание в docstring — допустимо)
        assert "await bot.get_chat" not in self._block()


class TestResolverHelper:
    """_resolve_group_title — С вызовом Telegram API."""

    def _block(self) -> str:
        source = _source("app/handlers/ad_market_v3.py")
        return source.split("async def _resolve_group_title", 1)[1].split(
            "async def _listing_view", 1
        )[0]

    def test_calls_bot_get_chat(self) -> None:
        assert "bot.get_chat" in self._block()

    def test_caches_title_in_db(self) -> None:
        block = self._block()
        assert "group.title = chat.title" in block

    def test_handles_telegram_errors(self) -> None:
        block = self._block()
        assert "TelegramBadRequest" in block
        assert "TelegramForbiddenError" in block

    def test_supports_for_buyer(self) -> None:
        assert "for_buyer" in self._block()


class TestUsageAcrossHandlers:
    """Проверяет, что разные места используют правильный хелпер."""

    def test_required_sell_home_uses_placeholder(self) -> None:
        """Список групп продавца — без API."""
        source = _source("app/handlers/ad_market_v3.py")
        # required_sell_home не имеет декоратора @router.callback_query ниже,
        # поэтому split идёт до начала _render_seller_listing
        block = source.split("async def required_sell_home", 1)[1].split(
            "async def _render_seller_listing", 1
        )[0]
        assert "_group_title_or_placeholder" in block
        assert "await _resolve_group_title" not in block

    def test_render_seller_listing_uses_resolver(self) -> None:
        """Детали группы продавца — с API."""
        source = _source("app/handlers/ad_market_v3.py")
        block = source.split("async def _render_seller_listing", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "_resolve_group_title" in block

    def test_required_market_uses_placeholder_for_buyer(self) -> None:
        """Каталог покупателя (список) — без API, for_buyer=True."""
        source = _source("app/handlers/ad_market_v3.py")
        block = source.split("async def required_market(", 1)[1].split(
            "async def required_market_detail", 1
        )[0]
        assert "_group_title_or_placeholder" in block
        assert "for_buyer=True" in block
        assert "await _resolve_group_title" not in block

    def test_required_market_detail_uses_resolver_for_buyer(self) -> None:
        """Детали для покупателя — с API, for_buyer=True."""
        source = _source("app/handlers/ad_market_v3.py")
        block = source.split("async def required_market_detail", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "_resolve_group_title" in block
        assert "for_buyer=True" in block

    def test_required_deal_start_uses_placeholder(self) -> None:
        """Список групп покупателя в сделке — без API."""
        source = _source("app/handlers/ad_market_v3.py")
        block = source.split("async def required_deal_start", 1)[1].split(
            "@router.callback_query", 1
        )[0]
        assert "_group_title_or_placeholder" in block


# ── Юнит-тесты хелпера _group_title_or_placeholder ───────────────────────


class FakeGroup:
    """Заглушка Group для юнит-тестов."""

    def __init__(self, title: str, telegram_chat_id: int) -> None:
        self.title = title
        self.telegram_chat_id = telegram_chat_id


class TestGroupTitleOrPlaceholderUnit:
    def test_valid_title_returned_as_is(self) -> None:
        group = FakeGroup(title="Моя группа", telegram_chat_id=-1001234567890)
        result = ad_market_v3._group_title_or_placeholder(group)
        assert result == "Моя группа"

    @pytest.mark.parametrize("bad_title", ["", "   ", "unknown", "неизвестно", "группа", "ЧАТ", "none", "-"])
    def test_placeholder_title_returns_chat_id_for_seller(self, bad_title: str) -> None:
        group = FakeGroup(title=bad_title, telegram_chat_id=-1001234567890)
        result = ad_market_v3._group_title_or_placeholder(group, for_buyer=False)
        assert result == "Чат -1001234567890"

    @pytest.mark.parametrize("bad_title", ["", "unknown", "неизвестно", "-"])
    def test_placeholder_title_returns_private_for_buyer(self, bad_title: str) -> None:
        group = FakeGroup(title=bad_title, telegram_chat_id=-1001234567890)
        result = ad_market_v3._group_title_or_placeholder(group, for_buyer=True)
        assert result == "Приватная группа"
        # Покупателю НЕ раскрываем telegram_chat_id
        assert "-1001234567890" not in result

    def test_whitespace_around_valid_title_stripped(self) -> None:
        group = FakeGroup(title="  Моя группа  ", telegram_chat_id=1)
        result = ad_market_v3._group_title_or_placeholder(group)
        assert result == "Моя группа"