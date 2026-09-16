from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source() -> str:
    return (ROOT / "app/handlers/member_center.py").read_text(encoding="utf-8")


def _handler(source: str, name: str) -> str:
    body = source.split(f"async def {name}(", 1)[1]
    boundaries = [
        index
        for marker in ("\n@router.", "\nasync def ")
        if (index := body.find(marker)) >= 0
    ]
    return body[: min(boundaries)] if boundaries else body


def test_owned_group_lock_is_opt_in() -> None:
    source = _source()
    helper = source.split("async def owned_group(", 1)[1].split("def _user_name", 1)[0]
    assert "for_update: bool = False" in helper
    assert "if for_update:" in helper
    assert "query = query.with_for_update()" in helper


def test_member_release_locks_before_telegram_effect_and_commit() -> None:
    body = _handler(_source(), "member_action")
    lock = body.index("for_update=True")
    unmute = body.index("await bot.restrict_chat_member(")
    unban = body.index("await bot.unban_chat_member(")
    commit = body.index("await session.commit()")
    assert lock < unmute < commit
    assert lock < unban < commit


def test_member_panel_durable_writes_use_locked_owner_boundary() -> None:
    source = _source()
    expectations = {
        "member_note_input": "ModeratorNote(",
        "member_tag_toggle": "await session.commit()",
        "member_tag_input": "await session.commit()",
        "_close_complaint": "row.status = status",
    }
    for name, mutation in expectations.items():
        body = _handler(source, name)
        assert "for_update=True" in body
        assert body.index("for_update=True") < body.index(mutation)
        assert "await session.commit()" in body


def test_member_reads_and_punishment_staging_remain_non_locking() -> None:
    source = _source()
    for name in (
        "member_card",
        "active_punishments",
        "member_history",
        "member_notes",
        "member_tags",
        "complaints",
        "complaint_detail",
    ):
        body = _handler(source, name)
        assert "for_update=True" not in body


def test_member_punish_only_stages_pending_action() -> None:
    body = _handler(_source(), "member_punish")
    assert "await redis.setex(" in body
    assert "await bot.restrict_chat_member(" not in body
    assert "await bot.ban_chat_member(" not in body
    assert "await bot.unban_chat_member(" not in body
