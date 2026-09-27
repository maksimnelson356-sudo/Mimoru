"""Live-database verification of the migration chain and resulting schema.

``scripts/check_migrations.py`` only walks the revision graph with ``ast`` and
``scripts/check_schema_consistency.py`` replays ``upgrade()`` against a fake
``op`` recorder that swallows every call except create/add/drop table/column.
Neither one proves that the SQL actually applies to PostgreSQL.

These tests run against a real database via ``scripts/check_db.sh``. They
deliberately read the expected head from the migration sources instead of
hard-coding it, so adding a new migration does not break the assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

# Register every ORM model, mirroring scripts/check_schema_consistency.py.
# Without these imports Base.metadata would be incomplete and the live/ORM
# comparison below would report migration tables as unknown.
import app.db.ad_market_models  # noqa: F401
import app.db.broadcast_models  # noqa: F401
import app.db.deleted_cleanup_retry_models  # noqa: F401
import app.db.fun_models  # noqa: F401
import app.db.game_models  # noqa: F401
import app.db.group_disconnect_models  # noqa: F401
import app.db.invite_operation_models  # noqa: F401
import app.db.models  # noqa: F401
import app.db.moderation_command_models  # noqa: F401
import app.db.moderation_operation_models  # noqa: F401
import app.db.payment_refund_models  # noqa: F401
import app.db.pending_bans  # noqa: F401
import app.db.permission_transition_models  # noqa: F401
import app.db.rank_models  # noqa: F401
import app.db.rank_provisioning_models  # noqa: F401
import app.db.required_reconcile_models  # noqa: F401
from app.db.base import Base

ROOT = Path(__file__).resolve().parents[1]
VERSION_NUM_WIDTH = 255


def _revisions() -> dict[str, str | None]:
    """Map every revision id to its down_revision by parsing the sources."""
    revisions: dict[str, str | None] = {}
    for path in sorted((ROOT / "alembic" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(node.value)
        revisions[str(values["revision"])] = values.get("down_revision")
    return revisions


def _expected_head() -> str:
    revisions = _revisions()
    heads = set(revisions)
    for parent in revisions.values():
        if parent:
            heads.discard(str(parent))
    assert len(heads) == 1, f"expected a single migration head, got {sorted(heads)}"
    return heads.pop()


def test_every_revision_id_fits_the_version_num_column() -> None:
    """A revision longer than the column would fail at deploy time."""
    too_long = sorted(rev for rev in _revisions() if len(rev) > VERSION_NUM_WIDTH)
    assert not too_long, f"revision ids exceed VARCHAR({VERSION_NUM_WIDTH}): {too_long}"


@pytest.mark.db
async def test_live_database_is_at_the_current_migration_head(db_engine) -> None:
    expected = _expected_head()
    async with db_engine.connect() as connection:
        rows = (
            (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            )
            .scalars()
            .all()
        )
    assert list(rows) == [expected]


@pytest.mark.db
async def test_alembic_version_column_is_wide_enough(db_engine) -> None:
    """Guards the scripts/ensure_version_column fix for long revision ids."""
    async with db_engine.connect() as connection:
        width = await connection.scalar(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'alembic_version' "
                "AND column_name = 'version_num'"
            )
        )
    assert width == VERSION_NUM_WIDTH


@pytest.mark.db
async def test_live_schema_matches_orm_metadata(db_engine) -> None:
    """Complements the fake-op recorder check with what PostgreSQL actually has."""
    async with db_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
        )
    live: dict[str, set[str]] = {}
    for table_name, column_name in result:
        live.setdefault(str(table_name), set()).add(str(column_name))

    # Alembic's bookkeeping table is intentionally absent from the ORM.
    live.pop("alembic_version", None)
    declared = {table.name: set(table.columns.keys()) for table in Base.metadata.sorted_tables}

    missing_tables = sorted(set(declared) - set(live))
    assert not missing_tables, f"ORM tables missing from the live database: {missing_tables}"

    missing_columns = {
        table: sorted(declared[table] - live[table])
        for table in declared
        if declared[table] - live[table]
    }
    assert not missing_columns, f"ORM columns missing from the live database: {missing_columns}"

    extra_tables = sorted(set(live) - set(declared))
    assert not extra_tables, f"live database has tables the ORM does not declare: {extra_tables}"
