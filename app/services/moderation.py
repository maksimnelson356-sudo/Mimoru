from __future__ import annotations

import structlog
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions
from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog, Punishment, UserMessage, Warning
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

log = structlog.get_logger()

# Telegram не удаляет сообщения старше 48 часов. Берём 47 часов с запасом на
# задержку фоновой уборки user_messages (background_leader чистит по 49 часов),
# иначе очистка при бане молча пропускала бы часть сообщений.
CLEANUP_WINDOW_HOURS = 47
CLEANUP_BATCH_SIZE = 100


async def purge_user_messages(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    group_id: int,
    target_id: int,
) -> tuple[int, int, int]:
    """Delete the recorded messages of one user. Returns (total, deleted, failed batches).

    Successfully deleted rows leave ``user_messages``: keeping them would make a
    second ban inside the window report a false "cleanup incomplete" for messages
    that are already gone. Rows of a failed batch stay, so a later ban retries them.

    The caller owns the transaction: this function never commits.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CLEANUP_WINDOW_HOURS)
    rows = (
        await session.scalars(
            select(UserMessage.message_id).where(
                UserMessage.group_id == group_id,
                UserMessage.user_telegram_id == target_id,
                UserMessage.created_at > cutoff,
            )
        )
    ).all()
    total = len(rows)
    deleted = 0
    failed_batches = 0
    for i in range(0, total, CLEANUP_BATCH_SIZE):
        batch = list(rows[i : i + CLEANUP_BATCH_SIZE])
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=batch)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # Telegram rejects the whole batch at once, so a single stale id
            # inside it costs every message of that batch.
            failed_batches += 1
            log.warning(
                "delete_messages_batch_failed",
                group_id=group_id,
                user_telegram_id=target_id,
                batch_size=len(batch),
                error=str(exc),
            )
            continue
        deleted += len(batch)
        await session.execute(
            sql_delete(UserMessage).where(
                UserMessage.group_id == group_id,
                UserMessage.user_telegram_id == target_id,
                UserMessage.message_id.in_(batch),
            )
        )
    return total, deleted, failed_batches


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
    cleanup_requested: bool
    cleanup_deleted: int
    cleanup_incomplete: bool
    cleanup_notice: str

    def __new__(
        cls,
        text: str,
        *,
        success: bool,
        commit: bool,
        public_notice: bool,
        cleanup_requested: bool = False,
        cleanup_deleted: int = 0,
        cleanup_incomplete: bool = False,
        cleanup_notice: str = "",
    ) -> "ModerationOutcome":
        obj = super().__new__(cls, text)
        obj.success = success
        obj.commit = commit
        obj.public_notice = public_notice
        obj.cleanup_requested = cleanup_requested
        obj.cleanup_deleted = cleanup_deleted
        obj.cleanup_incomplete = cleanup_incomplete
        obj.cleanup_notice = cleanup_notice
        return obj


def _success(
    text: str,
    *,
    commit: bool = True,
    public_notice: bool = True,
    cleanup_requested: bool = False,
    cleanup_deleted: int = 0,
    cleanup_incomplete: bool = False,
    cleanup_notice: str = "",
) -> ModerationOutcome:
    return ModerationOutcome(
        text,
        success=True,
        commit=commit,
        public_notice=public_notice,
        cleanup_requested=cleanup_requested,
        cleanup_deleted=cleanup_deleted,
        cleanup_incomplete=cleanup_incomplete,
        cleanup_notice=cleanup_notice,
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
    cleanup: bool = False,
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
        cleanup_total = 0
        cleanup_deleted = 0
        cleanup_failed_batches = 0
        if cleanup:
            cleanup_total, cleanup_deleted, cleanup_failed_batches = (
                await purge_user_messages(
                    bot,
                    session,
                    chat_id=chat_id,
                    group_id=group_id,
                    target_id=target_id,
                )
            )
            if cleanup_deleted:
                log.info(
                    "ban_cleanup_deleted",
                    group_id=group_id,
                    user_telegram_id=target_id,
                    deleted=cleanup_deleted,
                    attempted=cleanup_total,
                )
        cleanup_incomplete = bool(
            cleanup
            and (
                cleanup_failed_batches > 0
                or cleanup_deleted < cleanup_total
            )
        )
        if cleanup_incomplete:
            log.warning(
                "ban_cleanup_incomplete",
                group_id=group_id,
                user_telegram_id=target_id,
                attempted=cleanup_total,
                deleted=cleanup_deleted,
                failed_batches=cleanup_failed_batches,
            )
        notice = manual_action_notice(
            action="ban",
            target=target_name,
            moderator=moderator_name,
            reason=safe_reason,
            duration_seconds=duration,
            actor_role=actor_role,
        )
        cleanup_notice = ""
        if cleanup_incomplete:
            cleanup_notice = (
                "⚠️ Очистка сообщений выполнена не полностью: "
                f"удалено {cleanup_deleted} из {cleanup_total}."
            )
            notice = f"{notice}\n\n{cleanup_notice}"
        return _success(
            notice,
            cleanup_requested=bool(cleanup),
            cleanup_deleted=cleanup_deleted,
            cleanup_incomplete=cleanup_incomplete,
            cleanup_notice=cleanup_notice,
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
