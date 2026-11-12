from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ad_market_models import GlobalPostRequest
from app.services.ui import panel_header

router = Router(name=__name__)


@router.callback_query(F.data == "gpost:mine", F.message.invoice)
async def global_post_invoice_back(callback: CallbackQuery, session: AsyncSession) -> None:
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    rows = list((await session.scalars(
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
    text = "\n".join(
        f"#{item.id} · {labels.get(item.status, item.status)} · {item.price_stars} ₽"
        for item in rows
    ) if rows else "Заявок пока нет."
    await callback.bot.send_message(
        callback.from_user.id,
        panel_header("Мои рекламные посты", text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Новый рекламный пост", callback_data="ads:buy:post")],
            [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
        ]),
    )
    await callback.answer()
