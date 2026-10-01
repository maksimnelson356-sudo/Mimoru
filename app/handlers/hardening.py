from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AllowedLink, Complaint, Group
from app.services.content import normalize_domain
from app.services.owner_management import managed_group_for_message
from app.services.ui import clean_ui_text

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def managed_group(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    return await managed_group_for_message(
        message,
        bot,
        session,
        denial_text="Изменять эти настройки может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^разрешить ссылку .+"))
async def allow_link(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    try:
        domain = normalize_domain(message.text.split(maxsplit=2)[2])
    except ValueError:
        await message.reply("Укажите домен, например: разрешить ссылку example.com")
        return
    session.add(AllowedLink(group_id=group.id, domain=domain, created_by_telegram_id=message.from_user.id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.reply("Этот домен уже находится в белом списке.")
        return
    await message.reply(f"✅ Разрешён домен: {clean_ui_text(domain)}")


@router.message(F.text.regexp(r"(?i)^запретить ссылку .+"))
async def disallow_link(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group:
        return
    try:
        domain = normalize_domain(message.text.split(maxsplit=2)[2])
    except ValueError:
        await message.reply("Укажите корректный домен.")
        return
    row = await session.scalar(select(AllowedLink).where(AllowedLink.group_id == group.id, AllowedLink.domain == domain))
    if row is None:
        await message.reply("Этого домена нет в белом списке.")
        return
    await session.delete(row)
    await session.commit()
    await message.reply(f"✅ Домен удалён из белого списка: {clean_ui_text(domain)}")


@router.message(F.text.casefold().in_({"разрешенные ссылки", "разрешённые ссылки", "белый список ссылок"}))
async def allowed_links(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(AllowedLink.domain).where(AllowedLink.group_id == group.id).order_by(AllowedLink.domain))).all()
    text = "\n".join(f"• {clean_ui_text(domain)}" for domain in rows) if rows else "Список пуст."
    await message.reply("Разрешённые ссылки\n" + text)


@router.message(F.text.casefold() == "жалобы")
async def complaints(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(Complaint).where(Complaint.group_id == group.id, Complaint.status == "pending").order_by(Complaint.created_at.desc()).limit(30))).all()
    if not rows:
        await message.reply("Новых жалоб нет.")
        return
    lines = [f"#{row.id} · на {row.target_telegram_id} · от {row.reporter_telegram_id}" for row in rows]
    await message.reply("Новые жалобы\n" + "\n".join(lines) + "\n\nКоманда: закрыть жалобу ID причина")


@router.message(F.text.regexp(r"(?is)^закрыть жалобу \d+(?: .+)?$"))
async def resolve_complaint(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    parts = message.text.split(maxsplit=3)
    complaint_id = int(parts[2])
    resolution = clean_ui_text(parts[3][:1000]) if len(parts) > 3 else "Рассмотрено модератором"
    row = await session.get(Complaint, complaint_id)
    if row is None or row.group_id != group.id:
        await message.reply("Жалоба не найдена в этой группе.")
        return
    row.status = "resolved"
    row.reviewed_by_telegram_id = message.from_user.id
    row.resolution = resolution
    row.reviewed_at = datetime.now(timezone.utc)
    await session.commit()
    await message.reply(f"✅ Жалоба #{row.id} закрыта.")
