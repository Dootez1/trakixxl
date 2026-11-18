import logging
from datetime import datetime, timezone, timedelta
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

import config
from database.db import db
from bot.keyboards import get_claimed_lot_dm_keyboard, get_claimed_group_keyboard
from bot.formatters import format_lot_message

MSK = timezone(timedelta(hours=3))
logger = logging.getLogger(__name__)
group_router = Router()

@group_router.callback_query(F.data.startswith("claim_"))
async def handle_claim_lot(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.first_name
    lot_id_str = callback.data.split("_")[1]

    try:
        lot_id = int(lot_id_str)
    except ValueError:
        await callback.answer("Ошибка в ID лота.", show_alert=True)
        return

    # 1. Проверка кулдауна 5 секунд
    can_claim, remaining = await db.check_and_update_cooldown(user_id, config.CLAIM_COOLDOWN_SECONDS)
    if not can_claim:
        await callback.answer(
            f"⏳ Кулдаун! Подождите {remaining} сек. перед занятием следующего лота.",
            show_alert=True
        )
        return

    # 2. Атомарное бронирование лота
    success, lot, err_msg = await db.claim_lot(lot_id, user_id, callback.from_user.username)
    if not success:
        # Лот уже занят другим воркером - показываем информативный алерт
        lot_data = await db.get_lot(lot_id)
        if lot_data:
            claimed_by = lot_data.get("claimed_by_username") or "воркер"
            claimer_str = f"@{claimed_by}" if not claimed_by.startswith("@") else claimed_by
            claim_time = lot_data.get("claimed_at") or datetime.now(MSK).strftime("%H:%M:%S")
            alert_text = (
                f"❌ Данный лот уже занял другой воркер, попробуйте занять другой лот.\n\n"
                f"👤 Кто, и во сколько занял лот — {claimer_str}, {claim_time}.\n\n"
                f"❗️ Этот лот уже занят в избежании кражи мамонта у другого воркера."
            )
        else:
            alert_text = err_msg
        await callback.answer(alert_text, show_alert=True)
        return

    # Моментальный ответ на нажатие (убирает крутилку с кнопки за 0.01 сек)
    await callback.answer("✅ Вы заняли этот лот! Отправил контакт в ЛС.", show_alert=False)

    # 3. Отправка лота в ЛС занявшему пользователю со всей информацией и кнопками
    dm_text = (
        f"<b>Вы успешно заняли новый лот!</b>\n\n"
        f"{format_lot_message(lot, show_claim_info=False, is_dm=True)}\n\n"
        f"⚡️ <i>Свяжитесь с владельцем прямо сейчас по кнопке ниже:</i>"
    )
    dm_kb = get_claimed_lot_dm_keyboard(
        username=lot.get("owner_username"),
        lot_id=lot.get("id"),
        user_id=lot.get("owner_tg_id"),
        gift_link=lot.get("gift_link")
    )

    try:
        gif_url = lot.get("gift_image")
        if gif_url:
            await bot.send_animation(
                chat_id=user_id,
                animation=gif_url,
                caption=dm_text,
                reply_markup=dm_kb,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=user_id,
                text=dm_text,
                reply_markup=dm_kb,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
    except TelegramForbiddenError:
        # Пользователь не запустил бота в ЛС
        await callback.answer(
            "⚠️ Вы заняли лот, но бот не смог отправить его в ЛС!\n"
            "Пожалуйста, запустите бота @TrakiXXlss_bot в личных сообщениях командой /start",
            show_alert=True
        )
    except Exception as e:
        logger.error(f"Не удалось отправить лот {lot_id} в ЛС {user_id}: {e}")

    # 4. Обновление сообщения в группе:
    # Информация о мамонте НЕ СТИРАЕТСЯ (текст остаётся полностью нетронутым)!
    # Меняется только кнопка на [ ✅ Вы успешно заняли этот лот ]
    # Кнопки "Написать продавцу" и "Открыть на маркете" сохраняются!
    claimed_kb = get_claimed_group_keyboard(
        lot_id=lot_id,
        username=lot.get("owner_username"),
        user_id=lot.get("owner_tg_id"),
        gift_link=lot.get("gift_link")
    )

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=claimed_kb)
        except TelegramBadRequest as e:
            logger.debug(f"Не удалось обновить клавиатуру сообщения в группе: {e}")

@group_router.callback_query(F.data.startswith("claimed_info_"))
async def handle_claimed_info(callback: CallbackQuery):
    """
    Реакция на кнопку '✅ Вы успешно заняли этот лот' в группе.
    У того, кто занял: уведомление без ошибки ("✅ Вы успешно заняли этот лот!").
    У тех, кто НЕ занимал: всплывающий алерт (show_alert=True) о том, что лот занят другим воркером.
    """
    lot_id_str = callback.data.split("_")[2]
    try:
        lot_id = int(lot_id_str)
        lot = await db.get_lot(lot_id)
        if not lot:
            await callback.answer("Лот не найден.", show_alert=True)
            return

        claimer = lot.get("claimed_by_username") or "воркер"
        claimer_str = f"@{claimer}" if not claimer.startswith("@") else claimer
        claim_time = lot.get("claimed_at") or datetime.now(MSK).strftime("%H:%M:%S")

        if lot.get("claimed_by_id") == callback.from_user.id:
            await callback.answer(
                "✅ Вы успешно заняли этот лот!\n\n"
                "Все данные сохранены, кнопки для связи с продавцом доступны прямо под постом и в ЛС бота.",
                show_alert=False
            )
        else:
            await callback.answer(
                f"❌ Данный лот уже занял другой воркер, попробуйте занять другой лот.\n\n"
                f"👤 Кто, и во сколько занял лот — {claimer_str}, {claim_time}.\n\n"
                f"❗️ Этот лот мы скрыли в избежании кражи мамонта у другого воркера.",
                show_alert=True
            )
    except Exception as e:
        logger.error(f"Ошибка в handle_claimed_info: {e}")
        await callback.answer("Этот лот уже занят другим воркером.", show_alert=True)
