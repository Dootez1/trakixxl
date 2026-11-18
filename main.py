import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import config
from database.db import db
from bot.handlers.group import group_router
from bot.handlers.user_dm import dm_router
from bot.handlers.admin import admin_router, send_lot_to_group
from bot.handlers.channel_parser import parser_router
from scanner.profile_checker import profile_checker
from scanner.ton_scanner import ton_scanner
from scanner.see_scanner import see_scanner
from scanner.base_loader import base_manager
from scanner.dispatcher import topic_dispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TrekerMain")

async def on_new_lot_detected(lot_data: dict, bot: Bot):
    """Помещает найденный лот в очередь высокоскоростного диспетчера топиков."""
    try:
        await topic_dispatcher.push_lot(lot_data)
    except Exception as e:
        logger.error(f"Ошибка постановки лота в очередь: {e}")

async def main():
    logger.info("Инициализация трекера NFT-подарков...")

    if not config.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не указан в .env файле! Задайте токен перед запуском.")
        print("\n" + "!" * 60)
        print("ВНИМАНИЕ: Создайте файл .env на основе .env.example и укажите BOT_TOKEN!")
        print("!" * 60 + "\n")
        return

    # 1. Инициализация базы данных
    await db.init()
    logger.info("База данных SQLite инициализирована.")

    # 2. Инициализация Telethon профиль-чекера
    await profile_checker.init()

    # 3. Настройка aiogram бота
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    ton_scanner.bot = bot
    dp = Dispatcher()

    # Подключаем роутеры
    dp.include_router(group_router)
    dp.include_router(dm_router)
    dp.include_router(admin_router)
    dp.include_router(parser_router)

    # 4. Запуск фоновых задач
    # see_scanner — официальный live WebSocket поток с маркета (мгновенные листинги)
    # topic_dispatcher — поочередная отправка 1 лот в 3 сек по топикам
    async def see_scanner_wrapper():
        await see_scanner.run_loop(lambda lot: on_new_lot_detected(lot, bot))

    asyncio.create_task(see_scanner_wrapper())
    asyncio.create_task(topic_dispatcher.start(bot))

    logger.info("Бот запущен и ожидает событий. Нажмите Ctrl+C для остановки.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        topic_dispatcher.stop()
        see_scanner.stop()
        ton_scanner.stop()
        base_manager.is_processing = False
        await profile_checker.close()
        await bot.session.close()
        logger.info("Работа трекера корректно завершена.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
