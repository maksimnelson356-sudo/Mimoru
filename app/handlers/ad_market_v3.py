from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.ad_market_models import GlobalPostRequest, RequiredAdDealRequest, RequiredAdListing
from app.db.models import Group
from app.services.required_resources import normalize_public_telegram_resource, validate_invite_link
from app.services.ui import clean_ui_text, panel_header


router = Router(name=__name__)
settings = get_settings()


class GlobalPostForm(StatesGroup):
    text = State()
    photo = State()
    button = State()


class RequiredListingForm(StatesGroup):
    min_days = State()
    price = State()


class RequiredDealForm(StatesGroup):
    target = State()
    manual_target = State()


def _contact_url(user_id: int) -> str:
    return f"tg://user?id={user_id}"


def _unit_label(unit: str) -> str:
    return "за сутки" if unit == "day" else "за одного подписчика"


def _back(callback_data: str, text: str = "◀️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback_data)]])


async def _member_count(bot: Bot, group: Group) -> int | None:
    try:
        return max(0, int(await bot.get_chat_member_count(group.telegram_chat_id)))
    except (TelegramBadRequest, TelegramForbiddenError):
        return None


# ---------------------------------------------------------------------------
# Global advertising post: buyer -> Mimoru owner review -> Stars -> all groups
# ---------------------------------------------------------------------------


def _global_editor_text(item: GlobalPostRequest) -> str:
    return panel_header(
        f"Рекламный пост #{item.id}",
        f"Публикация: во всех активных группах Mimoru после одобрения создателем и оплаты.\n"
        f"Стоимость после одобрения: {item.price_stars} Stars.\n\n"
        f"Текст: {'✅ добавлен' if (item.text or '').strip() else '➖ не добавлен'}\n"
        f"Изображение: {'✅ добавлено' if item.photo_file_id else '➖ не добавлено'}\n"
        f"Кнопка: {'✅ ' + item.button_text if item.button_text and item.button_url else '➖ не добавлена'}\n\n"
        "Перед отправкой на проверку можно отдельно изменить каждый элемент и посмотреть предпросмотр.",
    )


def _global_editor_keyboard(item: GlobalPostRequest) -> InlineKeyboardMarkup:
    ready = bool((item.text or "").strip() or item.photo_file_id)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="✏️ Текст", callback_data=f"gpost:text:{item.id}"),
            InlineKeyboardButton(text="🖼 Изображение", callback_data=f"gpost:photo:{item.id}"),
        ],
        [InlineKeyboardButton(text="🔗 Кнопка", callback_data=f"gpost:button:{item.id}")],
    ]
    if item.photo_file_id:
        rows.append([InlineKeyboardButton(text="🗑 Удалить изображение", callback_data=f"gpost:remove_photo:{item.id}")])
    if item.button_text and item.button_url:
        rows.append([InlineKeyboardButton(text="🗑 Удалить кнопку", callback_data=f"gpost:remove_button:{item.id}")])
    rows.append([InlineKeyboardButton(text="👁 Предпросмотр", callback_data=f"gpost:preview:{item.id}")])
    if ready:
        rows.append([InlineKeyboardButton(text="📨 Отправить на проверку", callback_data=f"gpost:submit:{item.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К покупке рекламы", callback_data="ads:buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _creative_markup(item: GlobalPostRequest, *, preview: bool = False) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if item.button_text and item.button_url:
        rows.append([InlineKeyboardButton(text=item.button_text, url=item.button_url)])
    if preview:
        rows.append([InlineKeyboardButton(text="✖️ Закрыть предпросмотр", callback_data=f"gpost:preview_close:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _owned_global(session: AsyncSession, item_id: int, user_id: int, *, editable: bool = True) -> GlobalPostRequest | None:
    item = await session.get(GlobalPostRequest, item_id)
    if item is None or item.buyer_telegram_id != user_id:
        return None
    if editable and item.status != "draft":
        return None
    return item


@router.callback_query(F.data == "ads:buy:post")
async def global_post_entry(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    item = GlobalPostRequest(
        buyer_telegram_id=callback.from_user.id,
        status="draft",
        price_stars=settings.global_post_price_stars,
    )
    session.add(item)
    await session.commit()
    await callback.message.edit_text(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^gpost:editor:\d+$"))
async def global_editor(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None:
        await callback.answer("Черновик недоступен.", show_alert=True)
        return
    await callback.message.edit_text(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^gpost:text:\d+$"))
async def global_text_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None:
        await callback.answer("Черновик недоступен.", show_alert=True)
        return
    await state.set_state(GlobalPostForm.text)
    await state.update_data(item_id=item.id)
    await callback.message.edit_text(
        panel_header("Текст рекламного поста", "Отправьте текст до 1000 символов. Этот лимит позволяет безопасно использовать тот же текст как подпись к изображению."),
        reply_markup=_back(f"gpost:editor:{item.id}", "✖️ Отменить ввод"),
    )
    await callback.answer()


@router.message(GlobalPostForm.text, F.chat.type == "private")
async def global_text_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    item = await _owned_global(session, int(data.get("item_id", 0)), message.from_user.id)
    if item is None:
        await state.clear()
        await message.answer("Черновик недоступен.")
        return
    text = clean_ui_text((message.text or "").strip())
    if not text:
        await message.answer("Текст не может быть пустым.")
        return
    if len(text) > 1000:
        await message.answer("Текст слишком длинный. Максимум 1000 символов.")
        return
    item.text = text
    await session.commit()
    await state.clear()
    await message.answer(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))


@router.callback_query(F.data.regexp(r"^gpost:photo:\d+$"))
async def global_photo_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None:
        await callback.answer("Черновик недоступен.", show_alert=True)
        return
    await state.set_state(GlobalPostForm.photo)
    await state.update_data(item_id=item.id)
    await callback.message.edit_text(
        panel_header("Изображение", "Пришлите одну фотографию. Она заменит предыдущее изображение."),
        reply_markup=_back(f"gpost:editor:{item.id}", "✖️ Отменить ввод"),
    )
    await callback.answer()


@router.message(GlobalPostForm.photo, F.chat.type == "private")
async def global_photo_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    item = await _owned_global(session, int(data.get("item_id", 0)), message.from_user.id)
    if item is None:
        await state.clear()
        await message.answer("Черновик недоступен.")
        return
    if not message.photo:
        await message.answer("Нужно прислать фотографию.")
        return
    item.photo_file_id = message.photo[-1].file_id
    await session.commit()
    await state.clear()
    await message.answer(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))


@router.callback_query(F.data.regexp(r"^gpost:button:\d+$"))
async def global_button_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None:
        await callback.answer("Черновик недоступен.", show_alert=True)
        return
    await state.set_state(GlobalPostForm.button)
    await state.update_data(item_id=item.id)
    await callback.message.edit_text(
        panel_header("Кнопка поста", "Отправьте одной строкой: Название кнопки | https://example.com"),
        reply_markup=_back(f"gpost:editor:{item.id}", "✖️ Отменить ввод"),
    )
    await callback.answer()


@router.message(GlobalPostForm.button, F.chat.type == "private")
async def global_button_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    item = await _owned_global(session, int(data.get("item_id", 0)), message.from_user.id)
    if item is None:
        await state.clear()
        await message.answer("Черновик недоступен.")
        return
    raw = clean_ui_text((message.text or "").strip())
    if "|" not in raw:
        await message.answer("Формат: Название кнопки | https://example.com")
        return
    title, url = [part.strip() for part in raw.split("|", 1)]
    parsed = urlparse(url)
    if not title or len(title) > 64 or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        await message.answer("Проверьте название и ссылку. Разрешены только http/https ссылки.")
        return
    item.button_text = title
    item.button_url = url
    await session.commit()
    await state.clear()
    await message.answer(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))


@router.callback_query(F.data.regexp(r"^gpost:remove_photo:\d+$"))
async def global_remove_photo(callback: CallbackQuery, session: AsyncSession) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None:
        await callback.answer("Черновик недоступен.", show_alert=True)
        return
    item.photo_file_id = None
    await session.commit()
    await callback.message.edit_text(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))
    await callback.answer("Изображение удалено")


@router.callback_query(F.data.regexp(r"^gpost:remove_button:\d+$"))
async def global_remove_button(callback: CallbackQuery, session: AsyncSession) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None:
        await callback.answer("Черновик недоступен.", show_alert=True)
        return
    item.button_text = None
    item.button_url = None
    await session.commit()
    await callback.message.edit_text(_global_editor_text(item), reply_markup=_global_editor_keyboard(item))
    await callback.answer("Кнопка удалена")


@router.callback_query(F.data.regexp(r"^gpost:preview:\d+$"))
async def global_preview(callback: CallbackQuery, session: AsyncSession) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None or (not (item.text or "").strip() and not item.photo_file_id):
        await callback.answer("Сначала добавьте текст или изображение.", show_alert=True)
        return
    markup = _creative_markup(item, preview=True)
    if item.photo_file_id:
        await callback.message.answer_photo(item.photo_file_id, caption=item.text or None, reply_markup=markup)
    else:
        await callback.message.answer(item.text, reply_markup=markup)
    await callback.answer("Предпросмотр отправлен ниже")


@router.callback_query(F.data.regexp(r"^gpost:preview_close:\d+$"))
async def global_preview_close(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


async def _notify_global_review(bot: Bot, item: GlobalPostRequest) -> None:
    for owner_id in settings.service_owner_ids:
        try:
            if item.photo_file_id:
                await bot.send_photo(owner_id, item.photo_file_id, caption=item.text or None, reply_markup=_creative_markup(item))
            else:
                await bot.send_message(owner_id, item.text, reply_markup=_creative_markup(item))
            await bot.send_message(
                owner_id,
                panel_header(
                    "Проверка рекламного поста",
                    f"Заявка #{item.id}\nПокупатель: {item.buyer_telegram_id}\n"
                    f"После одобрения покупателю будет выставлен счёт: {item.price_stars} Stars.\n"
                    "После оплаты пост автоматически отправится во все активные группы Mimoru.",
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Связаться с покупателем", url=_contact_url(item.buyer_telegram_id))],
                    [
                        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"gpost:approve:{item.id}"),
                        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"gpost:reject:{item.id}"),
                    ],
                ]),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            continue


@router.callback_query(F.data.regexp(r"^gpost:submit:\d+$"))
async def global_submit(callback: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if item is None or (not (item.text or "").strip() and not item.photo_file_id):
        await callback.answer("Пост не готов к проверке.", show_alert=True)
        return
    if not settings.service_owner_ids:
        await callback.answer("Создатель Mimoru не настроен. Заявку пока нельзя отправить.", show_alert=True)
        return
    item.status = "pending_review"
    await session.commit()
    await _notify_global_review(bot, item)
    await callback.message.edit_text(
        panel_header("Рекламный пост отправлен", f"Заявка #{item.id} ожидает проверки создателем Mimoru. Счёт появится только после одобрения."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Мои рекламные посты", callback_data="gpost:mine")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )
    await callback.answer("Заявка отправлена")


async def _send_global_invoice(bot: Bot, item: GlobalPostRequest) -> None:
    await bot.send_invoice(
        chat_id=item.buyer_telegram_id,
        title=f"Рекламный пост Mimoru #{item.id}",
        description="Одобренный рекламный пост для публикации во всех активных группах Mimoru.",
        payload=f"globalpost:{item.id}",
        currency="XTR",
        prices=[LabeledPrice(label="Глобальный рекламный пост", amount=item.price_stars)],
        provider_token="",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Оплатить {item.price_stars} Stars", pay=True)],
            [InlineKeyboardButton(text="◀️ Мои рекламные посты", callback_data="gpost:mine")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^gpost:pay:\d+$"))
async def global_reissue_invoice(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    item = await _owned_global(session, int(callback.data.split(":")[-1]), callback.from_user.id, editable=False)
    if item is None or item.status != "approved":
        await callback.answer("Счёт для этой заявки недоступен.", show_alert=True)
        return
    await _send_global_invoice(bot, item)
    await callback.answer("Счёт отправлен новым сообщением")


@router.callback_query(F.data.regexp(r"^gpost:(approve|reject):\d+$"))
async def global_review(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if callback.from_user.id not in settings.service_owner_ids:
        await callback.answer("Это действие доступно только создателю Mimoru.", show_alert=True)
        return
    _, decision, raw_id = callback.data.split(":")
    item = await session.get(GlobalPostRequest, int(raw_id))
    if item is None or item.status != "pending_review":
        await callback.answer("Заявка уже обработана или недоступна.", show_alert=True)
        return
    now = datetime.now(timezone.utc)
    item.reviewed_by_telegram_id = callback.from_user.id
    item.reviewed_at = now
    if decision == "approve":
        item.status = "approved"
        await session.commit()
        try:
            await bot.send_message(
                item.buyer_telegram_id,
                f"✅ Рекламный пост #{item.id} одобрен создателем Mimoru. Оплатите счёт ниже — после успешной оплаты публикация начнётся автоматически во всех активных группах Mimoru.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Мои рекламные посты", callback_data="gpost:mine")]
                ]),
            )
            await _send_global_invoice(bot, item)
        except (TelegramBadRequest, TelegramForbiddenError):
            await callback.answer("Заявка одобрена, но покупателю не удалось отправить сообщение. Он сможет получить счёт из «Моих рекламных постов».", show_alert=True)
        status = "✅ Рекламный пост одобрен. Покупателю доступен счёт."
    else:
        item.status = "rejected"
        await session.commit()
        status = "❌ Рекламный пост отклонён."
        try:
            await bot.send_message(
                item.buyer_telegram_id,
                f"❌ Рекламный пост #{item.id} не прошёл проверку создателем Mimoru.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Мои рекламные посты", callback_data="gpost:mine")]]),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    await callback.message.edit_text(panel_header("Проверка завершена", status))
    await callback.answer("Решение сохранено")


@router.callback_query(F.data == "gpost:mine")
async def global_mine(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    items = list((await session.scalars(
        select(GlobalPostRequest)
        .where(GlobalPostRequest.buyer_telegram_id == callback.from_user.id)
        .order_by(GlobalPostRequest.created_at.desc())
        .limit(20)
    )).all())
    labels = {
        "draft": "✏️ черновик",
        "pending_review": "⏳ на проверке",
        "approved": "✅ одобрен, ждёт оплаты",
        "paid": "📣 публикуется",
        "completed": "✅ опубликован",
        "rejected": "❌ отклонён",
    }
    text = "\n".join(f"#{item.id} · {labels.get(item.status, item.status)} · {item.price_stars} Stars" for item in items) if items else "Заявок пока нет."
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        if item.status == "draft":
            rows.append([InlineKeyboardButton(text=f"✏️ Продолжить #{item.id}", callback_data=f"gpost:editor:{item.id}")])
        elif item.status == "approved":
            rows.append([InlineKeyboardButton(text=f"⭐ Получить счёт #{item.id}", callback_data=f"gpost:pay:{item.id}")])
    rows.extend([
        [InlineKeyboardButton(text="➕ Новый рекламный пост", callback_data="ads:buy:post")],
        [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
    ])
    await callback.message.edit_text(panel_header("Мои рекламные посты", text), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


# ---------------------------------------------------------------------------
# Required-subscription marketplace: seller listing -> contact -> buyer request
# ---------------------------------------------------------------------------


async def _seller_group(session: AsyncSession, group_id: int, owner_id: int) -> Group | None:
    return await session.scalar(select(Group).where(
        Group.id == group_id,
        Group.owner_telegram_id == owner_id,
        Group.is_active.is_(True),
    ))
# ── #2: fallback для отображения названий групп ──────────────────────────

_PLACEHOLDER_TITLES = {"unknown", "неизвестно", "группа", "чат", "none", "-"}


def _group_title_or_placeholder(group: Group, *, for_buyer: bool = False) -> str:
    """Быстрый fallback без обращения к Telegram API.

    Используется в списках, где вызов bot.get_chat на каждую группу
    был бы медленным.

    for_buyer=True — покупателю не раскрываем telegram_chat_id.
    """
    title = (group.title or "").strip()
    if title and title.lower() not in _PLACEHOLDER_TITLES:
        return clean_ui_text(title)
    if for_buyer:
        return "Приватная группа"
    return f"Чат {group.telegram_chat_id}"


async def _resolve_group_title(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    *,
    for_buyer: bool = False,
) -> str:
    """Название группы для UI с fallback-цепочкой (с обращением к Telegram API).

    1. group.title, если непустой и не placeholder
    2. bot.get_chat(telegram_chat_id).title (сохраняет в БД)
    3. bot.get_chat(...).username как @username
    4. for_buyer → "Приватная группа", иначе f"Чат {telegram_chat_id}"

    Обновляет group.title в БД при успешном ответе Telegram.
    """
    # 1. Быстрый путь — уже есть валидный title
    title = (group.title or "").strip()
    if title and title.lower() not in _PLACEHOLDER_TITLES:
        return clean_ui_text(title)

    # 2. Пробуем Telegram API
    try:
        chat = await bot.get_chat(group.telegram_chat_id)
        if chat.title:
            group.title = chat.title
            await session.commit()
            return clean_ui_text(chat.title)
        if chat.username:
            # @username сохраняем только в UI, в БД не пишем (это не title)
            return f"@{chat.username}"
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    # 3. Финальный fallback
    if for_buyer:
        return "Приватная группа"
    return f"Чат {group.telegram_chat_id}"

async def _listing_view(bot: Bot, session: AsyncSession, listing: RequiredAdListing) -> tuple[Group | None, int]:
    group = await session.get(Group, listing.seller_group_id)
    if group is None:
        return None, listing.member_count_snapshot
    current_count = await _member_count(bot, group)
    count = listing.member_count_snapshot if current_count is None else current_count
    if current_count is not None and current_count != listing.member_count_snapshot:
        listing.member_count_snapshot = current_count
        await session.commit()
    return group, count


@router.callback_query(F.data == "ads:sell:required")
async def required_sell_home(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    groups = list((await session.scalars(
        select(Group)
        .where(Group.owner_telegram_id == callback.from_user.id, Group.is_active.is_(True))
        .order_by(Group.title)
    )).all())
    rows = [[InlineKeyboardButton(text=_group_title_or_placeholder(group)[:58], callback_data=f"reqlist:group:{group.id}")] for group in groups]
    rows.append([InlineKeyboardButton(text="📥 Входящие заявки", callback_data="reqdeal:seller")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
    await callback.message.edit_text(
        panel_header(
            "Продать обязательную подписку",
            "Выберите свою группу. Для объявления покупателям показываются только название группы, количество участников, минимальный срок и ваша цена. ID и @username группы не публикуются."
            if groups else "У вас пока нет активных групп Mimoru.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def _render_seller_listing(callback: CallbackQuery, bot: Bot, session: AsyncSession, group: Group) -> None:
    listing = await session.scalar(select(RequiredAdListing).where(RequiredAdListing.seller_group_id == group.id))
    current_count = await _member_count(bot, group)
    count = current_count if current_count is not None else (listing.member_count_snapshot if listing is not None else 0)
    if listing is None:
        text = panel_header(
            "Объявление ОП",
            f"Группа: {await _resolve_group_title(bot, session, group)}\nУчастников: {count:,}\n\nОбъявление ещё не создано.",
        )
        rows = [
            [InlineKeyboardButton(text="➕ Создать объявление", callback_data=f"reqlist:start:{group.id}")],
            [InlineKeyboardButton(text="◀️ К моим группам", callback_data="ads:sell:required")],
        ]
    else:
        if current_count is not None:
            listing.member_count_snapshot = current_count
            await session.commit()
        text = panel_header(
            "Объявление ОП",
            f"Группа: {await _resolve_group_title(bot, session, group)}\n"
            f"Участников: {count:,}\n"
            f"Минимальный срок: {listing.min_days} дн.\n"
            f"Цена: {clean_ui_text(listing.price_text)} {_unit_label(listing.price_unit)}\n"
            f"Статус: {'✅ показывается покупателям' if listing.active else '⏸ скрыто'}",
        )
        rows = [
            [InlineKeyboardButton(text="✏️ Изменить условия", callback_data=f"reqlist:start:{group.id}")],
            [InlineKeyboardButton(text="⏸ Скрыть" if listing.active else "▶️ Опубликовать", callback_data=f"reqlist:toggle:{listing.id}")],
            [InlineKeyboardButton(text="📥 Входящие заявки", callback_data="reqdeal:seller")],
            [InlineKeyboardButton(text="◀️ К моим группам", callback_data="ads:sell:required")],
        ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^reqlist:group:\d+$"))
async def required_listing_group(callback: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    group = await _seller_group(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if group is None:
        await callback.answer("Группа недоступна.", show_alert=True)
        return
    await _render_seller_listing(callback, bot, session, group)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reqlist:start:\d+$"))
async def required_listing_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    group = await _seller_group(session, int(callback.data.split(":")[-1]), callback.from_user.id)
    if group is None:
        await callback.answer("Группа недоступна.", show_alert=True)
        return
    await state.set_state(RequiredListingForm.min_days)
    await state.update_data(group_id=group.id)
    await callback.message.edit_text(
        panel_header("Минимальный срок ОП", "Отправьте минимальное количество дней, на которое вы готовы включить обязательную подписку. От 1 до 365 дней."),
        reply_markup=_back(f"reqlist:group:{group.id}", "✖️ Отменить ввод"),
    )
    await callback.answer()


@router.message(RequiredListingForm.min_days, F.chat.type == "private")
async def required_min_days_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 365:
        await message.answer("Введите целое число от 1 до 365.")
        return
    data = await state.get_data()
    group_id = int(data.get("group_id", 0))
    await state.update_data(min_days=int(raw))
    await message.answer(
        panel_header("Как указывается цена", "Выберите, за что покупатель платит по вашему объявлению."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Цена за сутки", callback_data=f"reqlist:unit:{group_id}:day")],
            [InlineKeyboardButton(text="👤 Цена за подписчика", callback_data=f"reqlist:unit:{group_id}:subscriber")],
            [InlineKeyboardButton(text="✖️ Отменить", callback_data=f"reqlist:group:{group_id}")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^reqlist:unit:\d+:(day|subscriber)$"))
async def required_unit(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, _, raw_group, unit = callback.data.split(":")
    group = await _seller_group(session, int(raw_group), callback.from_user.id)
    data = await state.get_data()
    if group is None or not data.get("min_days"):
        await state.clear()
        await callback.answer("Форма устарела. Начните заново.", show_alert=True)
        return
    await state.set_state(RequiredListingForm.price)
    await state.update_data(group_id=group.id, price_unit=unit)
    await callback.message.edit_text(
        panel_header(
            "Цена объявления",
            f"Отправьте цену {_unit_label(unit)}. Можно написать валюту так, как вы реально договариваетесь с покупателем: например «50 Stars», «5 USDT» или «500 ₽». До 64 символов.",
        ),
        reply_markup=_back(f"reqlist:group:{group.id}", "✖️ Отменить ввод"),
    )
    await callback.answer()


@router.message(RequiredListingForm.price, F.chat.type == "private")
async def required_price_input(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    group = await _seller_group(session, int(data.get("group_id", 0)), message.from_user.id)
    price = clean_ui_text((message.text or "").strip())[:64]
    if group is None:
        await state.clear()
        await message.answer("Группа недоступна.")
        return
    if not price:
        await message.answer("Цена не может быть пустой.")
        return
    listing = await session.scalar(select(RequiredAdListing).where(RequiredAdListing.seller_group_id == group.id))
    current_count = await _member_count(bot, group)
    count = current_count if current_count is not None else (listing.member_count_snapshot if listing is not None else 0)
    if listing is None:
        listing = RequiredAdListing(
            seller_group_id=group.id,
            seller_owner_telegram_id=message.from_user.id,
            member_count_snapshot=count,
            min_days=int(data["min_days"]),
            price_unit=str(data["price_unit"]),
            price_text=price,
            active=True,
        )
        session.add(listing)
    else:
        listing.seller_owner_telegram_id = message.from_user.id
        listing.member_count_snapshot = count
        listing.min_days = int(data["min_days"])
        listing.price_unit = str(data["price_unit"])
        listing.price_text = price
        listing.active = True
    await session.commit()
    await state.clear()
    await message.answer(
        panel_header(
            "Объявление опубликовано",
            f"Группа: {await _resolve_group_title(bot, session, group)}\nУчастников: {count:,}\nМинимальный срок: {listing.min_days} дн.\nЦена: {price} {_unit_label(listing.price_unit)}",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К объявлению", callback_data=f"reqlist:group:{group.id}")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^reqlist:toggle:\d+$"))
async def required_listing_toggle(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    listing = await session.get(RequiredAdListing, int(callback.data.split(":")[-1]))
    if listing is None or listing.seller_owner_telegram_id != callback.from_user.id:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return
    listing.active = not listing.active
    await session.commit()
    group = await session.get(Group, listing.seller_group_id)
    if group is None:
        await callback.answer("Группа недоступна.", show_alert=True)
        return
    await _render_seller_listing(callback, bot, session, group)
    await callback.answer("Статус объявления изменён")


@router.callback_query(F.data == "ads:buy:required")
async def required_market(callback: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    listings = list((await session.scalars(
        select(RequiredAdListing)
        .where(
            RequiredAdListing.active.is_(True),
            RequiredAdListing.seller_owner_telegram_id != callback.from_user.id,
        )
        .order_by(RequiredAdListing.updated_at.desc())
        .limit(30)
    )).all())
    rows: list[list[InlineKeyboardButton]] = []
    visible_count = 0
    for listing in listings:
        group, count = await _listing_view(bot, session, listing)
        if group is None or not group.is_active:
            continue
        label = f"{_group_title_or_placeholder(group, for_buyer=True)[:24]} · {count:,} · {clean_ui_text(listing.price_text)[:18]}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"reqmarket:{listing.id}")])
        visible_count += 1
    rows.append([InlineKeyboardButton(text="📨 Мои запросы", callback_data="reqdeal:buyer")])
    rows.append([InlineKeyboardButton(text="◀️ К покупке рекламы", callback_data="ads:buy")])
    await callback.message.edit_text(
        panel_header(
            "Купить обязательную подписку",
            "Выберите объявление владельца группы. В каталоге специально не показываются ID и @username площадки. До отправки заявки можно напрямую связаться с продавцом и договориться об условиях."
            if visible_count else "Активных объявлений пока нет. Вы можете вернуться позже.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reqmarket:\d+$"))
async def required_market_detail(callback: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    listing = await session.get(RequiredAdListing, int(callback.data.split(":")[-1]))
    if listing is None or not listing.active:
        await callback.answer("Объявление больше недоступно.", show_alert=True)
        return
    group, count = await _listing_view(bot, session, listing)
    if group is None or not group.is_active:
        await callback.answer("Группа больше недоступна.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Предложение обязательной подписки",
            f"Группа: {await _resolve_group_title(bot, session, group, for_buyer=True)}\n"
            f"Участников: {count:,}\n"
            f"Минимальный срок: {listing.min_days} дн.\n"
            f"Цена: {clean_ui_text(listing.price_text)} {_unit_label(listing.price_unit)}\n\n"
            "Сначала можно написать владельцу и согласовать детали. После договорённости отправьте официальный запрос через Mimoru.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с продавцом", url=_contact_url(listing.seller_owner_telegram_id))],
            [InlineKeyboardButton(text="📨 Отправить запрос", callback_data=f"reqdeal:start:{listing.id}")],
            [InlineKeyboardButton(text="◀️ К объявлениям", callback_data="ads:buy:required")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reqdeal:start:\d+$"))
async def required_deal_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    listing = await session.get(RequiredAdListing, int(callback.data.split(":")[-1]))
    if listing is None or not listing.active or listing.seller_owner_telegram_id == callback.from_user.id:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return
    await state.clear()
    await state.update_data(listing_id=listing.id)
    groups = list((await session.scalars(
        select(Group)
        .where(Group.owner_telegram_id == callback.from_user.id, Group.is_active.is_(True))
        .order_by(Group.title)
    )).all())
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        rows.append([InlineKeyboardButton(
            text=_group_title_or_placeholder(group)[:58],
            callback_data=f"reqdeal:pick:{listing.id}:{group.id}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Ввести @username или ссылку вручную", callback_data=f"reqdeal:manual:{listing.id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"reqmarket:{listing.id}")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="ads:home")])
    await callback.message.edit_text(
        panel_header(
            "Куда подключить ОП",
            "Выберите группу, в которой будет действовать обязательная подписка, или введите @username / ссылку t.me вручную."
            if groups else "У вас пока нет активных групп Mimoru. Введите @username или ссылку t.me вручную.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reqdeal:pick:\d+:\d+$"))
async def required_deal_pick(callback: CallbackQuery, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    _, _, raw_listing_id, raw_group_id = callback.data.split(":")
    listing_id = int(raw_listing_id)
    group_id = int(raw_group_id)
    listing = await session.get(RequiredAdListing, listing_id)
    if listing is None or not listing.active or listing.seller_owner_telegram_id == callback.from_user.id:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return
    group = await session.get(Group, group_id)
    if group is None or not group.is_active or group.owner_telegram_id != callback.from_user.id:
        await callback.answer("Группа недоступна.", show_alert=True)
        return
    try:
        tg_chat = await bot.get_chat(group.telegram_chat_id)
        resolved_username = getattr(tg_chat, "username", None)
        invite_link = getattr(tg_chat, "invite_link", None)
    except (TelegramBadRequest, TelegramForbiddenError):
        resolved_username = None
        invite_link = None
    if resolved_username:
        target = f"@{resolved_username}"
    elif invite_link:
        target = invite_link
    else:
        await callback.answer(
            "Эта группа не имеет публичного @username. "
            "Для обязательной подписки нужен публичный канал или группа.",
            show_alert=True,
        )
        return
    duplicate = await session.scalar(select(RequiredAdDealRequest.id).where(
        RequiredAdDealRequest.listing_id == listing.id,
        RequiredAdDealRequest.buyer_telegram_id == callback.from_user.id,
        RequiredAdDealRequest.status == "pending",
    ))
    if duplicate:
        await state.clear()
        await callback.answer("У вас уже есть ожидающий запрос по этому объявлению.", show_alert=True)
        return
    deal = RequiredAdDealRequest(
        listing_id=listing.id,
        buyer_telegram_id=callback.from_user.id,
        seller_telegram_id=listing.seller_owner_telegram_id,
        target_resource=target,
        status="pending",
    )
    session.add(deal)
    await session.commit()
    seller_group = await session.get(Group, listing.seller_group_id)
    group_title = (
        await _resolve_group_title(bot, session, seller_group)
        if seller_group is not None
        else "Группа недоступна"
    )
    buyer_group_title = (
        await _resolve_group_title(bot, session, group)
        if group is not None
        else "Группа покупателя"
    )
    seller_text = panel_header(
        "Новый запрос на ОП",
        f"Ваша группа: {group_title}\n"
        f"Условия объявления: от {listing.min_days} дн., {clean_ui_text(listing.price_text)} {_unit_label(listing.price_unit)}\n\n"
        f"Покупатель хочет подключить: {target}\n"
        f"Группа покупателя: {buyer_group_title}\n\n"
        "Вы можете сначала связаться с покупателем напрямую, а затем принять или отклонить запрос.",
    )
    try:
        await bot.send_message(
            listing.seller_owner_telegram_id,
            seller_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Связаться с покупателем", url=_contact_url(callback.from_user.id))],
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"reqdeal:accept:{deal.id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reqdeal:reject:{deal.id}"),
                ],
                [InlineKeyboardButton(text="📥 Входящие заявки", callback_data="reqdeal:seller")],
            ]),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await state.clear()
    await callback.message.edit_text(
        panel_header("Запрос отправлен", f"Запрос #{deal.id} отправлен владельцу группы «{group_title}». Результат придёт обоим участникам сделки."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с продавцом", url=_contact_url(listing.seller_owner_telegram_id))],
            [InlineKeyboardButton(text="📨 Мои запросы", callback_data="reqdeal:buyer")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^reqdeal:manual:\d+$"))
async def required_deal_manual(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    listing_id = int(callback.data.split(":")[-1])
    listing = await session.get(RequiredAdListing, listing_id)
    if listing is None or not listing.active or listing.seller_owner_telegram_id == callback.from_user.id:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return
    await state.set_state(RequiredDealForm.target)
    await state.update_data(listing_id=listing.id)
    await callback.message.edit_text(
        panel_header(
            "Куда подключить ОП",
            "Отправьте публичный @username, ссылку t.me/username или invite-ссылку t.me/+..."
            "на Telegram-ресурс, который вы согласовали с продавцом.",
        ),
        reply_markup=_back(f"reqmarket:{listing.id}", "✖️ Отменить ввод"),
    )
    await callback.answer()


@router.message(RequiredDealForm.target, F.chat.type == "private")
async def required_deal_target(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    listing = await session.get(RequiredAdListing, int(data.get("listing_id", 0)))
    target = normalize_public_telegram_resource(message.text or "")
    if target is None:
        target = validate_invite_link(message.text or "")
    if listing is None or not listing.active:
        await state.clear()
        await message.answer("Объявление больше недоступно.")
        return
    if target is None:
        await message.answer("Отправьте публичный @username, ссылку t.me/username или invite-ссылку t.me/+...")
        return
    duplicate = await session.scalar(select(RequiredAdDealRequest.id).where(
        RequiredAdDealRequest.listing_id == listing.id,
        RequiredAdDealRequest.buyer_telegram_id == message.from_user.id,
        RequiredAdDealRequest.status == "pending",
    ))
    if duplicate:
        await state.clear()
        await message.answer("У вас уже есть ожидающий запрос по этому объявлению.")
        return
    deal = RequiredAdDealRequest(
        listing_id=listing.id,
        buyer_telegram_id=message.from_user.id,
        seller_telegram_id=listing.seller_owner_telegram_id,
        target_resource=target,
        status="pending",
    )
    session.add(deal)
    await session.commit()
    group = await session.get(Group, listing.seller_group_id)
    current_count = await _member_count(bot, group) if group is not None else None
    count = current_count if current_count is not None else listing.member_count_snapshot
    if group is not None and current_count is not None:
        listing.member_count_snapshot = current_count
        await session.commit()
    group_title = (
        await _resolve_group_title(bot, session, group)
        if group is not None
        else "Группа недоступна"
    )
    seller_text = panel_header(
        "Новый запрос на ОП",
        f"Ваша группа: {group_title}\n"
        f"Участников: {count:,}\n"
        f"Условия объявления: от {listing.min_days} дн., {clean_ui_text(listing.price_text)} {_unit_label(listing.price_unit)}\n\n"
        f"Покупатель хочет подключить: {target}\n\n"
        "Вы можете сначала связаться с покупателем напрямую, а затем принять или отклонить запрос.",
    )
    try:
        await bot.send_message(
            listing.seller_owner_telegram_id,
            seller_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Связаться с покупателем", url=_contact_url(message.from_user.id))],
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"reqdeal:accept:{deal.id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reqdeal:reject:{deal.id}"),
                ],
                [InlineKeyboardButton(text="📥 Входящие заявки", callback_data="reqdeal:seller")],
            ]),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await state.clear()
    await message.answer(
        panel_header("Запрос отправлен", f"Запрос #{deal.id} отправлен владельцу группы «{group_title}». Результат придёт обоим участникам сделки."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с продавцом", url=_contact_url(listing.seller_owner_telegram_id))],
            [InlineKeyboardButton(text="📨 Мои запросы", callback_data="reqdeal:buyer")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^reqdeal:(accept|reject):\d+$"))
async def required_deal_decision(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, decision, raw_id = callback.data.split(":")
    deal = await session.get(RequiredAdDealRequest, int(raw_id))
    if deal is None or deal.seller_telegram_id != callback.from_user.id:
        await callback.answer("Запрос недоступен.", show_alert=True)
        return
    if deal.status != "pending":
        await callback.answer("Запрос уже обработан.", show_alert=True)
        return
    listing = await session.get(RequiredAdListing, deal.listing_id)
    group = await session.get(Group, listing.seller_group_id) if listing is not None else None
    group_title = (
        await _resolve_group_title(bot, session, group, for_buyer=True)
        if group is not None
        else "Группа"
    )
    deal.status = "accepted" if decision == "accept" else "rejected"
    deal.decided_at = datetime.now(timezone.utc)
    await session.commit()
    accepted = decision == "accept"
    result = "✅ Запрос принят" if accepted else "❌ Запрос отклонён"
    seller_extra = (
        f"\n\nКогда будете готовы, включите ОП прямо в своей группе командой: подключить {deal.target_resource} 7 дней или подключить {deal.target_resource} 100 участников. Подтверждение от Mimoru для этой команды не требуется."
        if accepted else ""
    )
    await callback.message.edit_text(
        panel_header(result, f"Группа: {group_title}\nРесурс покупателя: {clean_ui_text(deal.target_resource)}\n\nРезультат отправлен покупателю.{seller_extra}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с покупателем", url=_contact_url(deal.buyer_telegram_id))],
            [InlineKeyboardButton(text="📥 Входящие заявки", callback_data="reqdeal:seller")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )
    try:
        await bot.send_message(
            deal.buyer_telegram_id,
            panel_header(
                result,
                f"Продавец группы «{group_title}» {'принял' if accepted else 'отклонил'} ваш запрос #{deal.id}.\n"
                f"Ресурс: {clean_ui_text(deal.target_resource)}"
                + ("\n\nМожно связаться с продавцом напрямую и завершить согласованные действия." if accepted else ""),
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Связаться с продавцом", url=_contact_url(deal.seller_telegram_id))],
                [InlineKeyboardButton(text="📨 Мои запросы", callback_data="reqdeal:buyer")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await callback.answer("Решение сохранено")


async def _render_deals(callback: CallbackQuery, session: AsyncSession, *, seller: bool) -> None:
    if seller:
        items = list((await session.scalars(
            select(RequiredAdDealRequest)
            .where(RequiredAdDealRequest.seller_telegram_id == callback.from_user.id)
            .order_by(RequiredAdDealRequest.created_at.desc())
            .limit(30)
        )).all())
        back = "ads:sell:required"
        title = "Входящие заявки ОП"
    else:
        items = list((await session.scalars(
            select(RequiredAdDealRequest)
            .where(RequiredAdDealRequest.buyer_telegram_id == callback.from_user.id)
            .order_by(RequiredAdDealRequest.created_at.desc())
            .limit(30)
        )).all())
        back = "ads:buy:required"
        title = "Мои запросы ОП"
    icons = {"pending": "⏳", "accepted": "✅", "rejected": "❌", "cancelled": "🚫", "activated": "🟢"}
    lines = [f"{icons.get(item.status, '•')} #{item.id} · {clean_ui_text(item.target_resource)[:45]}" for item in items]
    await callback.message.edit_text(
        panel_header(title, "\n".join(lines) if lines else "Заявок пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=back)],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "reqdeal:buyer")
async def required_buyer_deals(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await _render_deals(callback, session, seller=False)


@router.callback_query(F.data == "reqdeal:seller")
async def required_seller_deals(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await _render_deals(callback, session, seller=True)


# ---------------------------------------------------------------------------
# Compatibility for old buttons already present in users' Telegram history.
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "ads:sell:post")
async def legacy_sell_post_redirect(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        panel_header(
            "Рекламные посты изменены",
            "Отдельная продажа рекламных постов по группам больше не используется. Теперь рекламный пост проверяет создатель Mimoru, после оплаты он автоматически публикуется по всей активной сети групп.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📣 Создать рекламный пост", callback_data="ads:buy:post")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ads:placement:\d+$"))
async def group_advertising_redirect(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(
        panel_header(
            "Реклама группы",
            "Для этой группы можно управлять обычной обязательной подпиской. Покупка и продажа рекламы находятся в общем рекламном кабинете Mimoru.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Обязательная подписка", callback_data=f"channels:{group_id}")],
            [InlineKeyboardButton(text="📢 Открыть рекламу", callback_data="ads:home")],
            [InlineKeyboardButton(text="◀️ К группе", callback_data=f"group:{group_id}")],
        ]),
    )
    await callback.answer()
