"""Дедупликация обновлений по message_id / callback.id.

Telegram может доставить один и тот же update дважды (retry при сетевых
сбоях). Без дедупликации бот обработает сообщение дважды: продублирует
ответ, создаст второй GameEvent, спишет звёзды второй раз и т.п.

Этот middleware использует Redis SET NX EX — атомарно ставит ключ и
пропускает handler, если ключ уже существует.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis

import structlog


logger = structlog.get_logger(__name__)


# TTL ключей дедупликации
MESSAGE_DEDUP_TTL = 24 * 60 * 60       # 24 часа
CALLBACK_DEDUP_TTL = 60 * 60           # 1 час

MESSAGE_DEDUP_KEY = "mimoru:dedup:msg:{chat_id}:{message_id}"
CALLBACK_DEDUP_KEY = "mimoru:dedup:cbq:{user_id}:{callback_id}"


class MessageDeduplicationMiddleware(BaseMiddleware):
    """Пропускает дублирующиеся updates.

    Использует Redis SET NX EX:
    - Message:         mimoru:dedup:msg:{chat_id}:{message_id}    TTL 24h
    - CallbackQuery:   mimoru:dedup:cbq:{user_id}:{callback_id}   TTL 1h

    Если ключ уже существует — это дубликат: логируем warning и возвращаем None
    без вызова handler. Никаких ответов пользователю — Telegram не должен видеть
    реакцию на retry.

    Edited message, InlineQuery и прочие типы — пропускаются без дедупликации,
    потому что их повтор может быть легитимным (например, редактирование).
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        key = self._build_key(event)
        if key is None:
            # Тип события не поддерживает дедупликацию — пропускаем как есть
            return await handler(event, data)

        ttl = MESSAGE_DEDUP_TTL if isinstance(event, Message) else CALLBACK_DEDUP_TTL

        accepted = await self.redis.set(key, "1", nx=True, ex=ttl)
        if not accepted:
            logger.warning(
                "duplicate_update_skipped",
                key=key,
                event_type=type(event).__name__,
            )
            return None

        return await handler(event, data)

    @staticmethod
    def _build_key(event: TelegramObject) -> str | None:
        """Строит ключ дедупликации для Message / CallbackQuery.

        Возвращает None для событий, которые не нужно дедуплицировать.
        """
        if isinstance(event, Message):
            return MESSAGE_DEDUP_KEY.format(
                chat_id=event.chat.id,
                message_id=event.message_id,
            )
        if isinstance(event, CallbackQuery):
            return CALLBACK_DEDUP_KEY.format(
                user_id=event.from_user.id,
                callback_id=event.id,
            )
        return None


__all__ = [
    "MessageDeduplicationMiddleware",
    "MESSAGE_DEDUP_KEY",
    "CALLBACK_DEDUP_KEY",
    "MESSAGE_DEDUP_TTL",
    "CALLBACK_DEDUP_TTL",
]