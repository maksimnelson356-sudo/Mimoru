"""Проверка миграции, сворачивающей дубликаты жалоб.

``check_db.sh`` применяет цепочку миграций к пустой базе, поэтому ветка с
данными - удаление уже существующих дублей перед созданием уникального
ограничения - там не исполняется: DELETE не находит ничего, а
создание индекса проходит тривиально. Реально эта ветка отработала
только на проде, где данные уже были.

Здесь SQL берётся из исходника самой миграции, а не копируется в тест,
чтобы проверка не могла разойтись с тем, что реально выполнится.
Форма структур воспроизводится во временной таблице: временная таблица
``complaints`` перекрывает одноимённую постоянную в пределах сессии,
поэтому DELETE затрагивает только её. Количество строк в настоящей
таблице сравнивается до и после - тест не имеет права её менять.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "c7f4a1e2b9d3_complaints_unique_per_message.py"
CONSTRAINT_NAME = "uq_complaints_group_message"


def _migration_tree() -> ast.Module:
    return ast.parse(MIGRATION.read_text(encoding="utf-8"))


def _dedupe_sql() -> str:
    """Достаёт текст DELETE из вызова op.execute(sa.text(...))."""
    for node in ast.walk(_migration_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        wrapper = node.args[0]
        if isinstance(wrapper, ast.Call) and wrapper.args:
            payload = wrapper.args[0]
            if isinstance(payload, ast.Constant) and isinstance(payload.value, str):
                return payload.value
    raise AssertionError("миграция не содержит op.execute(sa.text(...))")


def _create_unique_constraint_args() -> tuple[str, str, list[str]] | None:
    for node in ast.walk(_migration_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "create_unique_constraint" or len(node.args) < 3:
            continue
        name, table, columns = node.args[0], node.args[1], node.args[2]
        if not isinstance(columns, (ast.List, ast.Tuple)):
            continue
        values = [
            element.value
            for element in columns.elts
            if isinstance(element, ast.Constant)
        ]
        if isinstance(name, ast.Constant) and isinstance(table, ast.Constant):
            return str(name.value), str(table.value), [str(v) for v in values]
    return None


# --- проверки без базы (быстрый гейт) ----------------------------------


def test_migration_creates_the_expected_unique_constraint() -> None:
    assert _create_unique_constraint_args() == (
        CONSTRAINT_NAME,
        "complaints",
        ["group_id", "message_id"],
    )


def test_dedupe_statement_is_extracted() -> None:
    sql = _dedupe_sql().casefold()
    assert "delete from complaints" in sql
    assert "group_id" in sql
    assert "message_id" in sql


# --- проверки против живого PostgreSQL ---------------------------------


@pytest.mark.db
async def test_dedupe_keeps_lowest_id_per_message(db_engine) -> None:
    """Дубли по одному сообщению схлопываются до самой ранней жалобы."""
    async with db_engine.connect() as connection:
        untouched = await connection.scalar(text("SELECT count(*) FROM complaints"))

        await connection.execute(
            text("CREATE TEMP TABLE complaints (id int primary key, group_id int, message_id bigint)")
        )
        # Два дубля для сообщения 555 в группе 3, дубль для 556 и разные группы.
        await connection.execute(
            text(
                """
                INSERT INTO complaints (id, group_id, message_id) VALUES
                    (10, 3, 555),
                    (11, 3, 555),
                    (12, 3, 556),
                    (13, 3, 556),
                    (14, 4, 555),
                    (15, 4, 555),
                    (16, 4, 556)
                """
            )
        )

        await connection.execute(text(_dedupe_sql()))
        rows = (
            (await connection.execute(text("SELECT id, group_id, message_id FROM complaints ORDER BY id")))
            .all()
        )

        # Первая жалоба на каждое сообщение в каждой группе выживает.
        assert [(int(r[0]), int(r[1]), int(r[2])) for r in rows] == [
            (10, 3, 555),
            (12, 3, 556),
            (14, 4, 555),
            (16, 4, 556),
        ]

        await connection.rollback()

    after = None
    async with db_engine.connect() as connection:
        after = await connection.scalar(text("SELECT count(*) FROM complaints"))
    assert after == untouched, "временная таблица не должна была задеть настоящую"


@pytest.mark.db
async def test_unique_constraint_applies_after_dedupe(db_engine) -> None:
    """После схлопывания ограничение действительно создаётся."""
    async with db_engine.connect() as connection:
        await connection.execute(
            text("CREATE TEMP TABLE complaints (id int primary key, group_id int, message_id bigint)")
        )
        await connection.execute(
            text(
                """
                INSERT INTO complaints (id, group_id, message_id) VALUES
                    (20, 3, 555), (21, 3, 555), (22, 3, 556)
                """
            )
        )
        await connection.execute(text(_dedupe_sql()))
        await connection.execute(
            text(
                f"ALTER TABLE complaints ADD CONSTRAINT {CONSTRAINT_NAME} "
                "UNIQUE (group_id, message_id)"
            )
        )

        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO complaints (id, group_id, message_id) "
                    "VALUES (23, 3, 556)"
                )
            )
        await connection.rollback()