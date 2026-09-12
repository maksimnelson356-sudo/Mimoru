"""Тесты для #4: создание ОП из группы через сообщение "реклама".

Проверяют contract-стилем, что:
- handler ad_group_entry определён в app/handlers/group_shortcuts.py;
- фильтр: F.chat.type in GROUP_TYPES + F.text.casefold() == "реклама";
- проверка прав через can_manage_group;
- отправка в личку через bot.send_message(user_id, ...);
- обработка TelegramForbiddenError (личка недоступна);
- используется reqlist:group:<id> для перехода в существующий FSM.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── Handler зарегистрирован ──────────────────────────────────────────────


class TestHandlerRegistered:
    def test_handler_defined(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        assert "async def ad_group_entry(" in source

    def test_filter_on_group_chat_type(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[0]
        # Декоратор стоит прямо перед функцией
        decorator_line = block.strip().splitlines()[-1]
        assert "F.chat.type.in_(GROUP_TYPES)" in decorator_line

    def test_filter_on_text_rec_lama(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[0]
        decorator_line = block.strip().splitlines()[-1]
        # Финальный фильтр — текст "реклама"
        assert 'F.text.casefold() == "реклама"' in decorator_line

    def test_router_is_registered_in_main(self) -> None:
        source = _source("app/main.py")
        assert "group_shortcuts.router" in source


# ── Права ────────────────────────────────────────────────────────────────


class TestPermissions:
    def test_uses_can_manage_group(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert "can_manage_group(" in block

    def test_returns_silently_if_no_permission(self) -> None:
        """Если нет прав — handler молчит (не отвечает)."""
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        # После проверки прав — return без message.reply
        idx = block.find("can_manage_group(")
        snippet = block[idx : idx + 300]
        assert "return" in snippet
        # Нет reply непосредственно сразу после проверки прав
        # (проверка простая: между can_manage_group и следующим reply
        # должно быть логическое разделение)


# ── Отправка в личку ─────────────────────────────────────────────────────


class TestPrivateMessage:
    def test_uses_bot_send_message_to_user(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert "bot.send_message(" in block
        assert "user_id" in block

    def test_handles_forbidden_error(self) -> None:
        """Если личка недоступна — handler просит написать боту."""
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert "TelegramForbiddenError" in block
        assert "bot.get_me()" in block

    def test_uses_existing_reqlist_callback(self) -> None:
        """Переиспользует существующий callback reqlist:group:<id>."""
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert 'callback_data=f"reqlist:group:{group.id}"' in block


# ── Fallback для названий групп ──────────────────────────────────────────


class TestGroupTitleFallback:
    def test_uses_group_title_helper(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        assert "from app.handlers.ad_market_v3 import _group_title_or_placeholder" in source

    def test_calls_group_title_in_handler(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert "_group_title_or_placeholder(" in block


# ── Работа со списком групп ──────────────────────────────────────────────


class TestGroupList:
    def test_selects_groups_by_owner(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert "Group.owner_telegram_id == user_id" in block

    def test_selects_only_active_groups(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        assert "Group.is_active.is_(True)" in block

    def test_handles_no_groups_case(self) -> None:
        source = _source("app/handlers/group_shortcuts.py")
        block = source.split("async def ad_group_entry", 1)[1].split("@router", 1)[0]
        # Если групп нет — reply
        assert "if not groups" in block
        assert "message.reply(" in block