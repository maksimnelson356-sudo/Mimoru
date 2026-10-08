"""Общие правила подачи жалоб.

Правило одно: на одно сообщение допускается не более одной жалобы.
Ключ — пара (group_id, message_id), потому что message_id уникален
внутри чата. Ограничение не зависит от автора и от статуса ранее
созданной жалобы: повторная жалоба от того же или другого участника,
а также жалоба на уже рассмотренное сообщение, не создаются заново.

На уровне БД то же правило закреплено уникальным ограничением
uq_complaints_group_message (см. app/db/models.py и миграцию
c7f4a1e2b9d3), поэтому проверка ниже — быстрый путь для пользователя,
а уникальный индекс защищает от гонки двух одновременных жалоб.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Complaint

#: Ответ на повторную жалобу. Формулировка обязана отличаться от принятой
#: жалобы («✅ Жалоба принята …»), иначе повтор читается как новая жалоба.
COMPLAINT_DUPLICATE_TEXT = "⚠️ На это сообщение жалоба уже отправлена."


async def complaint_exists_for_message(
    session: AsyncSession,
    *,
    group_id: int,
    message_id: int,
) -> bool:
    """True, если на сообщение уже есть жалоба в этой группе."""
    existing_id = await session.scalar(
        select(Complaint.id).where(
            Complaint.group_id == group_id,
            Complaint.message_id == message_id,
        )
    )
    return existing_id is not None