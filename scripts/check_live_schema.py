"""Compare the ORM schema against the live PostgreSQL schema.

Static ORM-versus-migration reconstruction cannot be trusted here: migrations
express the same DDL through several shapes (`op.create_table("t", *cols)`,
`op.drop_index(name, table_name=...)`, local helper functions, f-string index
names), so any recorder silently misses part of the schema and reports drift
that does not exist. The live database has no such ambiguity, so this compares
`Base.metadata` with `pg_catalog` and is meant to run from `scripts/check_db.sh`
against a real PostgreSQL.

Signatures are compared by column names rather than by object name, because a
migration and the ORM may legitimately name the same index differently.

Exits non-zero when the ORM expects something the database does not have: that
combination means a query would fail at runtime. Objects the database has and the
ORM does not know are reported as warnings, since the code never touches them.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import inspect

from app.db.base import Base
import app.db.ad_market_models  # noqa: F401  # registers ORM models
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
from app.db.session import engine

# Alembic owns its bookkeeping table; it is not part of the ORM.
INTERNAL_TABLES = {"alembic_version"}


def orm_index_signatures(table) -> set[tuple[tuple[str, ...], bool]]:
    """(columns, unique) for every index the ORM declares on a table."""
    return {
        (tuple(sorted(column.name for column in index.columns)), bool(index.unique))
        for index in table.indexes
    }


def orm_unique_signatures(table) -> set[tuple[str, ...]]:
    """Column tuples for every unique constraint the ORM declares on a table."""
    return {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in table.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }


def live_index_signatures(indexes) -> set[tuple[tuple[str, ...], bool]]:
    """Same signature shape as orm_index_signatures, built from the inspector."""
    return {
        (tuple(sorted(entry.get("column_names") or [])), bool(entry.get("unique")))
        for entry in indexes
    }


def live_unique_signatures(constraints) -> set[tuple[str, ...]]:
    return {
        tuple(sorted(entry.get("column_names") or []))
        for entry in constraints
    }


def compare(inspector) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    live_tables = set(inspector.get_table_names()) - INTERNAL_TABLES
    orm_tables = set(Base.metadata.tables)

    for name in sorted(orm_tables - live_tables):
        errors.append(f"table declared in ORM is missing in the database: {name}")
    for name in sorted(live_tables - orm_tables):
        warnings.append(f"table exists in the database but not in the ORM: {name}")

    for name in sorted(orm_tables & live_tables):
        orm_table = Base.metadata.tables[name]

        live_columns = {column["name"] for column in inspector.get_columns(name)}
        for column in sorted(orm_table.columns.keys() - live_columns):
            errors.append(f"column missing in the database: {name}.{column}")
        for column in sorted(live_columns - set(orm_table.columns.keys())):
            warnings.append(f"column exists in the database but not in the ORM: {name}.{column}")

        live_indexes = live_index_signatures(inspector.get_indexes(name))
        for signature in sorted(orm_index_signatures(orm_table) - live_indexes):
            errors.append(
                f"index missing in the database: {name} columns={list(signature[0])} "
                f"unique={signature[1]}"
            )
        for signature in sorted(live_indexes - orm_index_signatures(orm_table)):
            warnings.append(
                f"index exists in the database but not in the ORM: {name} "
                f"columns={list(signature[0])} unique={signature[1]}"
            )

        live_unique = live_unique_signatures(inspector.get_unique_constraints(name))
        for signature in sorted(orm_unique_signatures(orm_table) - live_unique):
            errors.append(
                f"unique constraint missing in the database: {name} columns={list(signature)}"
            )

    return errors, warnings


async def run() -> int:
    async with engine.connect() as connection:
        inspector = await connection.run_sync(lambda sync: inspect(sync))
        errors, warnings = compare(inspector)

    for warning in warnings:
        print(f"WARN {warning}", file=sys.stderr)
    if errors:
        print("Live schema does not satisfy the ORM:")
        for error in errors:
            print(f"- {error}")
        print(
            "Run `alembic upgrade head` and add a migration; the ORM alone cannot "
            "create the missing object.",
            file=sys.stderr,
        )
        return 1

    tables = len(set(Base.metadata.tables))
    print(f"Live schema matches the ORM: {tables} tables checked, no missing objects.")
    if warnings:
        print(f"Live schema has {len(warnings)} object(s) the ORM does not declare.")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    finally:
        asyncio.run(engine.dispose())


if __name__ == "__main__":
    raise SystemExit(main())