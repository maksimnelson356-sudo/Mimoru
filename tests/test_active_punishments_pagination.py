"""Постраничный список активных наказаний.

Проверяются обе части: обработчик (смещение, реальный итог, клампинг
страницы, совместимость старого callback) и клавиатура (кнопки
навигации, их отсутствие на краях).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import member_center
from app.keyboards import panel as panel_keyboards


def _rows(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(user_telegram_id=100 + i) for i in range(count)]


class _Rows:
    """Мини-результат session.scalars(): умеет отдавать .all()."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


def _buttons(markup) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


def _callback(data: str) -> SimpleNamespace:
    message = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=42),
        message=message,
        answer=AsyncMock(),
    )


async def _render(
    monkeypatch,
    *,
    data: str,
    total: int,
    stored: list[SimpleNamespace],
) -> tuple[SimpleNamespace, SimpleNamespace]:
    group = SimpleNamespace(id=3, telegram_chat_id=-1003, is_active=True)
    monkeypatch.setattr(member_center, "accessible_group", AsyncMock(return_value=group))
    # Обработчик строит клавиатуру сам, а та ходит в БД за именем участника.
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=_Rows(stored)),
        scalar=AsyncMock(return_value=total),
    )
    callback = _callback(data)
    await member_center.active_punishments(callback, session)
    return callback, session


# --- обработчик --------------------------------------------------------


@pytest.mark.asyncio
async def test_first_page_has_no_offset(monkeypatch) -> None:
    _, session = await _render(
        monkeypatch,
        data="active_punishments:3:ban",
        total=35,
        stored=_rows(10),
    )

    statement = session.scalars.await_args.args[0]
    assert statement._offset == 0
    assert statement._limit == member_center.ACTIVE_PUNISHMENTS_PAGE_SIZE


@pytest.mark.asyncio
async def test_legacy_callback_without_page_opens_first_page(monkeypatch) -> None:
    callback, session = await _render(
        monkeypatch,
        data="active_punishments:3:ban",
        total=35,
        stored=_rows(10),
    )

    assert session.scalars.await_args.args[0]._offset == 0
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_third_page_uses_matching_offset(monkeypatch) -> None:
    _, session = await _render(
        monkeypatch,
        data="active_punishments:3:mute:page:2",
        total=35,
        stored=_rows(10),
    )

    assert session.scalars.await_args.args[0]._offset == 2 * member_center.ACTIVE_PUNISHMENTS_PAGE_SIZE


@pytest.mark.asyncio
async def test_page_beyond_last_is_clamped(monkeypatch) -> None:
    callback, session = await _render(
        monkeypatch,
        data="active_punishments:3:warn:page:999",
        total=35,
        stored=_rows(5),
    )

    pages = 4  # 35 записей при PAGE_SIZE=10
    assert session.scalars.await_args.args[0]._offset == (pages - 1) * member_center.ACTIVE_PUNISHMENTS_PAGE_SIZE
    assert f"Страница {pages}/{pages}" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_counter_shows_true_total_not_page_length(monkeypatch) -> None:
    callback, _ = await _render(
        monkeypatch,
        data="active_punishments:3:ban",
        total=57,
        stored=_rows(10),
    )

    text = callback.message.edit_text.await_args.args[0]
    assert "Найдено: 57" in text
    assert "Найдено: 10" not in text


@pytest.mark.asyncio
async def test_single_page_omits_page_indicator(monkeypatch) -> None:
    callback, _ = await _render(
        monkeypatch,
        data="active_punishments:3:ban",
        total=4,
        stored=_rows(4),
    )

    text = callback.message.edit_text.await_args.args[0]
    assert "Найдено: 4" in text
    assert "Страница" not in text


@pytest.mark.asyncio
async def test_empty_list_renders_first_page(monkeypatch) -> None:
    callback, _ = await _render(
        monkeypatch,
        data="active_punishments:3:ban:page:3",
        total=0,
        stored=[],
    )

    text = callback.message.edit_text.await_args.args[0]
    assert "Найдено: 0" in text
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert _buttons(markup) == [("◀️ К участникам", "group_section:3:members")]


@pytest.mark.asyncio
async def test_access_denied_without_rendering(monkeypatch) -> None:
    monkeypatch.setattr(member_center, "accessible_group", AsyncMock(return_value=None))
    session = SimpleNamespace(scalars=AsyncMock(), scalar=AsyncMock())
    callback = _callback("active_punishments:3:ban")

    await member_center.active_punishments(callback, session)

    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()


# --- клавиатура --------------------------------------------------------


@pytest.mark.asyncio
async def test_middle_page_has_both_arrows(monkeypatch) -> None:
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    markup = await panel_keyboards.active_punishments_menu(
        3, "ban", _rows(10), page=1, pages=4
    )

    buttons = _buttons(markup)
    assert ("◀ Назад", "active_punishments:3:ban:page:0") in buttons
    assert ("Вперёд ▶", "active_punishments:3:ban:page:2") in buttons
    assert ("2/4", "noop") in buttons


@pytest.mark.asyncio
async def test_first_page_has_no_back_button(monkeypatch) -> None:
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    markup = await panel_keyboards.active_punishments_menu(
        3, "mute", _rows(10), page=0, pages=4
    )

    buttons = _buttons(markup)
    assert not any(text == "◀ Назад" for text, _ in buttons)
    assert ("Вперёд ▶", "active_punishments:3:mute:page:1") in buttons


@pytest.mark.asyncio
async def test_last_page_has_no_forward_button(monkeypatch) -> None:
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    markup = await panel_keyboards.active_punishments_menu(
        3, "warn", _rows(5), page=3, pages=4
    )

    buttons = _buttons(markup)
    assert not any(text == "Вперёд ▶" for text, _ in buttons)
    assert ("◀ Назад", "active_punishments:3:warn:page:2") in buttons


@pytest.mark.asyncio
async def test_single_page_has_no_navigation(monkeypatch) -> None:
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    markup = await panel_keyboards.active_punishments_menu(3, "ban", _rows(3))

    buttons = _buttons(markup)
    assert len(buttons) == 4  # 3 участника + возврат к участникам
    assert buttons[-1] == ("◀️ К участникам", "group_section:3:members")


@pytest.mark.asyncio
async def test_row_buttons_point_to_member_card(monkeypatch) -> None:
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    markup = await panel_keyboards.active_punishments_menu(
        3, "ban", _rows(2), page=0, pages=1
    )

    assert ("⛔ Участник", "member_card:3:100") in _buttons(markup)
    assert ("⛔ Участник", "member_card:3:101") in _buttons(markup)