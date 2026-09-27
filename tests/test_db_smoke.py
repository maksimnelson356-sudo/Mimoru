"""Smoke tests that need a real PostgreSQL, not a mock.

Everything else in the suite runs without a database, which leaves three classes
of behaviour unverified:

1. the unique constraints that stand between a retry and a double charge;
2. the ``UPDATE ... RETURNING`` claim pattern used by the report and scheduled
   message workers;
3. ``FOR UPDATE`` / ``FOR UPDATE SKIP LOCKED``, which every destructive handler
   relies on for serialization but which a mocked session cannot prove.

These tests run through ``scripts/check_db.sh``. They create rows under unique
Telegram ids and delete them afterwards, so nothing is left behind and a
misdirected DATABASE_URL cannot truncate unrelated data.
"""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models import DailyStat, Group, GroupSettings, Payment
from app.db.session import SessionFactory
from scripts import ensure_version_column


@pytest.fixture
async def make_group():
    """Create groups with unique ids and remove them after the test."""
    created: list[int] = []

    async def _create():
        suffix = secrets.randbelow(1_000_000_000)
        chat_id = -1_000_000_000 - suffix
        owner_id = 1_000_000_000 + suffix
        async with SessionFactory() as session:
            group = Group(
                telegram_chat_id=chat_id,
                title=f"db-smoke-{suffix}",
                owner_telegram_id=owner_id,
                settings=GroupSettings(),
            )
            session.add(group)
            await session.commit()
            created.append(group.id)
        return chat_id, owner_id

    yield _create

    async with SessionFactory() as session:
        for group_id in created:
            await session.execute(delete(Group).where(Group.id == group_id))
        await session.commit()


@pytest.mark.db
async def test_group_and_settings_round_trip(make_group) -> None:
    """Proves the ORM can insert and read the core one-to-one shape."""
    chat_id, owner_id = await make_group()

    async with SessionFactory() as session:
        group = await session.scalar(select(Group).where(Group.telegram_chat_id == chat_id))

    assert group is not None
    assert group.owner_telegram_id == owner_id
    assert group.is_active is True
    assert group.plan_code == "trial"
    assert group.settings.group_id == group.id
    assert group.settings.reports_enabled is True
    assert group.settings.last_report_date is None


@pytest.mark.db
async def test_group_telegram_chat_id_is_unique(make_group) -> None:
    chat_id, _ = await make_group()

    async with SessionFactory() as session:
        session.add(Group(telegram_chat_id=chat_id, title="duplicate"))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.db
async def test_group_allows_only_one_settings_row(make_group) -> None:
    _, owner_id = await make_group()

    async with SessionFactory() as session:
        group = await session.scalar(
            select(Group).where(Group.owner_telegram_id == owner_id)
        )
        assert group is not None
        session.add(GroupSettings(group_id=group.id))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.db
async def test_payment_charge_id_cannot_be_credited_twice(make_group) -> None:
    """The guard behind duplicate-charge suppression in _commit_payment_once."""
    _, owner_id = await make_group()
    charge_id = f"charge-{secrets.token_hex(8)}"

    async with SessionFactory() as session:
        group = await session.scalar(select(Group).where(Group.owner_telegram_id == owner_id))
        assert group is not None
        session.add(
            Payment(
                user_telegram_id=owner_id,
                group_id=group.id,
                provider_payment_id=charge_id,
                amount=100,
                plan_code="standard",
                duration_days=30,
                status="paid",
            )
        )
        await session.commit()

    async with SessionFactory() as session:
        session.add(
            Payment(
                user_telegram_id=owner_id,
                group_id=group.id,
                provider_payment_id=charge_id,
                amount=100,
                plan_code="standard",
                duration_days=30,
                status="paid",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.db
async def test_daily_stat_upsert_target_is_unique(make_group) -> None:
    """mark_message does select-then-insert; this constraint is the real guard."""
    _, owner_id = await make_group()
    date = "2026-09-27"

    async with SessionFactory() as session:
        group = await session.scalar(select(Group).where(Group.owner_telegram_id == owner_id))
        assert group is not None
        session.add(
            DailyStat(
                group_id=group.id,
                user_telegram_id=owner_id,
                date=date,
                messages_count=1,
            )
        )
        await session.commit()

    async with SessionFactory() as session:
        group = await session.scalar(select(Group).where(Group.owner_telegram_id == owner_id))
        assert group is not None
        session.add(
            DailyStat(
                group_id=group.id,
                user_telegram_id=owner_id,
                date=date,
                messages_count=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.db
async def test_daily_report_claim_is_atomic_via_update_returning(make_group) -> None:
    """Mirrors _claim_daily_report: the second claim must find nothing."""
    _, owner_id = await make_group()
    today = "2026-09-27"

    async with SessionFactory() as session:
        group = await session.scalar(select(Group).where(Group.owner_telegram_id == owner_id))
        assert group is not None
        settings_id = group.settings.id

        claimed = await session.scalar(
            update(GroupSettings)
            .where(
                GroupSettings.id == settings_id,
                or_(
                    GroupSettings.last_report_date.is_(None),
                    GroupSettings.last_report_date != today,
                ),
            )
            .values(last_report_date=today)
            .returning(GroupSettings.id)
        )
        await session.commit()

    async with SessionFactory() as session:
        claimed_again = await session.scalar(
            update(GroupSettings)
            .where(
                GroupSettings.id == settings_id,
                or_(
                    GroupSettings.last_report_date.is_(None),
                    GroupSettings.last_report_date != today,
                ),
            )
            .values(last_report_date=today)
            .returning(GroupSettings.id)
        )
        await session.commit()

    assert claimed == settings_id
    assert claimed_again is None


@pytest.mark.db
async def test_row_lock_is_held_against_a_second_transaction(make_group) -> None:
    """Proves FOR UPDATE really serializes, using NOWAIT so the test cannot hang."""
    chat_id, _ = await make_group()

    async with SessionFactory() as holder:
        await holder.scalar(select(Group).where(Group.telegram_chat_id == chat_id).with_for_update())

        async with SessionFactory() as contender:
            with pytest.raises(DBAPIError):
                await contender.scalar(
                    select(Group)
                    .where(Group.telegram_chat_id == chat_id)
                    .with_for_update(nowait=True)
                )


@pytest.mark.db
async def test_skip_locked_skips_rows_held_by_another_transaction(make_group) -> None:
    """Proves FOR UPDATE SKIP LOCKED, used by the scheduled-message and reconcile claims."""
    chat_id, _ = await make_group()

    async with SessionFactory() as holder:
        await holder.scalar(select(Group).where(Group.telegram_chat_id == chat_id).with_for_update())

        async with SessionFactory() as worker:
            rows = (
                await worker.scalars(
                    select(Group).where(Group.telegram_chat_id == chat_id).with_for_update(skip_locked=True)
                )
            ).all()

    assert rows == []


@pytest.mark.db
def test_ensure_version_column_is_idempotent() -> None:
    """Runs the deploy-time fix twice; it must stay a no-op once widened."""
    ensure_version_column.main()
    ensure_version_column.main()
