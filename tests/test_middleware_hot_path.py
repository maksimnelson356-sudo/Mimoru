from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_group_lookup_includes_untouchable_state_in_same_query() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    assert "untouchable_exists = (" in source
    assert "select(RankAssignment.id)" in source
    assert '.where(' in source
    assert "select(" in source
    assert "Group, untouchable_exists.label(\"is_untouchable\")" in source
    assert "await is_untouchable(" not in source


def test_group_message_tracking_skips_duplicate_user_and_row_fetch() -> None:
    source = (ROOT / "app/middlewares.py").read_text(encoding="utf-8")
    assert "ensure_user=False" in source
    assert "return_row=False" in source
