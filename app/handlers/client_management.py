from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Group, Payment, PromoCode, PromoCodeUse, SupportTicket, User
from app.services.client_access import set_group_service_active
from app.services.promos import extend_plan, normalize_promo_code, promo_is_available
from app.services.ui import clean_ui_text

router = Router(name=__name__)
settings = get_settings()


def is_owner(user_id: int) -> bool:
    return user_id in settings.service_owner_ids


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^создать промокод [A-Za-z0-9_-]+ (free|trial|standard|pro) \d+д \d+$"))
async def create_promo(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    _, _, raw_code, plan_code, raw_days, raw_uses = message.text.split()
    code = normalize_promo_code(raw_code)
    exists = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if exists:
        await message.answer("Промокод уже существует.")
        return
    promo = PromoCode(
        code=code,
        plan_code=plan_code.casefold(),
        bonus_days=int(raw_days[:-1]),
        max_uses=int(raw_uses),
        created_by_telegram_id=message.from_user.id,
    )
    session.add(promo)
    await session.flush()
    await session.commit()
    await message.answer(f"✅ Промокод {code} создан. ID: {promo.id}")


@router.message(F.chat.type == "private", F.text.casefold() == "промокоды")
async def list_promos(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    rows = (await session.scalars(select(PromoCode).order_by(PromoCode.created_at.desc()).limit(50))).all()
    if not rows:
        await message.answer("Промокодов пока нет.")
        return
    lines = ["Промокоды"]
    for item in rows:
        status = "✅" if promo_is_available(active=item.active, expires_at=item.expires_at, current_uses=item.current_uses, max_uses=item.max_uses) else "❌"
        lines.append(f"{status} {item.code} — {item.plan_code}, {item.bonus_days}д, {item.current_uses}/{item.max_uses}")
    await message.answer("\n".join(lines))


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^отключить промокод [A-Za-z0-9_-]+$"))
async def disable_promo(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    code = normalize_promo_code(message.text.split()[-1])
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if promo is None:
        await message.answer("Промокод не найден.")
        return
    promo.active = False
    await message.answer(f"✅ Промокод {code} отключён.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^промокод [A-Za-z0-9_-]+ \d+$"))
async def redeem_promo(message: Message, session: AsyncSession) -> None:
    _, raw_code, raw_group_id = message.text.split()
    code = normalize_promo_code(raw_code)
    group = await session.scalar(
        select(Group)
        .where(
            Group.id == int(raw_group_id),
            Group.is_active.is_(True),
            Group.owner_telegram_id == message.from_user.id,
        )
        .with_for_update()
    )
    if group is None:
        await message.answer("Группа не найдена или вы не являетесь её владельцем.")
        return
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == code).with_for_update())
    if promo is None or not promo_is_available(active=promo.active, expires_at=promo.expires_at, current_uses=promo.current_uses, max_uses=promo.max_uses):
        await message.answer("Промокод недействителен или его лимит исчерпан.")
        return
    already = await session.scalar(select(PromoCodeUse.id).where(PromoCodeUse.promo_code_id == promo.id, PromoCodeUse.user_telegram_id == message.from_user.id))
    if already:
        await message.answer("Вы уже использовали этот промокод.")
        return
    group.plan_code = promo.plan_code
    group.plan_expires_at = extend_plan(group.plan_expires_at, promo.bonus_days)
    promo.current_uses += 1
    session.add(PromoCodeUse(promo_code_id=promo.id, user_telegram_id=message.from_user.id, group_id=group.id))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await message.answer("Промокод уже был использован.")
        return
    await session.commit()
    await message.answer(f"✅ Тариф {promo.plan_code} активирован до {group.plan_expires_at:%d.%m.%Y}.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^заблокировать клиента \d+$"))
async def block_client(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    telegram_id = int(message.text.split()[-1])
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        await message.answer("Клиент не найден.")
        return
    user.service_blocked = True
    groups = (await session.scalars(select(Group).where(Group.owner_telegram_id == telegram_id))).all()
    for group in groups:
        group.is_active = False
    await message.answer(f"✅ Клиент {telegram_id} заблокирован. Отключено групп: {len(groups)}.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^разблокировать клиента \d+$"))
async def unblock_client(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    telegram_id = int(message.text.split()[-1])
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        await message.answer("Клиент не найден.")
        return
    user.service_blocked = False
    await message.answer(f"✅ Клиент {telegram_id} разблокирован. Группы включаются отдельно.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^включить группу \d+$"))
async def enable_group(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    result = await set_group_service_active(
        session,
        group_id=int(message.text.split()[-1]),
        active=True,
    )
    if result.group is None:
        await message.answer("Группа не найдена.")
        return
    if result.blocked_owner:
        await message.answer("Сначала разблокируйте клиента-владельца группы.")
        return
    await message.answer(f"✅ Группа «{clean_ui_text(result.group.title)}» включена.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^тестовый период \d+ \d+д$"))
async def grant_trial(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    _, _, raw_group_id, raw_days = message.text.split()
    group = await session.get(Group, int(raw_group_id))
    if group is None:
        await message.answer("Группа не найдена.")
        return
    group.plan_code = "trial"
    group.plan_expires_at = extend_plan(group.plan_expires_at, int(raw_days[:-1]))
    await message.answer(f"✅ Тестовый период продлён до {group.plan_expires_at:%d.%m.%Y}.")


@router.message(F.chat.type == "private", F.text.casefold() == "клиенты")
async def clients(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    rows = (await session.execute(
        select(User.telegram_id, User.username, User.service_blocked, func.count(Group.id))
        .outerjoin(Group, Group.owner_telegram_id == User.telegram_id)
        .group_by(User.id)
        .order_by(func.count(Group.id).desc())
        .limit(50)
    )).all()
    lines = ["Клиенты"]
    for telegram_id, username, blocked, groups_count in rows:
        mark = "🚫" if blocked else "✅"
        lines.append(f"{mark} {telegram_id} @{username or '-'} — групп: {groups_count}")
    await message.answer("\n".join(lines) if len(lines) > 1 else "Клиентов пока нет.")


@router.message(F.chat.type == "private", F.text.casefold() == "расширенная статистика сервиса")
async def extended_stats(message: Message, session: AsyncSession) -> None:
    if not is_owner(message.from_user.id):
        return
    now = datetime.now(timezone.utc)
    users = await session.scalar(select(func.count()).select_from(User)) or 0
    blocked = await session.scalar(select(func.count()).select_from(User).where(User.service_blocked.is_(True))) or 0
    groups = await session.scalar(select(func.count()).select_from(Group)) or 0
    active_groups = await session.scalar(select(func.count()).select_from(Group).where(Group.is_active.is_(True))) or 0
    paid_groups = await session.scalar(select(func.count()).select_from(Group).where(Group.plan_code.in_(["standard", "pro"]), Group.plan_expires_at > now)) or 0
    revenue_rub = await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.currency == "RUB")) or 0
    open_tickets = await session.scalar(select(func.count()).select_from(SupportTicket).where(SupportTicket.status == "new")) or 0
    await message.answer(
        "Статистика сервиса\n"
        f"Клиентов: {users}\nЗаблокировано: {blocked}\n"
        f"Групп: {groups}\nАктивных: {active_groups}\nПлатных: {paid_groups}\n"
        f"Получено за тарифы (₽): {revenue_rub}\nОткрытых обращений: {open_tickets}"
    )
