from pathlib import Path
import ast


def load_revisions() -> dict[str, tuple[str | None, str]]:
    """Load all migrations and return mapping revision -> (down_revision, filename)."""
    revisions = {}
    for path in Path("alembic/versions").glob("*.py"):
        tree = ast.parse(path.read_text())
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"revision", "down_revision"}:
                    values[node.targets[0].id] = ast.literal_eval(node.value)
        revisions[values["revision"]] = (values.get("down_revision"), path.name)
    return revisions


def build_chain(revisions: dict[str, tuple[str | None, str]]) -> list[str]:
    """Build linear migration chain from head to base."""
    heads = set(revisions)
    for parent, _ in revisions.values():
        if parent:
            if parent not in revisions:
                raise SystemExit(f"Missing migration parent: {parent}")
            heads.discard(parent)
    if len(heads) != 1:
        raise SystemExit(f"Expected one migration head, got: {sorted(heads)}")
    head = next(iter(heads))

    # Walk from head to base
    chain = []
    current = head
    while current:
        chain.append(current)
        current = revisions[current][0]
    chain.reverse()  # base -> head
    return chain


def main() -> int:
    revisions = load_revisions()
    chain = build_chain(revisions)
    print(f"Migration chain OK ({len(chain)} migrations), head: {chain[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
