from __future__ import annotations

import random
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from redis.asyncio import Redis
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity, ReactionTypeEmoji
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.fun_models import GameEvent, GroupMarriage
from app.db.models import Group, User
from app.entertainment_contracts import ENTERTAINMENT_ACTIONS, RELATIONSHIP_ACTIONS
from app.game_contracts import PROPOSALS, PROPOSAL_ACTIONS
from app.action_templates import ACTION_TEMPLATES, DEFAULT_ACTION_TEMPLATES
from app.russian_inflect import (
    CASE_ABLT,
    CASE_ACCS,
    CASE_DATV,
    CASE_GENT,
    _inflect,
)

router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}
ACTION_COOLDOWN_SECONDS = 3
_last_variant: dict[tuple[int, int, str], int] = {}
COOLDOWN_KEY_TEMPLATE = "mimoru:action-cooldown:{chat_id}:{user_id}:{action}"
FOREIGN_BUTTON_NOTICE = "Эти кнопки предназначены другому участнику 🙂"

ACTION_EMOJI = {
    "аннигилировать": "💥",
    "арестовать": "🚓",
    "благословить": "🙏",
    "взорвать": "💥",
    "воскресить": "✨",
    "выгнать из дома": "🚪",
    "выебать": "😏",
    "вызвать полицию": "🚓",
    "выкинуть": "🗑",
    "выкинуть в мусорку": "🗑",
    "дать вайфай": "📶",
    "дать дошик": "🍜",
    "дать леща": "👋",
    "дать подзатыльник": "🤚",
    "депортировать": "✈️",
    "забанить": "🔨",
    "забуллить": "😈",
    "завернуть в плед": "🛌",
    "загуглить": "🔎",
    "задушить обнимашками": "🫂",
    "зажарить": "🔥",
    "закопать": "⚰️",
    "заморозить": "🧊",
    "занять сотку": "💵",
    "запихнуть в холодильник": "🧊",
    "заскамить": "🎭",
    "засосать": "😘",
    "засунуть в шкаф": "🚪",
    "затащить в кровать": "🛏",
    "зафрендзонить": "🚧",
    "изгнать": "🚫",
    "испепелить": "🔥",
    "клонировать": "🧬",
    "купить": "🛒",
    "лизнуть": "👅",
    "наколдовать понос": "💩",
    "накормить": "🍕",
    "напоить": "🍺",
    "обменять": "🔄",
    "обнять": "🫂",
    "осудить": "⚖️",
    "отключить интернет": "📵",
    "отобрать еду": "🍽",
    "отомстить": "⚔️",
    "отправить в дурку": "🏥",
    "отправить на завод": "🏭",
    "отправить на картошку": "🥔",
    "отшлёпать": "👋",
    "пнуть": "🦵",
    "пнуть под зад": "🦵",
    "погладить": "🤗",
    "подарить миллион": "💰",
    "подраться": "🥊",
    "поклониться": "🙇",
    "покормить": "🍕",
    "покусать": "🦷",
    "помириться": "🤝",
    "понюхать": "👃",
    "понюхать волосы": "👃",
    "попросить денег": "💸",
    "поругаться": "💢",
    "посадить": "🪑",
    "посадить в тюрьму": "🔒",
    "поселить в подвале": "🏚",
    "поссориться": "💢",
    "потыкать": "👉",
    "похвалить": "👏",
    "поцеловать": "💋",
    "пощекотать": "😂",
    "превратить в дошик": "🍜",
    "превратить в жабу": "🐸",
    "превратить в кота": "🐈",
    "предать": "🗡",
    "призвать": "🔮",
    "продать": "💸",
    "проклясть": "🔮",
    "разбудить": "⏰",
    "раздеть": "👕",
    "сглазить": "👁",
    "сдать бабушке": "👵",
    "сдать в аренду": "🏠",
    "сделать админом": "🛡",
    "сделать комплимент": "✨",
    "сломать телефон": "📱",
    "снять админку": "🔓",
    "соблазнить": "😏",
    "съесть": "🍽",
    "убить": "☠️",
    "уважить": "🤝",
    "удалить аккаунт": "🗑",
    "ударить": "👊",
    "удочерить": "👨‍👧",
    "уебать": "👊",
    "украсть": "🤫",
    "украсть носок": "🧦",
    "украсть одеяло": "🛌",
    "украсть почку": "🫀",
    "украсть сердце": "💘",
    "укусить": "🦷",
    "уложить спать": "😴",
    "уменьшить": "📏",
    "усыновить": "👨‍👦",
    "усыпить": "😴",
    "ущипнуть": "🤏",
}
DEFAULT_EMOJIS = ("🎭", "😄", "✨", "😎", "🔥")
# ── #3c: relationship actions с подтверждением ────────────────────────────
# Slug-имена для callback_data — короткие, ASCII, влезают в лимит 64 байта.
RELATIONSHIP_ACTION_SLUGS: dict[str, str] = {
    "поссориться": "posoritsya",
    "поругаться": "porugatsya",
    "подраться": "podratsya",
    "помириться": "pomiritsya",
}
RELATIONSHIP_SLUG_TO_ACTION: dict[str, str] = {
    slug: action for action, slug in RELATIONSHIP_ACTION_SLUGS.items()
}

# Через сколько секунд предложение relationship-action считается устаревшим
RELATIONSHIP_CONFIRM_TTL_SECONDS = 5 * 60  # 5 минут

# Шаблон подтверждающего сообщения
RELATIONSHIP_PROMPT_TEMPLATE = (
    "{actor} предлагает {target_dat} {action_label}.\n"
    "{target}, подтверди, пожалуйста."
)

# Человекочитаемые лейблы для actions в подтверждающем сообщении
RELATIONSHIP_ACTION_LABELS: dict[str, str] = {
    "поссориться": "поссориться",
    "поругаться": "поругаться",
    "подраться": "подраться",
    "помириться": "помириться",
}
ACTION_VARIANTS = (
    "{emoji} {actor} → {target}: «{action}».",
    "{emoji} {actor} выбрал для {target}: «{action}» 😄",
    "{emoji} {target}, внимание: {actor} использует «{action}»!",
    "{emoji} Сегодня у {actor} план — «{action}» для {target} 😏",
    "{emoji} {actor} + {target}: действие момента — «{action}».",
    "{emoji} {actor} применяет к {target} «{action}».",
)
PROPOSAL_TEMPLATES = {
    "marry": (
        "💍 {actor} делает предложение {target}. Свадьбе быть?",
        "💒 {actor} зовёт {target} под виртуальный венец. Решение за тобой!",
        "🥂 {target}, у {actor} серьёзный вопрос: поженимся?",
    ),
}
RESULT_VARIANTS = {
    "marry": (
        "💍 {actor} и {target} теперь пара этой группы! Горько! 🎉",
        "💒 Свершилось: {actor} + {target} = ❤️. Чат готовит свадьбу!",
        "🥂 {actor} и {target} сказали друг другу «да». Поздравляем!",
    ),
}
TOKEN_RE = re.compile(r"\{(actor(?:_(?:acc|ablt|dat|gent))?|target(?:_(?:acc|ablt|dat|gent))?)\}")

def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _tg_name(user) -> str:
    full_name = (getattr(user, "full_name", None) or "").strip()
    if full_name:
        return full_name
    if getattr(user, "username", None):
        return f"@{user.username}"
    return "участник"


def _pick_variant(key: tuple[int, int, str], variants: tuple[str, ...]) -> str:
    previous = _last_variant.get(key)
    choices = [idx for idx in range(len(variants)) if idx != previous] or list(range(len(variants)))
    idx = random.choice(choices)
    _last_variant[key] = idx
    return variants[idx]

async def _check_cooldown(redis: Redis, chat_id: int, user_id: int, action: str) -> bool:
    """Атомарно проверяет и ставит кулдаун через Redis SET NX EX.

    Возвращает True, если действие можно выполнять (кулдаун не активен и только что поставлен).
    Возвращает False, если кулдаун ещё активен.
    """
    key = COOLDOWN_KEY_TEMPLATE.format(chat_id=chat_id, user_id=user_id, action=action)
    # SET key 1 NX EX N — атомарно ставит ключ, только если его нет
    accepted = await redis.set(key, "1", nx=True, ex=ACTION_COOLDOWN_SECONDS)
    return bool(accepted)

def _render(template: str, mentions: dict[str, tuple[str, int]], **plain: str) -> tuple[str, list[MessageEntity]]:
    template = template.format(**{key: "{" + key + "}" for key in mentions}, **plain)
    chunks: list[str] = []
    entities: list[MessageEntity] = []
    cursor = 0
    for match in TOKEN_RE.finditer(template):
        chunks.append(template[cursor:match.start()])
        name, user_id = mentions[match.group(1)]
        prefix = "".join(chunks)
        chunks.append(name)
        entities.append(MessageEntity(type="text_link", offset=_utf16_len(prefix), length=_utf16_len(name), url=f"tg://user?id={user_id}"))
        cursor = match.end()
    chunks.append(template[cursor:])
    return "".join(chunks), entities


async def _db_name(session: AsyncSession, user_id: int, fallback: str | None = None) -> str:
    user = await session.scalar(select(User).where(User.telegram_id == user_id))
    if user is not None:
        name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
        if name:
            return name
        if user.username:
            return f"@{user.username}"
    if fallback and fallback.strip() and not fallback.strip().isdigit():
        return fallback.strip()
    return "участник"


async def _active_group(session: AsyncSession, chat_id: int, *, for_update: bool = False) -> Group | None:
    query = select(Group).where(Group.telegram_chat_id == chat_id, Group.is_active.is_(True))
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


def _proposal_markup(kind: str, group_id: int, actor_id: int, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять", callback_data=f"fsfriendly:{kind}:{group_id}:{actor_id}:{target_id}:yes"), InlineKeyboardButton(text="❌ Отказать", callback_data=f"fsfriendly:{kind}:{group_id}:{actor_id}:{target_id}:no")]])


@router.message(F.chat.type.in_(GROUP_TYPES), F.reply_to_message, F.text.casefold().in_(ENTERTAINMENT_ACTIONS))
async def friendly_fun_action(message: Message, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    if message.from_user is None or message.reply_to_message.from_user is None:
        return
    actor, target = message.from_user, message.reply_to_message.from_user
    if target.is_bot:
        return
    if actor.id == target.id:
        await message.reply("😄 С собой это действие выглядит слишком подозрительно.")
        return

    action = " ".join((message.text or "").casefold().strip().split())

    # Redis-кулдаун per (chat_id, user_id, action) — 3 секунды
    if not await _check_cooldown(redis, message.chat.id, actor.id, action):
        await message.reply(f"⏳ Подожди {ACTION_COOLDOWN_SECONDS} секунды до следующего действия 😄")
        return

    action = " ".join((message.text or "").casefold().strip().split())
    variants = ACTION_TEMPLATES.get(action, DEFAULT_ACTION_TEMPLATES)
    variant = _pick_variant((message.chat.id, actor.id, action), variants)

    actor_name = _tg_name(actor)
    target_name = _tg_name(target)

    mentions = {
        "actor": (actor_name, actor.id),
        "target": (target_name, target.id),
        "actor_acc": (_inflect(actor_name, CASE_ACCS), actor.id),
        "actor_ablt": (_inflect(actor_name, CASE_ABLT), actor.id),
        "actor_dat": (_inflect(actor_name, CASE_DATV), actor.id),
        "actor_gent": (_inflect(actor_name, CASE_GENT), actor.id),
        "target_acc": (_inflect(target_name, CASE_ACCS), target.id),
        "target_ablt": (_inflect(target_name, CASE_ABLT), target.id),
        "target_dat": (_inflect(target_name, CASE_DATV), target.id),
        "target_gent": (_inflect(target_name, CASE_GENT), target.id),
    }

    text, entities = _render(variant, mentions)

    reply_msg = await message.reply(text, entities=entities)
    _emoji = ACTION_EMOJI.get(action)
    if _emoji and reply_msg is not None:
        try:
            await bot.set_message_reaction(
                chat_id=reply_msg.chat.id,
                message_id=reply_msg.message_id,
                reaction=[ReactionTypeEmoji(emoji=_emoji)],
            )
        except Exception as _exc:
            log.warning("reaction_set_failed", action=action, emoji=_emoji, error=str(_exc))
    group = await _active_group(session, message.chat.id)
    if group is not None:
        event_type = "relationship_action" if action in RELATIONSHIP_ACTIONS else "entertainment_action"
        session.add(GameEvent(group_id=group.id, event_type=event_type, action=action, actor_telegram_id=actor.id, target_telegram_id=target.id, actor_name=actor_name, target_name=target_name, outcome="done"))
        await session.commit()

# ── #3c: relationship actions с подтверждением ───────────────────────────


def _relationship_markup(slug: str, group_id: int, actor_id: int, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"fsrel:{slug}:{group_id}:{actor_id}:{target_id}:yes"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"fsrel:{slug}:{group_id}:{actor_id}:{target_id}:no"),
    ]])


@router.message(F.chat.type.in_(GROUP_TYPES), F.reply_to_message, F.text.casefold().in_(RELATIONSHIP_ACTIONS))
async def friendly_relationship_action(message: Message, session: AsyncSession, redis: Redis) -> None:
    """#3c: relationship actions требуют подтверждения от target."""
    if message.from_user is None or message.reply_to_message.from_user is None:
        return
    actor, target = message.from_user, message.reply_to_message.from_user
    if target.is_bot:
        await message.reply("🤖 Боты не участвуют в таких действиях.")
        return
    if actor.id == target.id:
        await message.reply("😄 С собой это действие выглядит слишком подозрительно.")
        return

    action = " ".join((message.text or "").casefold().strip().split())
    slug = RELATIONSHIP_ACTION_SLUGS.get(action)
    if slug is None:
        return

    if not await _check_cooldown(redis, message.chat.id, actor.id, action):
        await message.reply(f"⏳ Подожди {ACTION_COOLDOWN_SECONDS} секунды до следующего действия 😄")
        return

    group = await _active_group(session, message.chat.id)
    if group is None:
        return

    actor_name = _tg_name(actor)
    target_name = _tg_name(target)

    session.add(GameEvent(
        group_id=group.id,
        event_type="relationship_action",
        action=action,
        actor_telegram_id=actor.id,
        target_telegram_id=target.id,
        actor_name=actor_name,
        target_name=target_name,
        outcome="pending",
    ))
    await session.commit()

    label = RELATIONSHIP_ACTION_LABELS.get(action, action)
    prompt = RELATIONSHIP_PROMPT_TEMPLATE.format(
        actor="{" + "actor" + "}",
        target_dat="{" + "target_dat" + "}",
        target="{" + "target" + "}",
        action_label=label,
    )
    mentions = {
        "actor": (actor_name, actor.id),
        "target": (target_name, target.id),
        "target_dat": (_inflect(target_name, CASE_DATV), target.id),
    }
    text, entities = _render(prompt, mentions)
    await message.reply(text, entities=entities, reply_markup=_relationship_markup(slug, group.id, actor.id, target.id))


@router.callback_query(F.data.regexp(r"^fsrel:(posoritsya|porugatsya|podratsya|pomiritsya):\d+:\d+:\d+:(yes|no)$"))
async def friendly_relationship_answer(callback: CallbackQuery, session: AsyncSession) -> None:
    """#3c: подтверждение/отклонение relationship action."""
    _, slug, raw_group, raw_actor, raw_target, decision = (callback.data or "").split(":")
    group_id, actor_id, target_id = int(raw_group), int(raw_actor), int(raw_target)

    if callback.from_user.id != target_id:
        await callback.answer(FOREIGN_BUTTON_NOTICE, show_alert=True)
        return

    action = RELATIONSHIP_SLUG_TO_ACTION.get(slug)
    if action is None:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    group = await session.scalar(
        select(Group).where(Group.id == group_id, Group.is_active.is_(True)).with_for_update()
    )
    if group is None or callback.message is None or callback.message.chat.id != group.telegram_chat_id:
        await callback.answer("Это предложение уже недоступно.", show_alert=True)
        return

    event = await session.scalar(
        select(GameEvent)
        .where(
            GameEvent.group_id == group_id,
            GameEvent.event_type == "relationship_action",
            GameEvent.action == action,
            GameEvent.actor_telegram_id == actor_id,
            GameEvent.target_telegram_id == target_id,
            GameEvent.outcome == "pending",
        )
        .order_by(GameEvent.id.desc())
        .limit(1)
        .with_for_update()
    )
    if event is None:
        await callback.answer("На это предложение уже ответили.", show_alert=True)
        return

    created_at = event.created_at
    if created_at is not None:
        now = datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if (now - created_at).total_seconds() > RELATIONSHIP_CONFIRM_TTL_SECONDS:
            event.outcome = "expired"
            await session.commit()
            await callback.answer("Предложение устарело.", show_alert=True)
            return

    actor_name = await _db_name(session, actor_id, event.actor_name)
    target_name = await _db_name(session, target_id, event.target_name or _tg_name(callback.from_user))

    mentions = {
        "actor": (actor_name, actor_id),
        "target": (target_name, target_id),
        "actor_acc": (_inflect(actor_name, CASE_ACCS), actor_id),
        "actor_ablt": (_inflect(actor_name, CASE_ABLT), actor_id),
        "actor_dat": (_inflect(actor_name, CASE_DATV), actor_id),
        "actor_gent": (_inflect(actor_name, CASE_GENT), actor_id),
        "target_acc": (_inflect(target_name, CASE_ACCS), target_id),
        "target_ablt": (_inflect(target_name, CASE_ABLT), target_id),
        "target_dat": (_inflect(target_name, CASE_DATV), target_id),
        "target_gent": (_inflect(target_name, CASE_GENT), target_id),
    }

    if decision == "no":
        event.outcome = "rejected"
        await session.commit()
        text, entities = _render(
            random.choice((
                "🙅 {target} отказался. Без обид 😄",
                "😅 {actor}, {target} не поддержал это действие.",
            )),
            mentions,
        )
        await callback.message.edit_text(text, entities=entities)
        await callback.answer("Отклонено")
        return

    event.outcome = "accepted"
    await session.commit()

    variants = ACTION_TEMPLATES.get(action, DEFAULT_ACTION_TEMPLATES)
    variant = _pick_variant((callback.message.chat.id, actor_id, action), variants)
    text, entities = _render(variant, mentions)
    await callback.message.edit_text(text, entities=entities)
    await callback.answer("Принято")



@router.message(F.chat.type.in_(GROUP_TYPES), F.reply_to_message, F.text.casefold().in_(PROPOSAL_ACTIONS))
async def friendly_proposal(message: Message, session: AsyncSession) -> None:
    if message.from_user is None or message.reply_to_message.from_user is None:
        return
    actor, target = message.from_user, message.reply_to_message.from_user
    if actor.id == target.id:
        await message.reply("😄 Для этой истории нужен второй участник.")
        return
    if target.is_bot:
        await message.reply("🤖 Боты пока не принимают такие предложения.")
        return
    action = " ".join((message.text or "").casefold().strip().split())
    kind = PROPOSALS[action][0]
    group = await _active_group(session, message.chat.id, for_update=True)
    if group is None:
        return
    occupied = await session.scalar(select(GroupMarriage.id).where(GroupMarriage.group_id == group.id, GroupMarriage.active.is_(True), or_(GroupMarriage.user1_telegram_id.in_((actor.id, target.id)), GroupMarriage.user2_telegram_id.in_((actor.id, target.id)))).limit(1))
    if occupied is not None:
        await message.reply("💍 Кто-то из вас уже состоит в браке в этой группе.")
        return
    actor_name, target_name = _tg_name(actor), _tg_name(target)
    session.add(GameEvent(group_id=group.id, event_type="relationship_proposal", action=kind, actor_telegram_id=actor.id, target_telegram_id=target.id, actor_name=actor_name, target_name=target_name, outcome="pending"))
    await session.commit()
    template = _pick_variant((message.chat.id, actor.id, f"proposal:{kind}"), PROPOSAL_TEMPLATES[kind])
    text, entities = _render(template, {"actor": (actor_name, actor.id), "target": (target_name, target.id)})
    await message.reply(text, entities=entities, reply_markup=_proposal_markup(kind, group.id, actor.id, target.id))


@router.callback_query(F.data.regexp(r"^fsfriendly:marry:\d+:\d+:\d+:(yes|no)$"))
async def friendly_answer(callback: CallbackQuery, session: AsyncSession) -> None:
    _, kind, raw_group, raw_actor, raw_target, decision = (callback.data or "").split(":")
    group_id, actor_id, target_id = int(raw_group), int(raw_actor), int(raw_target)
    if callback.from_user.id != target_id:
        await callback.answer(FOREIGN_BUTTON_NOTICE, show_alert=True)
        return
    group = await session.scalar(select(Group).where(Group.id == group_id, Group.is_active.is_(True)).with_for_update())
    if group is None or callback.message is None or callback.message.chat.id != group.telegram_chat_id:
        await callback.answer("Это предложение уже недоступно.", show_alert=True)
        return
    event = await session.scalar(select(GameEvent).where(GameEvent.group_id == group_id, GameEvent.event_type.in_(("relationship_proposal", "proposal")), GameEvent.action == kind, GameEvent.actor_telegram_id == actor_id, GameEvent.target_telegram_id == target_id, GameEvent.outcome == "pending").order_by(GameEvent.id.desc()).limit(1).with_for_update())
    if event is None:
        await callback.answer("На это предложение уже ответили.", show_alert=True)
        return
    actor_name = await _db_name(session, actor_id, event.actor_name)
    target_name = await _db_name(session, target_id, event.target_name or _tg_name(callback.from_user))
    mentions = {"actor": (actor_name, actor_id), "target": (target_name, target_id)}
    event.outcome = "accepted" if decision == "yes" else "rejected"
    if decision == "no":
        await session.commit()
        text, entities = _render(random.choice(("❌ {target} отказал {actor}. Без обид 😄", "🙅 {target} сказал «не сегодня», {actor}.", "😅 {actor}, {target} отклонил предложение.")), mentions)
        await callback.message.edit_text(text, entities=entities)
        await callback.answer("Отказано")
        return
    occupied = await session.scalar(select(GroupMarriage.id).where(GroupMarriage.group_id == group.id, GroupMarriage.active.is_(True), or_(GroupMarriage.user1_telegram_id.in_((actor_id, target_id)), GroupMarriage.user2_telegram_id.in_((actor_id, target_id)))).limit(1))
    if occupied is not None:
        event.outcome = "cancelled"
        await session.commit()
        await callback.answer("Кто-то из участников уже состоит в браке.", show_alert=True)
        return
    first, second = sorted((actor_id, target_id))
    existing = await session.scalar(select(GroupMarriage).where(GroupMarriage.group_id == group.id, GroupMarriage.user1_telegram_id == first, GroupMarriage.user2_telegram_id == second))
    if existing is None:
        session.add(GroupMarriage(group_id=group.id, user1_telegram_id=first, user2_telegram_id=second, active=True))
    else:
        existing.active, existing.ended_at = True, None
    await session.commit()
    text, entities = _render(random.choice(RESULT_VARIANTS["marry"]), mentions)
    await callback.message.edit_text(text, entities=entities)
    await callback.answer("Принято")


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_({"развестись", "подать на развод"}))
async def friendly_divorce(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    group = await _active_group(session, message.chat.id, for_update=True)
    if group is None:
        return
    marriage = await session.scalar(select(GroupMarriage).where(GroupMarriage.group_id == group.id, GroupMarriage.active.is_(True), or_(GroupMarriage.user1_telegram_id == message.from_user.id, GroupMarriage.user2_telegram_id == message.from_user.id)).with_for_update())
    if marriage is None:
        await message.reply("💍 В этой группе ты сейчас не в браке.")
        return
    partner_id = marriage.user2_telegram_id if marriage.user1_telegram_id == message.from_user.id else marriage.user1_telegram_id
    partner_name = await _db_name(session, partner_id)
    marriage.active = False
    marriage.ended_at = datetime.now(timezone.utc)
    await session.commit()
    text, entities = _render(random.choice(("💔 {actor} и {target} теперь свободны. Без делёжки Wi‑Fi 😄", "💔 Брак завершён: {actor} и {target}. Жизнь продолжается!", "🕊️ {actor} и {target} мирно разошлись. Чат желает удачи обоим.")), {"actor": (_tg_name(message.from_user), message.from_user.id), "target": (partner_name, partner_id)})
    await message.reply(text, entities=entities)


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_({"мой брак", "брак", "мои отношения"}))
async def friendly_marriage(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    group = await _active_group(session, message.chat.id)
    if group is None:
        return
    marriage = await session.scalar(select(GroupMarriage).where(GroupMarriage.group_id == group.id, GroupMarriage.active.is_(True), or_(GroupMarriage.user1_telegram_id == message.from_user.id, GroupMarriage.user2_telegram_id == message.from_user.id)))
    if marriage is None:
        await message.reply("💍 В этой группе ты пока свободен 😄")
        return
    partner_id = marriage.user2_telegram_id if marriage.user1_telegram_id == message.from_user.id else marriage.user1_telegram_id
    partner_name = await _db_name(session, partner_id)
    text, entities = _render("💍 Твоя пара в этой группе — {target} ❤️", {"target": (partner_name, partner_id)})
    await message.reply(text, entities=entities)
