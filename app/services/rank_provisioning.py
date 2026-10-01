from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.db.rank_models import RankAssignment
from app.db.rank_provisioning_models import RankProvisioningIntent
from app.db.session import SessionFactory
from app.services.ranks import (
    ADMIN_RANKS,
    HELPER,
    add_rank_event,
    can_assign_rank,
    can_remove_assignment,
    demote_telegram_admin,
    get_assignment,
    telegram_rights_for_rank,
)

TELEGRAM_MODE = "telegram"
BOT_ONLY_MODE = "bot_only"


async def _drop_intent(session: AsyncSession, intent: RankProvisioningIntent) -> None:
    await session.delete(intent)
    await session.commit()


async def _create_intent(
    session: AsyncSession,
    *,
    group: Group,
    actor_id: int,
    target_id: int,
    operation: str,
    telegram_action: str,
    desired_rank_code: str | None,
    desired_access_mode: str | None,
    payload: dict,
) -> RankProvisioningIntent | None:
    intent = RankProvisioningIntent(
        group_id=group.id,
        user_telegram_id=target_id,
        actor_telegram_id=actor_id,
        operation=operation,
        telegram_action=telegram_action,
        desired_rank_code=desired_rank_code,
        desired_access_mode=desired_access_mode,
        payload=payload,
    )
    session.add(intent)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(RankProvisioningIntent).where(
                RankProvisioningIntent.group_id == group.id,
                RankProvisioningIntent.user_telegram_id == target_id,
            )
        )
        if existing is not None:
            return None
        raise
    return intent


async def _finalize_intent(
    session: AsyncSession,
    intent: RankProvisioningIntent,
) -> RankAssignment | None:
    assignment = await get_assignment(
        session,
        intent.group_id,
        intent.user_telegram_id,
        active_only=False,
    )
    payload = dict(intent.payload or {})
    old_rank = payload.get("old_rank")

    if intent.operation == "remove":
        if assignment is not None:
            assignment.active = False
            assignment.permissions = {}
            assignment.helper_for_telegram_id = None
            assignment.telegram_admin_managed = False
        add_rank_event(
            session,
            group_id=intent.group_id,
            actor_id=intent.actor_telegram_id,
            target_id=intent.user_telegram_id,
            action="remove",
            old_rank=old_rank,
            new_rank=None,
            details={"provisioning_recovered": bool(payload.get("recovery"))},
        )
        await session.delete(intent)
        await session.commit()
        return assignment

    rank_code = intent.desired_rank_code
    access_mode = intent.desired_access_mode
    if rank_code is None or access_mode not in {TELEGRAM_MODE, BOT_ONLY_MODE}:
        raise RuntimeError("Invalid rank provisioning intent")

    if assignment is None:
        assignment = RankAssignment(
            group_id=intent.group_id,
            user_telegram_id=intent.user_telegram_id,
            rank_code=rank_code,
            permissions={},
            active=True,
            assigned_by_telegram_id=intent.actor_telegram_id,
            helper_for_telegram_id=intent.actor_telegram_id if rank_code == HELPER else None,
            access_mode=access_mode,
            telegram_admin_managed=bool(payload.get("managed", access_mode == TELEGRAM_MODE)),
        )
        session.add(assignment)
    else:
        assignment.rank_code = rank_code
        assignment.permissions = {}
        assignment.active = True
        assignment.assigned_by_telegram_id = intent.actor_telegram_id
        assignment.helper_for_telegram_id = intent.actor_telegram_id if rank_code == HELPER else None
        assignment.access_mode = access_mode
        assignment.telegram_admin_managed = bool(payload.get("managed", access_mode == TELEGRAM_MODE))

    add_rank_event(
        session,
        group_id=intent.group_id,
        actor_id=intent.actor_telegram_id,
        target_id=intent.user_telegram_id,
        action="assign" if old_rank is None else "change",
        old_rank=old_rank,
        new_rank=rank_code,
        details={
            "access_mode": access_mode,
            "old_access_mode": payload.get("old_access_mode"),
            "provisioning_recovered": bool(payload.get("recovery")),
        },
    )
    await session.delete(intent)
    await session.commit()
    return assignment


async def _execute_live_intent(
    bot: Bot,
    session: AsyncSession,
    *,
    intent_id: int,
) -> tuple[bool, str, RankAssignment | None]:
    group_id = await session.scalar(
        select(RankProvisioningIntent.group_id).where(RankProvisioningIntent.id == intent_id)
    )
    if group_id is None:
        return False, "Изменение ранга больше не ожидает выполнения.", None

    group = await session.scalar(
        select(Group).where(Group.id == group_id).with_for_update()
    )
    intent = await session.scalar(
        select(RankProvisioningIntent)
        .where(RankProvisioningIntent.id == intent_id)
        .with_for_update()
    )
    if group is None or intent is None:
        return False, "Группа или изменение ранга больше не доступны.", None
    if not group.is_active:
        await _drop_intent(session, intent)
        return False, "Группа неактивна. Изменение ранга отменено.", None

    if intent.operation == "remove":
        assignment = await get_assignment(
            session,
            intent.group_id,
            intent.user_telegram_id,
        )
        if assignment is None:
            await _drop_intent(session, intent)
            return False, "Ранг уже снят или больше не активен.", None
        allowed, reason = await can_remove_assignment(
            session,
            group,
            intent.actor_telegram_id,
            assignment,
        )
    else:
        rank_code = intent.desired_rank_code
        if rank_code is None or intent.desired_access_mode not in {TELEGRAM_MODE, BOT_ONLY_MODE}:
            await _drop_intent(session, intent)
            return False, "Изменение ранга содержит некорректные данные.", None
        if intent.user_telegram_id == group.owner_telegram_id:
            await _drop_intent(session, intent)
            return False, "Владельцу группы внутренний ранг не назначается.", None
        allowed, reason = await can_assign_rank(
            session,
            group,
            intent.actor_telegram_id,
            rank_code,
            target_id=intent.user_telegram_id,
        )

    if not allowed:
        await _drop_intent(session, intent)
        return False, reason or "Право на изменение ранга больше недоступно.", None

    if intent.telegram_action == "promote":
        rank_code = intent.desired_rank_code
        if rank_code is None:
            await _drop_intent(session, intent)
            return False, "Не указан ранг для Telegram-администратора.", None
        try:
            member = await bot.get_chat_member(group.telegram_chat_id, intent.user_telegram_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            await _drop_intent(session, intent)
            return False, "Не удалось найти участника в Telegram-группе.", None
        if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            await _drop_intent(session, intent)
            return False, "Пользователь должен состоять в группе.", None
        if member.status == ChatMemberStatus.CREATOR:
            await _drop_intent(session, intent)
            return False, "Владельцу Telegram-группы внутренний ранг не назначается.", None
        try:
            await bot.promote_chat_member(
                group.telegram_chat_id,
                intent.user_telegram_id,
                **telegram_rights_for_rank(rank_code),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            await _drop_intent(session, intent)
            return False, "Telegram не позволил применить права выбранного ранга.", None
    elif intent.telegram_action == "demote":
        if not await demote_telegram_admin(bot, group, intent.user_telegram_id):
            await _drop_intent(session, intent)
            return False, (
                "Не удалось снять Telegram-права администратора. "
                "Проверьте, что у Mimoru есть право назначать администраторов в группе."
            ), None
    elif intent.telegram_action != "none":
        await _drop_intent(session, intent)
        return False, "Неизвестное действие Telegram для изменения ранга.", None

    assignment = await _finalize_intent(session, intent)
    return True, "", assignment


async def provision_assignment(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target_id: int,
    rank_code: str,
    access_mode: str,
    *,
    force_bot_only_demotion: bool,
) -> tuple[bool, str, RankAssignment | None]:
    allowed, reason = await can_assign_rank(session, group, actor_id, rank_code, target_id=target_id)
    if not allowed:
        return False, reason, None
    if target_id == group.owner_telegram_id:
        return False, "Владельцу группы внутренний ранг не назначается.", None
    if access_mode not in {TELEGRAM_MODE, BOT_ONLY_MODE}:
        return False, "Неизвестный способ доступа.", None
    if access_mode == TELEGRAM_MODE and rank_code not in ADMIN_RANKS:
        return False, "Этот ранг работает только внутри Mimoru и не требует Telegram-админки.", None

    try:
        member = await bot.get_chat_member(group.telegram_chat_id, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, "Не удалось найти участника в Telegram-группе.", None
    if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return False, "Пользователь должен состоять в группе.", None
    if member.status == ChatMemberStatus.CREATOR:
        return False, "Владельцу Telegram-группы внутренний ранг не назначается.", None

    existing = await get_assignment(session, group.id, target_id, active_only=False)
    old_rank = existing.rank_code if existing and existing.active else None
    old_mode = getattr(existing, "access_mode", BOT_ONLY_MODE) if existing else None
    old_managed = bool(existing and existing.telegram_admin_managed)

    if access_mode == TELEGRAM_MODE:
        telegram_action = "promote"
        managed = True
    elif member.status == ChatMemberStatus.ADMINISTRATOR and (force_bot_only_demotion or old_managed):
        telegram_action = "demote"
        managed = False
    else:
        telegram_action = "none"
        managed = False

    intent = await _create_intent(
        session,
        group=group,
        actor_id=actor_id,
        target_id=target_id,
        operation="assign" if old_rank is None else "change",
        telegram_action=telegram_action,
        desired_rank_code=rank_code,
        desired_access_mode=access_mode,
        payload={
            "old_rank": old_rank,
            "old_access_mode": old_mode,
            "managed": managed,
        },
    )
    if intent is None:
        return False, "Для этого участника уже выполняется изменение ранга. Повторите позже.", None

    return await _execute_live_intent(bot, session, intent_id=intent.id)


async def remove_assignment(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    actor_id: int,
    assignment: RankAssignment,
) -> tuple[bool, str]:
    allowed, reason = await can_remove_assignment(session, group, actor_id, assignment)
    if not allowed:
        return False, reason

    telegram_action = "demote" if assignment.telegram_admin_managed else "none"
    intent = await _create_intent(
        session,
        group=group,
        actor_id=actor_id,
        target_id=assignment.user_telegram_id,
        operation="remove",
        telegram_action=telegram_action,
        desired_rank_code=None,
        desired_access_mode=None,
        payload={"old_rank": assignment.rank_code, "old_access_mode": assignment.access_mode},
    )
    if intent is None:
        return False, "Для этого участника уже выполняется изменение ранга. Повторите позже."

    ok, error, _ = await _execute_live_intent(bot, session, intent_id=intent.id)
    return ok, error


def _telegram_rights_match(member: object, rank_code: str) -> bool:
    if getattr(member, "status", None) != ChatMemberStatus.ADMINISTRATOR:
        return False
    for name, expected in telegram_rights_for_rank(rank_code).items():
        if bool(getattr(member, name, False)) != bool(expected):
            return False
    return True


async def _actor_can_recover_bot_only_intent(
    session: AsyncSession,
    group: Group,
    intent: RankProvisioningIntent,
) -> bool:
    """Revalidate current authority before applying or reconciling a persisted rank intent."""
    if intent.operation == "remove":
        assignment = await get_assignment(
            session,
            intent.group_id,
            intent.user_telegram_id,
        )
        if assignment is None:
            return False
        allowed, _ = await can_remove_assignment(
            session,
            group,
            intent.actor_telegram_id,
            assignment,
        )
        return allowed

    rank_code = intent.desired_rank_code
    if rank_code is None or intent.user_telegram_id == group.owner_telegram_id:
        return False
    allowed, _ = await can_assign_rank(
        session,
        group,
        intent.actor_telegram_id,
        rank_code,
        target_id=intent.user_telegram_id,
    )
    return allowed


async def _actor_can_recover_intent(
    session: AsyncSession,
    group: Group,
    intent: RankProvisioningIntent,
) -> bool:
    return await _actor_can_recover_bot_only_intent(session, group, intent)


async def recover_rank_provisioning_intents(bot: Bot) -> None:
    """Reconcile durable intents without replaying Telegram mutations.

    Each candidate is re-opened under the same Group serialization boundary used by
    live execution. Current actor authority must still hold before Telegram state is
    allowed to finalize an internal rank mutation.
    """
    log = structlog.get_logger()
    async with SessionFactory() as session:
        intent_ids = list((await session.scalars(
            select(RankProvisioningIntent.id).order_by(RankProvisioningIntent.created_at)
        )).all())

    for intent_id in intent_ids:
        async with SessionFactory() as session:
            group_id = await session.scalar(
                select(RankProvisioningIntent.group_id).where(RankProvisioningIntent.id == intent_id)
            )
            if group_id is None:
                continue

            group = await session.scalar(
                select(Group).where(Group.id == group_id).with_for_update()
            )
            intent = await session.scalar(
                select(RankProvisioningIntent)
                .where(RankProvisioningIntent.id == intent_id)
                .with_for_update()
            )
            if intent is None:
                continue
            if group is None or not group.is_active:
                await _drop_intent(session, intent)
                continue

            if intent.telegram_action == "none":
                if not await _actor_can_recover_bot_only_intent(session, group, intent):
                    await _drop_intent(session, intent)
                    continue
                payload = dict(intent.payload or {})
                payload["recovery"] = True
                intent.payload = payload
                await _finalize_intent(session, intent)
                continue

            if not await _actor_can_recover_intent(session, group, intent):
                await _drop_intent(session, intent)
                continue

            try:
                member = await bot.get_chat_member(group.telegram_chat_id, intent.user_telegram_id)
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                log.warning("rank_provisioning_recovery_lookup_failed", intent_id=intent.id, error=str(error))
                continue

            if intent.telegram_action == "promote":
                if intent.desired_rank_code and _telegram_rights_match(member, intent.desired_rank_code):
                    payload = dict(intent.payload or {})
                    payload["recovery"] = True
                    intent.payload = payload
                    await _finalize_intent(session, intent)
                elif member.status != ChatMemberStatus.ADMINISTRATOR:
                    await _drop_intent(session, intent)
                else:
                    log.warning("rank_provisioning_recovery_uncertain_admin", intent_id=intent.id)
            elif intent.telegram_action == "demote":
                if member.status != ChatMemberStatus.ADMINISTRATOR:
                    payload = dict(intent.payload or {})
                    payload["recovery"] = True
                    intent.payload = payload
                    await _finalize_intent(session, intent)
                else:
                    await _drop_intent(session, intent)
