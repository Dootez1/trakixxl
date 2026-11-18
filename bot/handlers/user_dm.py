import os
from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.filters import Command

from database.db import db
from bot.formatters import format_lot_message
from bot.keyboards import get_claimed_lot_dm_keyboard
import config

dm_router = Router()

@dm_router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    await db.register_user(user_id, username, first_name)
    await message.answer("✅ Бот готов к работе. Занятые лоты будут приходить сюда.")

@dm_router.message(Command("my_lots"))
async def cmd_my_lots(message: Message):
    lots = await db.get_user_claimed_lots(message.from_user.id, limit=5)
    if not lots:
        await message.answer("У вас пока нет занятых лотов.")
        return

    await message.answer(f"📋 <b>Ваши последние занятые лоты ({len(lots)}):</b>", parse_mode="HTML")
    for lot in lots:
        text = format_lot_message(lot, show_claim_info=False, is_dm=True)
        kb = get_claimed_lot_dm_keyboard(
            username=lot.get("owner_username"),
            user_id=lot.get("owner_tg_id"),
            gift_link=lot.get("gift_link")
        )
        await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

@dm_router.message(Command("stats"))
async def cmd_stats(message: Message):
    user = await db.get_user(message.from_user.id)
    claims = user.get("claims_count", 0) if user else 0
    await message.answer(
        f"📊 <b>Ваша статистика:</b>\n\n"
        f"• Всего успешно занято лотов: <b>{claims}</b>\n"
        f"• Кулдаун между лотами: <b>{config.CLAIM_COOLDOWN_SECONDS} сек.</b>",
        parse_mode="HTML"
    )

@dm_router.callback_query(F.data.startswith("release_"))
async def handle_release_lot(callback, bot: Bot):
    """Освобождает лот и возвращает его в группу для других воркеров."""
    lot_id_str = callback.data.split("_")[1]
    try:
        lot_id = int(lot_id_str)
        user_id = callback.from_user.id
        success, lot, err = await db.release_lot(lot_id, user_id)
        if not success:
            await callback.answer(err, show_alert=True)
            return

        # 1. Обновляем клавиатуру в ЛС
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("↩️ Лот успешно освобожден и возвращен в группу!", show_alert=True)

        # 2. Возвращаем кнопку 'Занять лот' в сообщение группы
        from bot.keyboards import get_lot_keyboard
        group_msg_id = lot.get("group_message_id")
        if group_msg_id and config.TELEGRAM_GROUP_ID:
            try:
                lot_kb = get_lot_keyboard(
                    lot_id=lot_id,
                    username=lot.get("owner_username"),
                    user_id=lot.get("owner_tg_id"),
                    gift_link=lot.get("market_link") or lot.get("gift_link")
                )
                await bot.edit_message_reply_markup(
                    chat_id=config.TELEGRAM_GROUP_ID,
                    message_id=group_msg_id,
                    reply_markup=lot_kb
                )
            except Exception:
                pass
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)

@dm_router.message(Command("top"))
async def cmd_top(message: Message):
    """Лидерборд воркеров команды."""
    top_users = await db.get_top_claimers(limit=10)
    if not top_users:
        await message.answer("🏆 Лидерборд пуст: пока никто не занимал лоты.")
        return

    lines = ["🏆 <b>ТОП ВОРКЕРОВ HASTER TEAM:</b>\n"]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, u in enumerate(top_users):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        username = f"@{u['username']}" if u.get("username") else u.get("first_name", "Воркер")
        count = u.get("claims_count", 0)
        lines.append(f"{medal} <b>{username}</b> — <b>{count}</b> лотов")

    await message.answer("\n".join(lines), parse_mode="HTML")
