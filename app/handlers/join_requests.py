from __future__ import annotations

from datetime import datetime, timezone
from html import escape

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.invite_operation_models import InviteOperation
from app.db.models import Group, InviteCampaign, JoinRequestRecord
from app.services.join_request_transitions import (
    CREATE_IN_PROGRESS,
    CREATE_UNCERTAIN,
    PROCESSING_APPROVE,
    REVOKE_IN_PROGRESS,
    REVOKE_UNCERTAIN,
    begin_invite_creation,
    begin_invite_revocation,
    claim_join_review,
    finalize_invite_creation,
    finalize_invite_revocation,
    finalize_join_review,
    mark_invite_creation_compensation,
    mark_invite_revocation_uncertain,
    release_failed_join_review,
)
from app.services.join_requests import join_request_status_label, parse_invite_command
from app.services.owner_management import managed_group_for_message

router = Router(name=__name__)


async def managed_group(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or not message.from_user:
        return None
    return await managed_group_for_message(
        message,
        bot,
        session,
        denial_text="Изменять эти настройки может владелец или администратор группы.",
        for_update=for_update,
    )


async def abort_invite_operation(session: AsyncSession, operation_id: int) -> None:
    operation = await session.scalar(
        select(InviteOperation).where(InviteOperation.id == operation_id).with_for_update()
    )
    if operation is not None and operation.status in {
        CREATE_IN_PROGRESS,
        REVOKE_IN_PROGRESS,
        REVOKE_UNCERTAIN,
    }:
        operation.status = "failed"
        operation.error_text = "authority changed before Telegram side effect"
    await session.commit()


@router.message(F.text.regexp(r"(?i)^создать ссылку(?:-заявку| заявку)? .+"))
async def create_invite(message: Message, bot: Bot, session: AsyncSession) -> None:
    parsed = parse_invite_command(message.text or "")
    if not parsed or not message.from_user:
        return
    group = await managed_group(message, bot, session, for_update=True)
    if not group:
        return
    operation = await begin_invite_creation(
        session,
        group=group,
        actor_id=message.from_user.id,
        name=parsed.name,
        creates_join_request=parsed.creates_join_request,
    )
    if operation is None:
        await message.reply(
            "Ссылка с таким названием уже существует либо предыдущая попытка создания требует проверки."
        )
        return

    # begin_invite_creation commits crash-safe intent and therefore releases the
    # ownership lock. Reauthorize immediately before the Telegram side effect and
    # keep the reacquired Group lock until finalization commits.
    group = await managed_group(message, bot, session, for_update=True)
    if group is None:
        await abort_invite_operation(session, operation.id)
        return
    try:
        link = await bot.create_chat_invite_link(
            chat_id=message.chat.id,
            name=parsed.name,
            creates_join_request=parsed.creates_join_request,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        stored = await session.get(InviteOperation, operation.id)
        if stored is not None:
            stored.status = "failed"
            stored.error_text = str(error)[:1000]
            await session.commit()
        await message.reply(f"Не удалось создать ссылку: {escape(str(error))}")
        return

    try:
        campaign = await finalize_invite_creation(
            session,
            operation_id=operation.id,
            invite_link=link.invite_link,
        )
    except IntegrityError as error:
        await session.rollback()
        compensated = False
        compensation_error = str(error)
        try:
            await bot.revoke_chat_invite_link(message.chat.id, link.invite_link)
            compensated = True
        except (TelegramBadRequest, TelegramForbiddenError) as revoke_error:
            compensation_error = f"{error}; revoke: {revoke_error}"
        await mark_invite_creation_compensation(
            session,
            operation.id,
            invite_link=link.invite_link,
            compensated=compensated,
            error_text=None if compensated else compensation_error,
        )
        await message.reply(
            "Не удалось сохранить новую ссылку. Telegram-ссылка была отозвана."
            if compensated
            else "Не удалось надёжно сохранить или отозвать новую ссылку; операция помечена как неопределённая."
        )
        return
    if campaign is None:
        await message.reply("Ссылка создана в Telegram, но её фиксация в Mimoru требует проверки.")
        return
    mode = "с заявкой" if parsed.creates_join_request else "обычная"
    await message.reply(f"✅ Создана {mode} ссылка «{escape(parsed.name)}»:\n{link.invite_link}")


@router.message(F.text.casefold().in_({"ссылки приглашений", "источники входа", "пригласительные ссылки"}))
async def invite_list(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session)
    if not group:
        return
    rows = list((await session.scalars(select(InviteCampaign).where(
        InviteCampaign.group_id == group.id,
        InviteCampaign.active.is_(True),
    ).order_by(InviteCampaign.created_at.desc()).limit(30))).all())
    open_ops = list((await session.scalars(select(InviteOperation).where(
        InviteOperation.group_id == group.id,
        InviteOperation.status.in_([CREATE_UNCERTAIN, REVOKE_IN_PROGRESS, REVOKE_UNCERTAIN]),
    ).order_by(InviteOperation.created_at.desc()).limit(20))).all())
    if not rows and not open_ops:
        await message.reply("Отслеживаемых ссылок пока нет.")
        return
    lines = ["<b>Источники входа</b>"]
    for row in rows:
        lines.append(
            f"• <b>{escape(row.name)}</b> — входов: {row.joined_count}, заявок: {row.requested_count}\n"
            f"  {row.invite_link}"
        )
    for operation in open_ops:
        if operation.status == CREATE_UNCERTAIN:
            lines.append(
                f"⚠️ Создание «{escape(operation.name or 'без имени')}» прервалось. "
                "Telegram мог создать ссылку, URL которой Mimoru не успела получить. Проверьте ссылки группы вручную."
            )
        else:
            lines.append(
                f"⚠️ Отзыв ссылки #{operation.campaign_id or '—'} не подтверждён. "
                "Повторите команду отключения, чтобы безопасно повторить отзыв."
            )
    await message.reply("\n".join(lines))


@router.message(F.text.regexp(r"(?i)^отключить ссылку \d+$"))
async def disable_invite(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group or message.from_user is None:
        return
    campaign_id = int((message.text or "").split()[-1])
    row = await session.scalar(select(InviteCampaign).where(
        InviteCampaign.id == campaign_id,
        InviteCampaign.group_id == group.id,
        InviteCampaign.active.is_(True),
    ))
    if row is None:
        await message.reply("Активная ссылка с таким ID не найдена.")
        return
    invite_link = row.invite_link

    operation = await session.scalar(
        select(InviteOperation)
        .where(
            InviteOperation.campaign_id == row.id,
            InviteOperation.operation == "revoke",
            InviteOperation.status == REVOKE_UNCERTAIN,
        )
        .with_for_update()
    )
    if operation is not None:
        operation.status = REVOKE_IN_PROGRESS
        operation.actor_telegram_id = message.from_user.id
        operation.error_text = None
        await session.commit()
    else:
        operation = await begin_invite_revocation(
            session,
            campaign=row,
            actor_id=message.from_user.id,
        )
    if operation is None:
        await message.reply("Отзыв этой ссылки уже выполняется.")
        return

    group = await managed_group(message, bot, session, for_update=True)
    if group is None:
        await abort_invite_operation(session, operation.id)
        return
    try:
        await bot.revoke_chat_invite_link(message.chat.id, invite_link)
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        await mark_invite_revocation_uncertain(session, operation.id, str(error))
        structlog.get_logger().warning(
            "invite_revoke_uncertain",
            campaign_id=row.id,
            chat_id=message.chat.id,
            error=str(error),
        )
        await message.reply(
            "Telegram не подтвердил отзыв ссылки. Mimoru сохранила операцию как неопределённую; повторите команду позже."
        )
        return
    await finalize_invite_revocation(session, operation.id)
    await message.reply(f"✅ Ссылка #{row.id} отключена.")


@router.message(F.text.regexp(r"(?i)^заявки (вкл|выкл)$"))
async def toggle_requests(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group:
        return
    group.settings.join_requests_enabled = (message.text or "").casefold().endswith("вкл")
    await session.commit()
    await message.reply("✅ Учёт заявок включён." if group.settings.join_requests_enabled else "❌ Учёт заявок выключен.")


@router.message(F.text.regexp(r"(?i)^заявки авто (вкл|выкл)$"))
async def toggle_auto_approve(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group:
        return
    group.settings.join_requests_auto_approve = (message.text or "").casefold().endswith("вкл")
    await session.commit()
    await message.reply("✅ Автоодобрение заявок включено." if group.settings.join_requests_auto_approve else "❌ Автоодобрение заявок выключено.")


@router.message(F.text.casefold().in_({"заявки", "заявки на вступление"}))
async def pending_requests(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed_group(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(JoinRequestRecord).where(
        JoinRequestRecord.group_id == group.id,
        JoinRequestRecord.status == "pending",
    ).order_by(JoinRequestRecord.requested_at.desc()).limit(30))).all()
    uncertain = int(await session.scalar(select(JoinRequestRecord.id).where(
        JoinRequestRecord.group_id == group.id,
        JoinRequestRecord.status == "review_uncertain",
    ).limit(1)) or 0)
    if not rows:
        suffix = " Есть заявка с неопределённым результатом после прерванной обработки." if uncertain else ""
        await message.reply("Ожидающих заявок нет." + suffix)
        return
    text = ["<b>Ожидающие заявки</b>"]
    for row in rows:
        handle = f" (@{row.username})" if row.username else ""
        text.append(f"#{row.id} — {escape(row.first_name)}{handle}")
    if uncertain:
        text.append("\n⚠️ Есть заявки с неопределённым результатом после прерванной обработки; они не повторяются автоматически.")
    text.append("\nКоманды: <code>одобрить заявку ID</code> или <code>отклонить заявку ID</code>.")
    await message.reply("\n".join(text))


async def review_request(message: Message, bot: Bot, session: AsyncSession, approve: bool) -> None:
    group = await managed_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    request_id = int((message.text or "").split()[-1])
    row = await claim_join_review(
        session,
        request_id=request_id,
        group_id=group.id,
        actor_id=message.from_user.id,
        approve=approve,
    )
    if row is None:
        await message.reply("Ожидающая заявка с таким ID не найдена или уже обрабатывается.")
        return

    group = await managed_group(message, bot, session, for_update=True)
    if group is None:
        await release_failed_join_review(session, row.id, approve=approve)
        return
    try:
        if approve:
            await bot.approve_chat_join_request(message.chat.id, row.user_telegram_id)
        else:
            await bot.decline_chat_join_request(message.chat.id, row.user_telegram_id)
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        await release_failed_join_review(session, row.id, approve=approve)
        await message.reply(f"Не удалось обработать заявку: {escape(str(error))}")
        return
    finalized = await finalize_join_review(session, row.id, approve=approve)
    status = finalized.status if finalized is not None else ("approved" if approve else "declined")
    await message.reply(f"✅ Заявка #{row.id}: {join_request_status_label(status)}.")


@router.message(F.text.regexp(r"(?i)^одобрить заявку \d+$"))
async def approve_request(message: Message, bot: Bot, session: AsyncSession) -> None:
    await review_request(message, bot, session, True)


@router.message(F.text.regexp(r"(?i)^отклонить заявку \d+$"))
async def decline_request(message: Message, bot: Bot, session: AsyncSession) -> None:
    await review_request(message, bot, session, False)


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, bot: Bot, session: AsyncSession) -> None:
    group = await session.scalar(select(Group).where(Group.telegram_chat_id == request.chat.id))
    if group is None or not group.is_active or not group.settings.join_requests_enabled:
        return
    campaign = None
    if request.invite_link:
        campaign = await session.scalar(select(InviteCampaign).where(
            InviteCampaign.group_id == group.id,
            InviteCampaign.invite_link == request.invite_link.invite_link,
        ))
        if campaign:
            campaign.requested_count += 1
    row = JoinRequestRecord(
        group_id=group.id,
        campaign_id=campaign.id if campaign else None,
        user_telegram_id=request.from_user.id,
        user_chat_id=request.user_chat_id,
        username=request.from_user.username,
        first_name=request.from_user.first_name or "",
        bio=request.bio,
        status=PROCESSING_APPROVE if group.settings.join_requests_auto_approve else "pending",
        reviewed_at=datetime.now(timezone.utc) if group.settings.join_requests_auto_approve else None,
        reviewed_by_telegram_id=0 if group.settings.join_requests_auto_approve else None,
    )
    session.add(row)
    await session.commit()

    if group.settings.join_requests_auto_approve:
        try:
            await request.approve()
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            await release_failed_join_review(session, row.id, approve=True)
            structlog.get_logger().warning(
                "join_request_auto_approve_failed",
                chat_id=request.chat.id,
                user_id=request.from_user.id,
                error=str(error),
            )
        else:
            await finalize_join_review(session, row.id, approve=True)

    row = await session.get(JoinRequestRecord, row.id)
    if group.owner_telegram_id and row is not None:
        source = campaign.name if campaign else "неизвестный источник"
        try:
            await bot.send_message(
                group.owner_telegram_id,
                f"📥 Новая заявка в <b>{escape(group.title)}</b>\n"
                f"Пользователь: {request.from_user.mention_html()}\n"
                f"Источник: {escape(source)}\n"
                f"ID заявки: <code>{row.id}</code>\n"
                f"Статус: {join_request_status_label(row.status)}",
            )
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            structlog.get_logger().warning(
                "join_request_owner_notification_failed",
                group_id=group.id,
                owner_id=group.owner_telegram_id,
                error=str(error),
            )


@router.chat_member()
async def count_campaign_joins(event: ChatMemberUpdated, session: AsyncSession) -> None:
    if not event.invite_link:
        return
    old_status = event.old_chat_member.status.value
    new_status = event.new_chat_member.status.value
    if old_status not in {"left", "kicked"} or new_status not in {"member", "restricted", "administrator"}:
        return
    group = await session.scalar(select(Group).where(Group.telegram_chat_id == event.chat.id))
    if not group:
        return
    campaign = await session.scalar(select(InviteCampaign).where(
        InviteCampaign.group_id == group.id,
        InviteCampaign.invite_link == event.invite_link.invite_link,
        InviteCampaign.active.is_(True),
    ))
    if campaign and not event.new_chat_member.user.is_bot:
        campaign.joined_count += 1