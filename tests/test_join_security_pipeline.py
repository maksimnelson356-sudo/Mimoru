from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_members_join_winner_runs_deferred_bans_before_security_pipeline() -> None:
    source = (ROOT / "app/handlers/members.py").read_text(encoding="utf-8")
    body = source.split("async def welcome", 1)[1].split("async def verification_callback", 1)[0]

    deferred = body.index("await enforce_pending_ban_on_join")
    group = body.index("select(Group)", deferred)
    channels = body.index("await required_channels", group)
    tracking = body.index("await track_group_member", channels)
    commit = body.index("await session.commit()", tracking)
    restrict = body.index("await bot.restrict_chat_member", commit)
    captcha_state = body.index("await redis.set(", restrict)

    # The DB transaction must be committed before any Telegram network call, so
    # no connection / row lock is held across the network I/O.
    assert deferred < group < channels < tracking < commit < restrict < captcha_state
    assert ".with_for_update()" not in body or body.index(".with_for_update()") > captcha_state


def test_banned_members_are_skipped_but_other_members_continue() -> None:
    members = (ROOT / "app/handlers/members.py").read_text(encoding="utf-8")
    body = members.split("async def welcome", 1)[1].split("async def verification_callback", 1)[0]
    deferred = (ROOT / "app/handlers/deferred_bans.py").read_text(encoding="utf-8")
    helper = deferred.split("async def enforce_pending_ban_on_join", 1)[1]

    assert "banned_user_ids: set[int] = set()" in helper
    telegram_ban = helper.index("await bot.ban_chat_member")
    add_banned = helper.index("banned_user_ids.add(member.id)", telegram_ban)
    commit = helper.index("await session.commit()", add_banned)
    returned = helper.index("return banned_user_ids", commit)
    assert telegram_ban < add_banned < commit < returned

    skip = body.index("if member.is_bot or member.id in banned_user_ids:")
    tracking = body.index("await track_group_member", skip)
    verification = body.index("needs_verification =", tracking)
    assert skip < tracking < verification


def test_only_members_router_registers_broad_new_member_handler() -> None:
    deferred = (ROOT / "app/handlers/deferred_bans.py").read_text(encoding="utf-8")
    members = (ROOT / "app/handlers/members.py").read_text(encoding="utf-8")

    assert "@router.message(F.chat.type.in_(GROUP_TYPES), F.new_chat_members)" not in deferred
    assert members.count("@router.message(F.new_chat_members)") == 1
    assert "from app.handlers.deferred_bans import enforce_pending_ban_on_join" in members
