from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog, Punishment, Warning
from app.services.access import can_moderate
from app.services.ranks import (
    ADMIN_RANKS,
    add_rank_event,
    can_moderate_target,
    demote_telegram_admin,
    get_assignment,
    restore_telegram_rank,
)
from app.services.repositories import active_warnings_count
from app.services.ui import manual_action_notice

MUTED = ChatPermissions(can_send_messages=False)
UNMUTED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


class ModerationOutcome(str):
    """String-compatible result with explicit moderation execution semantics."""

    success: bool
    commit: bool
    public_notice: bool

    def __new__(
        cls,
        text: str,
        *,
        success: bool,
        commit: bool,
        public_notice: bool,
    ) -> "ModerationOutcome":
        obj = super().__new__(cls, text)
        obj.success = success
        obj.commit = commit
        obj.public_notice = public_notice
        return obj


def _success(
    text: str, *, commit: bool = True, public_notice: bool = True
) -> ModerationOutcome:
    return ModerationOutcome(
        text, success=True, commit=commit, public_notice=public_notice
    )


def _failure(text: str) -> ModerationOutcome:
    return ModerationOutcome(text, success=False, commit=False, public_notice=False)


def _partial(text: str) -> ModerationOutcome:
    return ModerationOutcome(text, success=False, commit=True, public_notice=True)


def log_action(
    session: AsyncSession,
    group_id: int,
    actor_id: int,
    target_id: int,
    action: str,
    reason: str | None = None,
    metadata: dict | None = None,
) -> None:
    session.add(
        ModerationLog(
            group_id=group_id,
            actor_telegram_id=actor_id,
            target_telegram_id=target_id,
            action=action,
            reason=reason,
            metadata_json=metadata,
        )
    )


async def deactivate_punishments(
    session: AsyncSession,
    group_id: int,
    target_id: int,
    kind: str,
) -> None:
    rows = (
        await session.scalars(
            select(Punishment).where(
                Punishment.group_id == group_id,
                Punishment.user_telegram_id == target_id,
                Punishment.kind == kind,
                Punishment.active.is_(True),
            )
        )
    ).all()
    for row in rows:
        row.active = False


async def _prepare_ranked_admin_punishment(bot: Bot, group: Group, assignment) -> bool:
    if assignment is None or assignment.rank_code not in ADMIN_RANKS:
        return True
    return await demote_telegram_admin(bot, group, assignment.user_telegram_id)


async def _restore_after_failed_action(bot: Bot, group: Group, assignment) -> None:
    if assignment is None or assignment.rank_code not in ADMIN_RANKS:
        return
    try:
        await restore_telegram_rank(bot, group, assignment)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def _unmanaged_telegram_admin(
    bot: Bot,
    group: Group,
    target_id: int,
    target_assignment,
) -> bool:
    if target_assignment is not None and target_assignment.rank_code in ADMIN_RANKS:
        return False
    try:
        member = await bot.get_chat_member(group.telegram_chat_id, target_id)
    except TelegramForbiddenError:
        return True
    except TelegramBadRequest:
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def execute(
    *,
    bot: Bot,
    session: AsyncSession,
    chat_id: int,
    group_id: int,
    target_id: int,
    moderator_id: int,
    action: str,
    duration: int | None,
    reason: str,
    warnings_limit: int,
    default_mute: int,
    target_name: str,
    moderator_name: str,
    actor_role: str = "admin",
) -> ModerationOutcome:
    group = await session.scalar(
        select(Group).where(Group.id == group_id).with_for_update()
    )
    if group is None or not group.is_active:
        return _failure("Группа больше не обслуживается Mimoru.")
    if not await can_moderate(bot, session, group, moderator_id, action):
        return _failure("Право на это действие больше недоступно.")
    allowed, denial = await can_moderate_target(session, group, moderator_id, target_id)
    if not allowed:
        return _failure(denial)
    target_assignment = await get_assignment(session, group_id, target_id)
    if action in {"ban", "mute", "kick", "warn"} and await _unmanaged_telegram_admin(
        bot, group, target_id, target_assignment
    ):
        return _failure(
            "Этот пользователь является Telegram-владельцем или администратором, "
            "который не управляется рангами Mimoru. Сначала синхронизируйте его роль."
        )
    target_is_admin_rank = bool(
        target_assignment and target_assignment.rank_code in ADMIN_RANKS
    )
    now = datetime.now(timezone.utc)
    safe_reason = reason.strip() or "Не указана"

    if action in {"ban", "mute"}:
        existing = await session.scalar(
            select(Punishment.id)
            .where(
                Punishment.group_id == group_id,
                Punishment.user_telegram_id == target_id,
                Punishment.kind == action,
                Punishment.active.is_(True),
            )
            .limit(1)
        )
        if existing is not None:
            return _failure(f"У пользователя уже есть активное наказание ({action}).")

    if action == "ban":
        if not await _prepare_ranked_admin_punishment(bot, group, target_assignment):
            return _failure(
                "Telegram не позволил временно снять admin-права нижестоящего администратора. Проверьте право Mimoru назначать администраторов."
            )
        until = now + timedelta(seconds=duration) if duration else None
        try:
            await bot.ban_chat_member(
                chat_id, target_id, until_date=int(until.timestamp()) if until else None
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            await _restore_after_failed_action(bot, group, target_assignment)
            return _failure(
                "Telegram не позволил забанить пользователя. Проверьте права Mimoru и состояние участника."
            )
        session.add(
            Punishment(
                group_id=group_id,
                user_telegram_id=target_id,
                moderator_telegram_id=moderator_id,
                kind="ban",
                reason=safe_reason,
                ends_at=until,
            )
        )
        if target_is_admin_rank and target_assignment is not None:
            old_rank = target_assignment.rank_code
            target_assignment.active = False
            target_assignment.telegram_admin_managed = False
            target_assignment.restore_after_mute = False
            add_rank_event(
                session,
                group_id=group_id,
                actor_id=moderator_id,
                target_id=target_id,
                action="remove_by_ban",
                old_rank=old_rank,
                new_rank=None,
                details={"reason": safe_reason},
            )
        log_action(
            session,
            group_id,
            moderator_id,
            target_id,
            "ban",
            safe_reason,
            {"duration": duration},
        )
        return _success(
            manual_action_notice(
                action="ban",
                target=target_name,
                moderator=moderator_name,
                reason=safe_reason,
                duration_seconds=duration,
                actor_role=actor_role,
            )
        )

    if action == "unban":
        try:
            await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except (TelegramBadRequest, TelegramForbiddenError):
            return _failure(
                "Telegram не позволил разблокировать пользователя. Проверьте права Mimoru."
            )
        await deactivate_punishments(session, group_id, target_id, "ban")
        log_action(session, group_id, moderator_id, target_id, "unban", safe_reason)
        return _success(
            manual_action_notice(
                action="unban",
                target=target_name,
                moderator=moderator_name,
                reason=safe_reason,
                actor_role=actor_role,
            )
        )

    if action == "mute":
        if not await _prepare_ranked_admin_punishment(bot, group, target_assignment):
            return _failure(
                "Telegram не позволил временно снять admin-права нижестоящего администратора. Проверьте право Mimoru назначать администраторов."
            )
        seconds = duration or default_mute
        until = now + timedelta(seconds=seconds)
        try:
            await bot.restrict_chat_member(
                chat_id, target_id, permissions=MUTED, until_date=int(until.timestamp())
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            await _restore_after_failed_action(bot, group, target_assignment)
            return _failure(
                "Telegram не позволил выдать мут. Проверьте права Mimoru и состояние участника."
            )
        if target_is_admin_rank and target_assignment is not None:
            target_assignment.restore_after_mute = True
        session.add(
            Punishment(
                group_id=group_id,
                user_telegram_id=target_id,
                moderator_telegram_id=moderator_id,
                kind="mute",
                reason=safe_reason,
                ends_at=until,
            )
        )
        log_action(
            session,
            group_id,
            moderator_id,
            target_id,
            "mute",
            safe_reason,
            {
                "duration": seconds,
                "restore_rank": (
                    target_assignment.rank_code
                    if target_is_admin_rank and target_assignment
                    else None
                ),
            },
        )
        return _success(
            manual_action_notice(
                action="mute",
                target=target_name,
                moderator=moderator_name,
                reason=safe_reason,
                duration_seconds=seconds,
                actor_role=actor_role,
            )
        )

    if action == "unmute":
        try:
            await bot.restrict_chat_member(chat_id, target_id, permissions=UNMUTED)
        except (TelegramBadRequest, TelegramForbiddenError):
            return _failure(
                "Telegram не позволил снять мут. Проверьте права Mimoru и состояние участника."
            )
        await deactivate_punishments(session, group_id, target_id, "mute")
        if target_assignment is not None and target_assignment.rank_code in ADMIN_RANKS:
            if await restore_telegram_rank(bot, group, target_assignment):
                target_assignment.restore_after_mute = False
        log_action(session, group_id, moderator_id, target_id, "unmute", safe_reason)
        return _success(
            manual_action_notice(
                action="unmute",
                target=target_name,
                moderator=moderator_name,
                reason=safe_reason,
                actor_role=actor_role,
            )
        )

    if action == "kick":
        if not await _prepare_ranked_admin_punishment(bot, group, target_assignment):
            return _failure(
                "Telegram не позволил снять admin-права нижестоящего администратора. Проверьте право Mimoru назначать администраторов."
            )
        try:
            await bot.ban_chat_member(chat_id, target_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            await _restore_after_failed_action(bot, group, target_assignment)
            return _failure(
                "Telegram не позволил исключить пользователя. Проверьте права Mimoru и состояние участника."
            )
        try:
            await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except (TelegramBadRequest, TelegramForbiddenError):
            session.add(
                Punishment(
                    group_id=group_id,
                    user_telegram_id=target_id,
                    moderator_telegram_id=moderator_id,
                    kind="ban",
                    reason="Не удалось завершить кик: пользователь остался заблокирован",
                    ends_at=None,
                )
            )
            if target_is_admin_rank and target_assignment is not None:
                old_rank = target_assignment.rank_code
                target_assignment.active = False
                target_assignment.telegram_admin_managed = False
                target_assignment.restore_after_mute = False
                add_rank_event(
                    session,
                    group_id=group_id,
                    actor_id=moderator_id,
                    target_id=target_id,
                    action="remove_by_ban",
                    old_rank=old_rank,
                    new_rank=None,
                    details={"reason": "Сбой завершения кика"},
                )
            log_action(
                session,
                group_id,
                moderator_id,
                target_id,
                "kick_failed_ban",
                safe_reason,
            )
            return _partial(
                "Telegram заблокировал пользователя, но не смог сразу разблокировать после кика. Пользователь оставлен в бане и записан в журнал."
            )
        if target_is_admin_rank and target_assignment is not None:
            old_rank = target_assignment.rank_code
            target_assignment.active = False
            target_assignment.telegram_admin_managed = False
            target_assignment.restore_after_mute = False
            add_rank_event(
                session,
                group_id=group_id,
                actor_id=moderator_id,
                target_id=target_id,
                action="remove_by_kick",
                old_rank=old_rank,
                new_rank=None,
                details={"reason": safe_reason},
            )
        log_action(session, group_id, moderator_id, target_id, "kick", safe_reason)
        return _success(
            manual_action_notice(
                action="kick",
                target=target_name,
                moderator=moderator_name,
                reason=safe_reason,
                actor_role=actor_role,
            )
        )

    if action == "warn":
        session.add(
            Warning(
                group_id=group_id,
                user_telegram_id=target_id,
                moderator_telegram_id=moderator_id,
                reason=safe_reason,
            )
        )
        await session.flush()
        count = await active_warnings_count(session, group_id, target_id)
        log_action(
            session,
            group_id,
            moderator_id,
            target_id,
            "warn",
            safe_reason,
            {"active_count": count},
        )
        notice = manual_action_notice(
            action="warn",
            target=target_name,
            moderator=moderator_name,
            reason=safe_reason,
            warning_count=count,
            warning_limit=warnings_limit,
            actor_role=actor_role,
        )
        if count >= warnings_limit and not target_is_admin_rank:
            until = now + timedelta(seconds=default_mute)
            try:
                await bot.restrict_chat_member(
                    chat_id,
                    target_id,
                    permissions=MUTED,
                    until_date=int(until.timestamp()),
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                notice += "\n\n⚠️ Лимит предупреждений достигнут, но Telegram не позволил автоматически выдать мут."
            else:
                session.add(
                    Punishment(
                        group_id=group_id,
                        user_telegram_id=target_id,
                        moderator_telegram_id=moderator_id,
                        kind="mute",
                        reason="Лимит предупреждений",
                        ends_at=until,
                    )
                )
                log_action(
                    session,
                    group_id,
                    moderator_id,
                    target_id,
                    "auto_mute",
                    "Лимит предупреждений",
                    {"duration": default_mute},
                )
                notice += "\n\n" + manual_action_notice(
                    action="mute",
                    target=target_name,
                    moderator=moderator_name,
                    reason="достигнут лимит предупреждений",
                    duration_seconds=default_mute,
                    actor_role=actor_role,
                )
        elif count >= warnings_limit and target_is_admin_rank:
            notice += "\n\nЛимит предупреждений достигнут, но администратор не был автоматически ограничен."
        return _success(notice)

    if action == "unwarn":
        warning = await session.scalar(
            select(Warning)
            .where(
                Warning.group_id == group_id,
                Warning.user_telegram_id == target_id,
                Warning.active.is_(True),
            )
            .order_by(Warning.created_at.desc())
        )
        if warning is None:
            return _failure(f"У {target_name} нет активных предупреждений.")
        warning.active = False
        await session.flush()
        count = await active_warnings_count(session, group_id, target_id)
        log_action(
            session,
            group_id,
            moderator_id,
            target_id,
            "unwarn",
            safe_reason,
            {"warning_id": warning.id, "active_count": count},
        )
        return _success(
            manual_action_notice(
                action="unwarn",
                target=target_name,
                moderator=moderator_name,
                reason=safe_reason,
                actor_role=actor_role,
            )
            + f"\n\nАктивных предупреждений: {count}."
        )

    if action == "warnings":
        count = await active_warnings_count(session, group_id, target_id)
        return _success(
            f"⚠️ У {target_name} активных предупреждений: {count}/{warnings_limit}.",
            commit=False,
            public_notice=False,
        )

    raise ValueError(f"Unsupported moderation action: {action}")
