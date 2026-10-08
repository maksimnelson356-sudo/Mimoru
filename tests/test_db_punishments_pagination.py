"""Пагинация активных наказаний против настоящего PostgreSQL.

Юнит-тесты ``test_active_punishments_pagination.py`` подменяют сессию,
поэтому проверяют только то, что обработник правильно посчитал смещение
и пределы. Сам SQL - ``ORDER BY created_at DESC, id DESC`` с ``OFFSET`` и
``LIMIT`` плюс отдельный ``count(*)`` - до сих пор ни разу не был выполнен
PostgreSQL.

Такая проверка нужна в первую очередь ради разбиения на страницы: без
второго ключа сортировки строки с одинаковым ``created_at`` могут
перескакивать между страницами, и пользователь увидит одну запись дважды
и потеряет другую. Здесь у всех строк намеренно одинаковое время.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.db.models import Group, GroupSettings, Punishment
from app.handlers import member_center
from app.keyboards import panel as panel_keyboards

PAGE_SIZE = member_center.ACTIVE_PUNISHMENTS_PAGE_SIZE
TOTAL = 25


@pytest.fixture
async def group_with_bans(db_session_factory):
    """Группа с TOTAL активными банами, у всех один и тот же created_at."""
    suffix = secrets.randbelow(1_000_000_000)
    async with db_session_factory() as session:
        group = Group(
            telegram_chat_id=-2_000_000_000 - suffix,
            title=f"db-paging-{suffix}",
            owner_telegram_id=2_000_000_000 + suffix,
            settings=GroupSettings(),
        )
        session.add(group)
        await session.commit()
        group_id = group.id

    same_moment = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    async with db_session_factory() as session:
        session.add_all(
            [
                Punishment(
                    group_id=group_id,
                    user_telegram_id=3_000_000_000 + suffix + index,
                    moderator_telegram_id=2_000_000_000 + suffix,
                    kind="ban",
                    reason=f"ban-{index}",
                    active=True,
                    created_at=same_moment,
                )
                for index in range(TOTAL)
            ]
        )
        await session.commit()

    yield group_id

    async with db_session_factory() as session:
        await session.execute(delete(Punishment).where(Punishment.group_id == group_id))
        await session.execute(delete(Group).where(Group.id == group_id))
        await session.commit()


async def _page_ids(
    monkeypatch, db_session_factory, group_id: int, page: int
) -> tuple[list[int], str]:
    """Возвращает id участников, показанных на странице, и текст заголовка."""
    group = await _load_group(db_session_factory, group_id)
    monkeypatch.setattr(member_center, "accessible_group", AsyncMock(return_value=group))
    monkeypatch.setattr(
        panel_keyboards, "stored_visible_name", AsyncMock(return_value="Участник")
    )

    suffix = "" if page == 0 else f":page:{page}"
    callback = SimpleNamespace(
        data=f"active_punishments:{group_id}:ban{suffix}",
        from_user=SimpleNamespace(id=2_000_000_000),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    async with db_session_factory() as session:
        await member_center.active_punishments(callback, session)

    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    ids = [
        int(data.split(":")[2])
        for _text, data in (
            (button.text, button.callback_data)
            for row in markup.inline_keyboard
            for button in row
        )
        if data.startswith("member_card:")
    ]
    return ids, callback.message.edit_text.await_args.args[0]


async def _load_group(db_session_factory, group_id: int) -> Group:
    async with db_session_factory() as session:
        return await session.get(Group, group_id)


@pytest.mark.db
async def test_pages_partition_every_ban_exactly_once(
    monkeypatch, db_session_factory, group_with_bans
) -> None:
    """Страницы не пересекаются и вместе покрывают все записи."""
    pages = (TOTAL + PAGE_SIZE - 1) // PAGE_SIZE
    seen: list[int] = []
    for page in range(pages):
        ids, _text = await _page_ids(monkeypatch, db_session_factory, group_with_bans, page)
        expected = min(PAGE_SIZE, TOTAL - page * PAGE_SIZE)
        assert len(ids) == expected, f"страница {page}: ожидалось {expected}, получено {len(ids)}"
        seen.extend(ids)

    assert len(seen) == TOTAL
    assert len(set(seen)) == TOTAL, "запись повторилась на разных страницах"

    async with db_session_factory() as session:
        stored = (
            await session.scalars(
                select(Punishment.user_telegram_id).where(Punishment.group_id == group_with_bans)
            )
        ).all()
    assert set(seen) == set(stored)


@pytest.mark.db
async def test_paging_is_stable_when_timestamps_tie(
    monkeypatch, db_session_factory, group_with_bans
) -> None:
    """Одинаковый created_at не должен перемешивать страницы."""
    first_pass = [
        (await _page_ids(monkeypatch, db_session_factory, group_with_bans, page))[0]
        for page in range(3)
    ]
    second_pass = [
        (await _page_ids(monkeypatch, db_session_factory, group_with_bans, page))[0]
        for page in range(3)
    ]
    assert first_pass == second_pass


@pytest.mark.db
async def test_header_reports_true_total_not_page_length(
    monkeypatch, db_session_factory, group_with_bans
) -> None:
    _ids, text = await _page_ids(monkeypatch, db_session_factory, group_with_bans, 0)
    assert f"Найдено: {TOTAL}" in text


@pytest.mark.db
async def test_page_beyond_last_is_clamped_to_last_page(
    monkeypatch, db_session_factory, group_with_bans
) -> None:
    last_page = (TOTAL + PAGE_SIZE - 1) // PAGE_SIZE - 1
    ids, text = await _page_ids(monkeypatch, db_session_factory, group_with_bans, 999)
    assert f"Страница {last_page + 1}/{last_page + 1}" in text
    assert ids == (await _page_ids(monkeypatch, db_session_factory, group_with_bans, last_page))[0]


@pytest.mark.db
async def test_inactive_bans_are_excluded_from_the_total(
    monkeypatch, db_session_factory, group_with_bans
) -> None:
    """Счётчик обязан совпадать с выборкой, а не показывать все строки."""
    async with db_session_factory() as session:
        row = await session.scalar(
            select(Punishment).where(Punishment.group_id == group_with_bans).limit(1)
        )
        assert row is not None
        row.active = False
        await session.commit()

    _ids, text = await _page_ids(monkeypatch, db_session_factory, group_with_bans, 0)
    assert f"Найдено: {TOTAL - 1}" in text