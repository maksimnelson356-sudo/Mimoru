from __future__ import annotations

from sqlalchemy import select

from app.db.models import AutomationLog, Group, Warning
from app.db.session import SessionFactory
from app.services.safety import warning_expiry_cutoff


async def expire_warnings() -> None:
    """Expire warnings only after revalidating current automation settings."""
    async with SessionFactory() as session:
        candidate_ids = list((await session.scalars(
            select(Group.id).where(Group.is_active.is_(True)).order_by(Group.id)
        )).all())

    for group_id in candidate_ids:
        async with SessionFactory() as session:
            group = await session.scalar(
                select(Group)
                .where(Group.id == group_id, Group.is_active.is_(True))
                .with_for_update()
            )
            if group is None or not group.settings.automation_enabled:
                continue

            hours = group.settings.warning_expire_hours
            cutoff = warning_expiry_cutoff(hours)
            if cutoff is None:
                continue

            rows = list((await session.scalars(
                select(Warning)
                .where(
                    Warning.group_id == group.id,
                    Warning.active.is_(True),
                    Warning.created_at < cutoff,
                )
                .with_for_update()
            )).all())
            for row in rows:
                row.active = False
            if rows:
                session.add(AutomationLog(
                    group_id=group.id,
                    rule_code="warning_expiry",
                    status="ok",
                    details={"expired": len(rows), "hours": hours},
                ))
            await session.commit()
