import random
import logging
from aiogram import Router, Bot, F
from aiogram.types import Message
from aiogram.filters import Command

import config
from database.db import db
from bot.keyboards import get_lot_keyboard
from bot.formatters import format_lot_message
from scanner.filters import (
    FILTER_CHEAP, FILTER_MEDIUM, FILTER_EXPENSIVE,
    FILTER_GIRLS, FILTER_UPGRADED, get_topic_id_for_filter
)

admin_router = Router()
logger = logging.getLogger(__name__)

def update_env_variable(key: str, value: str):
    """Обновляет значение переменной в .env файле."""
    env_path = config.BASE_DIR / ".env"
    if not env_path.exists():
        return
    content = env_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

async def send_lot_to_group(bot: Bot, lot_data: dict, target_chat_id: int = None) -> bool:
    """Публикует карточку лота в группу (в соответствующий топик)."""
    group_id = target_chat_id or config.TELEGRAM_GROUP_ID
    if not group_id:
        logger.warning("TELEGRAM_GROUP_ID не настроен!")
        return False

    thread_id = get_topic_id_for_filter(lot_data.get("filter_type"))
    text = format_lot_message(lot_data)
    kb = get_lot_keyboard(
        lot_id=lot_data["id"],
        username=lot_data.get("owner_username"),
        user_id=lot_data.get("owner_tg_id"),
        gift_link=lot_data.get("market_link") or lot_data.get("gift_link"),
        owner_name=lot_data.get("owner_name")
    )

    try:
        from aiogram.types import LinkPreviewOptions
        gift_link = lot_data.get("gift_link") or f"https://t.me/nft/{lot_data.get('gift_identifier')}"
        lpo = LinkPreviewOptions(
            url=lot_data.get("gift_image") or gift_link,
            is_disabled=False,
            prefer_large_media=True,
            show_above_text=False
        )
        kwargs = {
            "chat_id": group_id,
            "text": text,
            "reply_markup": kb,
            "parse_mode": "HTML",
            "link_preview_options": lpo
        }
        if thread_id and thread_id > 0:
            kwargs["message_thread_id"] = thread_id
        msg = await bot.send_message(**kwargs)

        await db.update_lot_message(lot_data["id"], msg.message_id, thread_id or 0)
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить лот в группу {group_id} (топик {thread_id}): {e}")
        return False

@admin_router.message(Command("set_group"))
async def cmd_set_group(message: Message):
    """Привязывает группу к трекеру."""
    chat_id = message.chat.id
    config.TELEGRAM_GROUP_ID = chat_id
    update_env_variable("TELEGRAM_GROUP_ID", str(chat_id))
    await message.reply(
        f"✅ <b>Группа успешно привязана к трекеру!</b>\n"
        f"ID группы: <code>{chat_id}</code>\n\n"
        f"Теперь вы можете настроить топики форума (если есть темы):\n"
        f"Напишите в нужных темах команды:\n"
        f"• <code>/set_cheap</code> — для дешёвых (1-5)\n"
        f"• <code>/set_medium</code> — для средних (5-20)\n"
        f"• <code>/set_expensive</code> — для дорогих (20-100)\n"
        f"• <code>/set_girls</code> — для подарков девушек\n"
        f"• <code>/set_upgraded</code> — для улучшений за звёзды\n\n"
        f"Либо сразу запустите <code>/test_lot</code> для проверки!",
        parse_mode="HTML"
    )

@admin_router.message(Command("set_cheap"))
async def cmd_set_cheap(message: Message):
    tid = message.message_thread_id or 0
    config.TOPIC_CHEAP_ID = tid
    update_env_variable("TOPIC_CHEAP_ID", str(tid))
    await message.reply(f"✅ Топик <b>Дешёвые подарки (1-5)</b> привязан к теме ID: <code>{tid}</code>", parse_mode="HTML")

@admin_router.message(Command("set_medium"))
async def cmd_set_medium(message: Message):
    tid = message.message_thread_id or 0
    config.TOPIC_MEDIUM_ID = tid
    update_env_variable("TOPIC_MEDIUM_ID", str(tid))
    await message.reply(f"✅ Топик <b>Средние подарки (5-20)</b> привязан к теме ID: <code>{tid}</code>", parse_mode="HTML")

@admin_router.message(Command("set_expensive"))
async def cmd_set_expensive(message: Message):
    tid = message.message_thread_id or 0
    config.TOPIC_EXPENSIVE_ID = tid
    update_env_variable("TOPIC_EXPENSIVE_ID", str(tid))
    await message.reply(f"✅ Топик <b>Дорогие подарки (20-100)</b> привязан к теме ID: <code>{tid}</code>", parse_mode="HTML")

@admin_router.message(Command("set_girls"))
async def cmd_set_girls(message: Message):
    tid = message.message_thread_id or 0
    config.TOPIC_GIRLS_ID = tid
    update_env_variable("TOPIC_GIRLS_ID", str(tid))
    await message.reply(f"✅ Топик <b>Подарки девушек (до 100)</b> привязан к теме ID: <code>{tid}</code>", parse_mode="HTML")

@admin_router.message(Command("set_upgraded"))
async def cmd_set_upgraded(message: Message):
    tid = message.message_thread_id or 0
    config.TOPIC_UPGRADED_ID = tid
    update_env_variable("TOPIC_UPGRADED_ID", str(tid))
    await message.reply(f"✅ Топик <b>Улучшения за звёзды</b> привязан к теме ID: <code>{tid}</code>", parse_mode="HTML")

@admin_router.message(Command("group_info"))
async def cmd_group_info(message: Message):
    """Показывает текущие настройки группы."""
    text = (
        f"⚙️ <b>Текущие настройки привязки:</b>\n\n"
        f"• ID группы: <code>{config.TELEGRAM_GROUP_ID}</code>\n"
        f"• Топик 'Дешёвые': <code>{config.TOPIC_CHEAP_ID}</code>\n"
        f"• Топик 'Средние': <code>{config.TOPIC_MEDIUM_ID}</code>\n"
        f"• Топик 'Дорогие': <code>{config.TOPIC_EXPENSIVE_ID}</code>\n"
        f"• Топик 'Девушки': <code>{config.TOPIC_GIRLS_ID}</code>\n"
        f"• Топик 'Улучшения': <code>{config.TOPIC_UPGRADED_ID}</code>\n"
        f"• Кулдаун: <b>{config.CLAIM_COOLDOWN_SECONDS} сек.</b>\n"
        f"• Действие при занятии: <b>{config.CLAIM_ACTION}</b>"
    )
    await message.reply(text, parse_mode="HTML")

@admin_router.message(Command("test_lot"))
async def cmd_test_lot(message: Message, bot: Bot):
    """Проверяет отправку реальных лотов из базы трекера в группу."""
    target_chat = config.TELEGRAM_GROUP_ID
    if not target_chat or target_chat == 0:
        if message.chat.type in ["group", "supergroup"]:
            target_chat = message.chat.id
            config.TELEGRAM_GROUP_ID = target_chat
            update_env_variable("TELEGRAM_GROUP_ID", str(target_chat))
        else:
            await message.answer(
                "⚠️ Группа еще не настроена!\n"
                "Добавьте бота в группу и отправьте там команду <code>/set_group</code>.",
                parse_mode="HTML"
            )
            return

    await message.answer("🔍 Ищу реальные проверенные NFT-подарки из базы для теста отправки...")
    targets = await db.get_pending_targets(limit=10)
    if not targets:
        await message.answer("ℹ️ В очереди нет непроверенных контактов. Сканнер работает в фоновом режиме по живым чатам.")
        return

    from scanner.base_loader import base_manager
    sent = 0
    async def _callback(lot):
        nonlocal sent
        sent += 1
        await send_lot_to_group(bot, lot, target_chat_id=target_chat)

    # Запускаем проверку одной порции
    await base_manager.run_worker(_callback)
    await message.answer(f"✅ Проверка завершена! Найдено и отправлено реальных лотов: <b>{sent}</b> шт.", parse_mode="HTML")
