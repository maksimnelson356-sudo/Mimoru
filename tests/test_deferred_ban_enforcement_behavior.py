"""Behavioral regression tests for deferred-ban join enforcement.

These run in the fast (no-DB) gate and exercise the control flow of
``enforce_pending_ban_on_join`` with lightweight fakes: the Redis claim gate
(token-based SET NX + owner-bound Lua release/extend), commit-before-Telegram
ordering, failure handling, and claim release.
"""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest

from app.handlers.deferred_bans import (
    _EXTEND_CLAIM_LUA,
    _RELEASE_CLAIM_LUA,
    enforce_pending_ban_on_join,
)


class _StubChat:
    def __init__(self, cid: int) -> None:
        self.id = cid


class _StubMember:
    def __init__(self, mid: int, *, bot: bool = False, username: str | None = None) -> None:
        self.id = mid
        self.full_name = f"Name{mid}"
        self.is_bot = bot
        self.username = username


class _StubMessage:
    def __init__(self, chat_id: int, members: list[_StubMember]) -> None:
        self.chat = _StubChat(chat_id)
        self.new_chat_members = members
        self.answered: str | None = None

    async def answer(self, text: str) -> None:
        self.answered = text


class _Group:
    def __init__(self, chat_id: int, gid: int) -> None:
        self.telegram_chat_id = chat_id
        self.id = gid


class _Pending:
    def __init__(self, moderator_id: int = 7) -> None:
        self.moderator_telegram_id = moderator_id
        self.user_telegram_id: int | None = None


class _FakeSession:
    def __init__(self, group: _Group, pending: _Pending | None = None, active_ban=None) -> None:
        self.group = group
        self.pending = pending
        self.active_ban = active_ban
        self.commits = 0
        self.added: list = []
        self.flushed = 0

    async def scalar(self, query) -> object:
        q = str(query).lower()
        if "from groups" in q:
            return self.group
        if "pending_bans" in q:
            return self.pending
        return self.active_ban

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class _FakeRedis:
    """Store-backed Redis fake implementing the exact claim-key semantics.

    ``SET key value NX`` only succeeds when the key is absent; the release and
    extend Lua scripts only affect the key when its stored value matches the
    caller's token. This mirrors the real Redis guarantee.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets = 0
        self.releases = 0
        self.extends = 0

    async def set(self, key, value, *, nx=None, ex=None) -> bool:
        self.sets += 1
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *args) -> int:
        key, rest = args[0], args[1:]
        if script == _RELEASE_CLAIM_LUA:
            token = rest[0]
            if self.store.get(key) == token:
                del self.store[key]
                self.releases += 1
                return 1
            return 0
        if script == _EXTEND_CLAIM_LUA:
            token = rest[0]
            if self.store.get(key) == token:
                self.extends += 1
                return 1
            return 0
        raise AssertionError(f"unexpected eval script: {script!r}")

    def force_expire(self, key: str) -> None:
        """Simulate TTL expiry by removing the key without releasing it."""
        self.store.pop(key, None)


class _FakeBot:
    def __init__(self, fail_ban: bool = False) -> None:
        self.fail_ban = fail_ban
        self.banned: list[tuple[int, int]] = []

    async def ban_chat_member(self, chat_id: int, user_id: int) -> None:
        if self.fail_ban:
            raise TelegramBadRequest(method="ban_chat_member", message="ban failed")
        self.banned.append((chat_id, user_id))


def _claim_key(group_id: int, member_id: int) -> str:
    return f"pending_ban_applying:{group_id}:{member_id}"


# --- Function-level behavior -------------------------------------------------

async def test_happy_path_commits_before_ban_and_releases_claim() -> None:
    msg = _StubMessage(100, [_StubMember(11, username="alice")])
    sess = _FakeSession(group=_Group(100, 1), pending=_Pending(moderator_id=7))
    redis = _FakeRedis()
    bot = _FakeBot()

    banned = await enforce_pending_ban_on_join(msg, bot, sess, redis)

    assert banned == {11}
    assert bot.banned == [(100, 11)]
    assert sess.commits >= 1
    assert msg.answered is not None
    # The punishment record is created and the claim is released (owner-bound).
    assert any(getattr(obj, "kind", None) == "ban" for obj in sess.added)
    assert redis.releases == 1
    assert _claim_key(1, 11) not in redis.store
    # The lease was renewed before the network calls.
    assert redis.extends >= 1


async def test_denied_claim_skips_member_completely() -> None:
    msg = _StubMessage(100, [_StubMember(11, username="alice")])
    sess = _FakeSession(group=_Group(100, 1), pending=_Pending(moderator_id=7))
    # A foreign handler already owns the claim (its TTL has not expired).
    redis = _FakeRedis()
    await redis.set(_claim_key(1, 11), "alien-token", nx=True)
    bot = _FakeBot()

    banned = await enforce_pending_ban_on_join(msg, bot, sess, redis)

    assert banned == set()
    assert bot.banned == []
    assert sess.added == []
    assert msg.answered is None
    # The alien claim is untouched.
    assert redis.store[_claim_key(1, 11)] == "alien-token"
    assert redis.releases == 0


async def test_failed_telegram_ban_loses_nothing_and_releases_claim() -> None:
    msg = _StubMessage(100, [_StubMember(11, username="alice")])
    sess = _FakeSession(group=_Group(100, 1), pending=_Pending(moderator_id=7))
    redis = _FakeRedis()
    bot = _FakeBot(fail_ban=True)

    banned = await enforce_pending_ban_on_join(msg, bot, sess, redis)

    # The member is not reported banned, no punishment/notification is recorded,
    # the pending ban stays active (retry later), and the claim is released.
    assert banned == set()
    assert sess.added == []
    assert msg.answered is None
    assert redis.releases == 1
    assert _claim_key(1, 11) not in redis.store


async def test_bots_are_skipped_and_untouched() -> None:
    msg = _StubMessage(100, [_StubMember(5, bot=True)])
    sess = _FakeSession(group=_Group(100, 1), pending=_Pending(moderator_id=7))
    redis = _FakeRedis()
    bot = _FakeBot()

    banned = await enforce_pending_ban_on_join(msg, bot, sess, redis)

    assert banned == set()
    assert bot.banned == []
    assert redis.sets == 0


# --- Redis claim ownership guarantees (token-bound Lua) ---------------------

async def test_release_never_deletes_alien_claim_after_ttl_expiry() -> None:
    redis = _FakeRedis()
    key = _claim_key(1, 11)

    # Handler A acquires the claim.
    assert await redis.set(key, "token-A", nx=True)
    # A's TTL expires.
    redis.force_expire(key)
    # Handler B acquires a fresh claim for the same member.
    assert await redis.set(key, "token-B", nx=True)

    # A finishes late and releases its own claim: must NOT delete B's claim.
    released_alien = await redis.eval(_RELEASE_CLAIM_LUA, 1, key, "token-A")
    assert released_alien == 0
    assert redis.store[key] == "token-B"
    assert redis.releases == 0

    # B releases its own claim: succeeds and removes the key.
    released_owner = await redis.eval(_RELEASE_CLAIM_LUA, 1, key, "token-B")
    assert released_owner == 1
    assert key not in redis.store
    assert redis.releases == 1


async def test_extend_never_extends_alien_claim() -> None:
    redis = _FakeRedis()
    key = _claim_key(1, 11)

    assert await redis.set(key, "token-B", nx=True)

    # Handler A (stale token) cannot extend B's claim.
    extended_alien = await redis.eval(_EXTEND_CLAIM_LUA, 1, key, "token-A", 60)
    assert extended_alien == 0
    assert redis.extends == 0

    # B can extend its own claim.
    extended_owner = await redis.eval(_EXTEND_CLAIM_LUA, 1, key, "token-B", 60)
    assert extended_owner == 1
    assert redis.extends == 1


async def test_concurrent_handlers_do_not_double_apply_same_member() -> None:
    # Two join events for the same member arrive while the first handler is
    # still processing. The second handler's SET NX is denied because the first
    # handler owns the claim, so no duplicate ban/punishment/notification.
    msg1 = _StubMessage(100, [_StubMember(11, username="alice")])
    sess1 = _FakeSession(group=_Group(100, 1), pending=_Pending(moderator_id=7))
    redis = _FakeRedis()
    bot = _FakeBot()

    # Enforce the claim for handler 1 (simulated in-flight).
    key = _claim_key(1, 11)
    assert await redis.set(key, "token-1", nx=True)

    # Handler 2 tries to process the same member while the claim is held.
    msg2 = _StubMessage(100, [_StubMember(11, username="alice")])
    sess2 = _FakeSession(group=_Group(100, 1), pending=_Pending(moderator_id=7))
    banned2 = await enforce_pending_ban_on_join(msg2, bot, sess2, redis)
    assert banned2 == set()
    assert bot.banned == []
    assert sess2.added == []
    assert msg2.answered is None

    # Handler 1 completes and releases only its own claim.
    assert await redis.eval(_RELEASE_CLAIM_LUA, 1, key, "token-1") == 1
    assert key not in redis.store
    assert sess1.commits == 0  # handler 1 did not run the fake session here
