from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Column

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.base import Base
import app.db.ad_market_models  # noqa: F401
import app.db.moderation_command_models  # noqa: F401
import app.db.moderation_operation_models  # noqa: F401
import app.db.payment_refund_models  # noqa: F401
import app.db.broadcast_models  # noqa: F401  # registers broadcast delivery ORM models
import app.db.deleted_cleanup_retry_models  # noqa: F401  # registers cleanup retry ORM model
import app.db.fun_models  # noqa: F401  # registers entertainment ORM models
import app.db.game_models  # noqa: F401  # registers durable game engine ORM models
import app.db.group_disconnect_models  # noqa: F401  # registers group disconnect ORM model
import app.db.invite_operation_models  # noqa: F401  # registers invite operation ORM models
import app.db.models  # noqa: F401  # registers core ORM models
import app.db.pending_bans  # noqa: F401  # registers deferred moderation ORM models
import app.db.permission_transition_models  # noqa: F401  # registers chat permission transition ORM models
import app.db.rank_models  # noqa: F401  # registers rank ORM models
import app.db.rank_provisioning_models  # noqa: F401  # registers rank provisioning ORM models
import app.db.required_reconcile_models  # noqa: F401  # registers durable required-reconcile ORM model


class SchemaRecorder:
    """Minimal Alembic op replacement used to reconstruct the final schema."""

    def __init__(self) -> None:
        self.tables: dict[str, set[str]] = {}

    def create_table(self, name: str, *items: Any, **_: Any) -> None:
        self.tables[name] = {item.name for item in items if isinstance(item, Column)}

    def add_column(self, table_name: str, column: Column[Any]) -> None:
        self.tables.setdefault(table_name, set()).add(column.name)

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.tables.setdefault(table_name, set()).discard(column_name)

    def drop_table(self, table_name: str) -> None:
        self.tables.pop(table_name, None)

    def __getattr__(self, _: str):
        return lambda *args, **kwargs: None


def migration_schema(versions_dir: Path) -> dict[str, set[str]]:
    recorder = SchemaRecorder()
    for path in sorted(versions_dir.glob("*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load migration: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = recorder
        module.upgrade()
    return recorder.tables


def orm_schema() -> dict[str, set[str]]:
    return {table.name: set(table.columns.keys()) for table in Base.metadata.sorted_tables}


def compare_schemas() -> list[str]:
    root = Path(__file__).resolve().parents[1]
    migrated = migration_schema(root / "alembic" / "versions")
    models = orm_schema()
    errors: list[str] = []

    for table in sorted(models.keys() - migrated.keys()):
        errors.append(f"ORM table is missing from migrations: {table}")
    for table in sorted(migrated.keys() - models.keys()):
        errors.append(f"Migrated table is missing from ORM: {table}")

    for table in sorted(models.keys() & migrated.keys()):
        for column in sorted(models[table] - migrated[table]):
            errors.append(f"ORM column is missing from migrations: {table}.{column}")
        for column in sorted(migrated[table] - models[table]):
            errors.append(f"Migrated column is missing from ORM: {table}.{column}")
    return errors


def main() -> int:
    errors = compare_schemas()
    if errors:
        print("Schema consistency check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Schema consistency OK: ORM tables and columns match migration output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
