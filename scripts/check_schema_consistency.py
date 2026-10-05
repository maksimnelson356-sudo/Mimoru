from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Column, ForeignKeyConstraint, Index, UniqueConstraint, CheckConstraint, PrimaryKeyConstraint

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.base import Base
import app.db.ad_market_models  # noqa: F401
import app.db.moderation_command_models  # noqa: F401
import app.db.moderation_operation_models  # noqa: F401
import app.db.payment_refund_models  # noqa: F401
import app.db.broadcast_models  # noqa: F401
import app.db.deleted_cleanup_retry_models  # noqa: F401
import app.db.fun_models  # noqa: F401
import app.db.game_models  # noqa: F401
import app.db.group_disconnect_models  # noqa: F401
import app.db.invite_operation_models  # noqa: F401
import app.db.models  # noqa: F401
import app.db.pending_bans  # noqa: F401
import app.db.permission_transition_models  # noqa: F401
import app.db.rank_models  # noqa: F401
import app.db.rank_provisioning_models  # noqa: F401
import app.db.required_reconcile_models  # noqa: F401


def load_revisions() -> dict[str, tuple[str | None, Path]]:
    """Load all migrations and return mapping revision -> (down_revision, path)."""
    import ast
    revisions = {}
    for path in Path("alembic/versions").glob("*.py"):
        tree = ast.parse(path.read_text())
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"revision", "down_revision"}:
                    values[node.targets[0].id] = ast.literal_eval(node.value)
        revisions[values["revision"]] = (values.get("down_revision"), path)
    return revisions


def build_chain(revisions: dict[str, tuple[str | None, Path]]) -> list[Path]:
    """Build linear migration chain from base to head."""
    heads = set(revisions)
    for parent, _ in revisions.values():
        if parent:
            if parent not in revisions:
                raise SystemExit(f"Missing migration parent: {parent}")
            heads.discard(parent)
    if len(heads) != 1:
        raise SystemExit(f"Expected one migration head, got: {sorted(heads)}")
    head = next(iter(heads))

    chain = []
    current = head
    while current:
        chain.append(revisions[current][1])
        current = revisions[current][0]
    chain.reverse()
    return chain


class SchemaRecorder:
    """Minimal Alembic op replacement used to reconstruct the final schema."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Any]] = {}

    def _ensure_table(self, name: str) -> dict[str, Any]:
        if name not in self.tables:
            self.tables[name] = {
                "columns": {},
                "indexes": [],
                "foreign_keys": [],
                "unique_constraints": [],
                "check_constraints": [],
                "primary_key": [],
            }
        return self.tables[name]

    def _column_info(self, col: Column) -> dict[str, Any]:
        return {
            "type": str(col.type),
            "nullable": col.nullable,
            "default": str(col.default.arg) if col.default is not None else None,
            "server_default": str(col.server_default.arg) if col.server_default is not None else None,
            "primary_key": col.primary_key,
            "autoincrement": col.autoincrement,
        }

    def create_table(self, name: str, *items: Any, **kwargs: Any) -> None:
        table = self._ensure_table(name)
        # First pass: collect columns
        for item in items:
            if isinstance(item, Column):
                table["columns"][item.name] = self._column_info(item)
                # Track primary key from column
                if item.primary_key:
                    table["primary_key"].append(item.name)
                # Track foreign keys from column (sa.ForeignKey)
                for fk in item.foreign_keys:
                    # In migrations, FK target is in target_fullname like "groups.id"
                    target = getattr(fk, "target_fullname", "") or ""
                    ref_table = None
                    ref_col = None
                    if "." in target:
                        ref_table, ref_col = target.split(".", 1)
                    table["foreign_keys"].append({
                        "name": fk.name,
                        "columns": [item.name],
                        "referred_table": ref_table,
                        "referred_columns": [ref_col] if ref_col else [],
                        "ondelete": fk.ondelete,
                    })

        # Second pass: collect constraints that reference columns
        for item in items:
            if isinstance(item, UniqueConstraint):
                # In migrations, UniqueConstraint columns are strings
                cols = list(item.columns) if hasattr(item.columns, '__iter__') else []
                table["unique_constraints"].append({
                    "name": item.name,
                    "columns": cols,
                })
            elif isinstance(item, ForeignKeyConstraint):
                # In migrations, FKs are usually inline on columns, not separate constraints
                pass
            elif isinstance(item, CheckConstraint):
                table["check_constraints"].append({
                    "name": item.name,
                    "sqltext": str(item.sqltext),
                })
            elif isinstance(item, Index):
                table["indexes"].append({
                    "name": item.name,
                    "columns": [c.name if hasattr(c, 'name') else str(c) for c in item.columns],
                    "unique": item.unique,
                })

    def create_index(self, name: str, table_name: str, columns: list[str], unique: bool = False, **_: Any) -> None:
        table = self._ensure_table(table_name)
        table["indexes"].append({
            "name": name,
            "columns": columns,
            "unique": unique,
        })

    def add_column(self, table_name: str, column: Column[Any]) -> None:
        table = self._ensure_table(table_name)
        table["columns"][column.name] = self._column_info(column)

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.tables.setdefault(table_name, {}).setdefault("columns", {}).pop(column_name, None)

    def drop_table(self, table_name: str) -> None:
        self.tables.pop(table_name, None)

    def drop_index(self, name: str, table_name: str, **_: Any) -> None:
        table = self.tables.get(table_name)
        if table:
            table["indexes"] = [i for i in table["indexes"] if i["name"] != name]

    def drop_constraint(self, name: str, table_name: str, type_: str = "", **_: Any) -> None:
        table = self.tables.get(table_name)
        if not table:
            return
        if type_ == "unique" or "unique" in name.lower():
            table["unique_constraints"] = [c for c in table["unique_constraints"] if c["name"] != name]
        elif type_ == "foreignkey" or "fk_" in name.lower():
            table["foreign_keys"] = [c for c in table["foreign_keys"] if c["name"] != name]
        elif type_ == "check" or "ck_" in name.lower():
            table["check_constraints"] = [c for c in table["check_constraints"] if c["name"] != name]
        elif type_ == "primary" or "pk_" in name.lower():
            table["primary_key"] = []

    def create_unique_constraint(self, name: str, table_name: str, columns: list[str], **_: Any) -> None:
        table = self._ensure_table(table_name)
        table["unique_constraints"].append({"name": name, "columns": columns})

    def create_foreign_key(self, name: str, source_table: str, referent_table: str,
                           local_cols: list[str], remote_cols: list[str], ondelete: str | None = None, **_: Any) -> None:
        table = self._ensure_table(source_table)
        table["foreign_keys"].append({
            "name": name,
            "columns": local_cols,
            "referred_table": referent_table,
            "referred_columns": remote_cols,
            "ondelete": ondelete,
        })

    def create_check_constraint(self, name: str, table_name: str, condition: str, **_: Any) -> None:
        table = self._ensure_table(table_name)
        table["check_constraints"].append({"name": name, "sqltext": condition})

    def __getattr__(self, _: str):
        return lambda *args, **kwargs: None


def migration_schema(versions_dir: Path) -> dict[str, dict[str, Any]]:
    recorder = SchemaRecorder()
    revisions = load_revisions()
    chain = build_chain(revisions)

    for path in chain:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load migration: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = recorder
        module.upgrade()
    return recorder.tables


def orm_schema() -> dict[str, dict[str, Any]]:
    result = {}
    for table in Base.metadata.sorted_tables:
        t = {
            "columns": {},
            "indexes": [],
            "foreign_keys": [],
            "unique_constraints": [],
            "check_constraints": [],
            "primary_key": [],
        }
        for col in table.columns:
            t["columns"][col.name] = {
                "type": str(col.type),
                "nullable": col.nullable,
                "default": str(col.default.arg) if col.default is not None else None,
                "server_default": str(col.server_default.arg) if col.server_default is not None else None,
                "primary_key": col.primary_key,
                "autoincrement": col.autoincrement,
            }
        for idx in table.indexes:
            t["indexes"].append({
                "name": idx.name,
                "columns": [c.name for c in idx.columns],
                "unique": idx.unique,
            })
        for fk in table.foreign_keys:
            ref_table_name = None
            try:
                if fk.column.table is not None:
                    ref_table_name = fk.column.table.name
            except Exception:
                pass
            t["foreign_keys"].append({
                "name": fk.name,
                "columns": [fk.parent.name],
                "referred_table": ref_table_name,
                "referred_columns": [fk.column.name],
                "ondelete": fk.ondelete,
            })
        for uc in table.constraints:
            if isinstance(uc, UniqueConstraint):
                t["unique_constraints"].append({
                    "name": uc.name,
                    "columns": list(uc.columns.keys()),
                })
            elif isinstance(uc, CheckConstraint):
                t["check_constraints"].append({
                    "name": uc.name,
                    "sqltext": str(uc.sqltext),
                })
            elif isinstance(uc, PrimaryKeyConstraint):
                t["primary_key"] = list(uc.columns.keys())
        result[table.name] = t
    return result


def compare_schemas() -> list[str]:
    root = Path(__file__).resolve().parents[1]
    migrated = migration_schema(root / "alembic" / "versions")
    models = orm_schema()
    errors: list[str] = []

    all_tables = set(models.keys()) | set(migrated.keys())

    for table in sorted(all_tables):
        if table not in models:
            errors.append(f"ORM table is missing from migrations: {table}")
            continue
        if table not in migrated:
            errors.append(f"Migrated table is missing from ORM: {table}")
            continue

        m_table = models[table]
        r_table = migrated[table]

        # Compare columns
        all_cols = set(m_table["columns"].keys()) | set(r_table["columns"].keys())
        for col in sorted(all_cols):
            if col not in m_table["columns"]:
                errors.append(f"ORM column is missing from migrations: {table}.{col}")
                continue
            if col not in r_table["columns"]:
                errors.append(f"Migrated column is missing from ORM: {table}.{col}")
                continue

            m_col = m_table["columns"][col]
            r_col = r_table["columns"][col]

            # Compare type (normalize some common differences)
            m_type = m_col["type"].replace("VARCHAR", "STRING").replace("BIGINT", "INTEGER").upper()
            r_type = r_col["type"].replace("VARCHAR", "STRING").replace("BIGINT", "INTEGER").upper()
            if m_type != r_type:
                errors.append(f"Column type mismatch: {table}.{col} ORM={m_col['type']} vs migration={r_col['type']}")

            # Compare nullable
            if m_col["nullable"] != r_col["nullable"]:
                errors.append(f"Column nullable mismatch: {table}.{col} ORM={m_col['nullable']} vs migration={r_col['nullable']}")

            # Compare primary_key
            if m_col["primary_key"] != r_col["primary_key"]:
                errors.append(f"Column primary_key mismatch: {table}.{col} ORM={m_col['primary_key']} vs migration={r_col['primary_key']}")

            # Compare server_default (only if both present)
            m_sd = m_col["server_default"]
            r_sd = r_col["server_default"]
            if m_sd and r_sd and m_sd != r_sd:
                if not (m_sd.strip("'\"") == r_sd.strip("'\"")):
                    errors.append(f"Column server_default mismatch: {table}.{col} ORM={m_sd} vs migration={r_sd}")

        # Compare primary keys
        m_pk = sorted(m_table["primary_key"])
        r_pk = sorted(r_table["primary_key"])
        if m_pk != r_pk:
            errors.append(f"Primary key mismatch: {table} ORM={m_pk} vs migration={r_pk}")

        # Compare indexes (by columns+unique, ignore names which may differ)
        def idx_key(idx):
            return (tuple(sorted(idx["columns"])), idx["unique"])
        m_idxs = {idx_key(i) for i in m_table["indexes"]}
        r_idxs = {idx_key(i) for i in r_table["indexes"]}
        for idx in m_idxs - r_idxs:
            errors.append(f"Index missing in migration: {table} columns={idx[0]} unique={idx[1]}")
        for idx in r_idxs - m_idxs:
            errors.append(f"Index missing in ORM: {table} columns={idx[0]} unique={idx[1]}")

        # Compare unique constraints (by columns, ignore names)
        m_ucs = {tuple(sorted(c["columns"])) for c in m_table["unique_constraints"]}
        r_ucs = {tuple(sorted(c["columns"])) for c in r_table["unique_constraints"]}
        for uc in m_ucs - r_ucs:
            errors.append(f"Unique constraint missing in migration: {table} columns={uc}")
        for uc in r_ucs - m_ucs:
            errors.append(f"Unique constraint missing in ORM: {table} columns={uc}")

        # Compare foreign keys (by columns+referred_table+ondelete, ignore names)
        def fk_key(fk):
            return (tuple(sorted(fk["columns"])), fk["referred_table"], fk.get("ondelete"))
        m_fks = {fk_key(f) for f in m_table["foreign_keys"]}
        r_fks = {fk_key(f) for f in r_table["foreign_keys"]}
        for fk in m_fks - r_fks:
            errors.append(f"Foreign key missing in migration: {table} columns={fk[0]} ref={fk[1]} ondelete={fk[2]}")
        for fk in r_fks - m_fks:
            errors.append(f"Foreign key missing in ORM: {table} columns={fk[0]} ref={fk[1]} ondelete={fk[2]}")

        # Compare check constraints (by sqltext, ignore names)
        m_ccs = {c["sqltext"] for c in m_table["check_constraints"]}
        r_ccs = {c["sqltext"] for c in r_table["check_constraints"]}
        for cc in m_ccs - r_ccs:
            errors.append(f"Check constraint missing in migration: {table} condition={cc}")
        for cc in r_ccs - m_ccs:
            errors.append(f"Check constraint missing in ORM: {table} condition={cc}")

    return errors


def main() -> int:
    errors = compare_schemas()
    if errors:
        print("Schema consistency check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Schema consistency OK: ORM tables, columns, indexes, FKs, constraints match migration output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
