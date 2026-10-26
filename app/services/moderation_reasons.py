from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GroupSettings, ModerationReason

DEFAULT_REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Флуд", ("warn", "mute", "ban")),
    ("Оскорбление участников", ("warn", "mute", "ban")),
    ("Спам", ("warn", "mute", "ban")),
    ("Реклама", ("warn", "mute", "ban")),
    ("Нецензурная лексика", ("warn", "mute")),
    ("Без пошлостей", ("warn", "mute")),
    ("Нарушение правил", ("warn", "mute", "ban")),
)


def normalize_actions(actions: list[str] | tuple[str, ...] | None) -> list[str]:
    allowed = {"warn", "mute", "ban"}
    result: list[str] = []
    for action in actions or []:
        if action in allowed and action not in result:
            result.append(action)
    return result


async def ensure_default_reasons(session: AsyncSession, group_id: int) -> None:
    settings = await session.scalar(select(GroupSettings).where(GroupSettings.group_id == group_id))
    if settings is not None and settings.moderation_reasons_initialized:
        return
    count = int(
        await session.scalar(
            select(func.count()).select_from(ModerationReason).where(
                ModerationReason.group_id == group_id
            )
        )
        or 0
    )
    if count == 0:
        for order, (name, actions) in enumerate(DEFAULT_REASONS, 10):
            session.add(
                ModerationReason(
                    group_id=group_id,
                    name=name,
                    actions=list(actions),
                    active=True,
                    sort_order=order,
                )
            )
    if settings is not None:
        settings.moderation_reasons_initialized = True
    await session.flush()


async def active_reasons(
    session: AsyncSession, group_id: int, action: str
) -> list[ModerationReason]:
    await ensure_default_reasons(session, group_id)
    rows = (
        await session.scalars(
            select(ModerationReason)
            .where(
                ModerationReason.group_id == group_id,
                ModerationReason.active.is_(True),
            )
            .order_by(ModerationReason.sort_order, ModerationReason.id)
        )
    ).all()
    return [row for row in rows if action in normalize_actions(row.actions)]
