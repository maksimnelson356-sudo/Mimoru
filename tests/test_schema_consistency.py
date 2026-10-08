from scripts.check_schema_consistency import compare_schemas


def test_orm_matches_migration_schema() -> None:
    assert compare_schemas() == []
