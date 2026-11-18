import asyncio
import logging
from typing import Dict, Any, Optional
from aiogram import Bot

import config
from database.db import db
from bot.handlers.admin import send_lot_to_group
from scanner.filters import (
    FILTER_CHEAP, FILTER_MEDIUM, FILTER_EXPENSIVE,
    FILTER_GIRLS, FILTER_UPGRADED
)

logger = logging.getLogger(__name__)

class TopicDispatcher:
    """
    Высокоскоростной диспетчер очередей (Professional Round-Robin Dispatcher):
    - 5 независимых очередей (по одной на каждый топик)
    - Строго поочередная отправка: cheap -> upgraded -> girls -> medium -> expensive -> cheap...
    - Быстрый интервал между сообщениями (2.0 - 2.5 секунды)
    - Ни один топик не простаивает
    - Защита от дублей
    """
    def __init__(self):
        self.queues: Dict[str, asyncio.Queue] = {
            FILTER_CHEAP: asyncio.Queue(),
            FILTER_UPGRADED: asyncio.Queue(),
            FILTER_GIRLS: asyncio.Queue(),
            FILTER_MEDIUM: asyncio.Queue(),
            FILTER_EXPENSIVE: asyncio.Queue()
        }
        self.seen_identifiers = set()
        self.is_running = False
        self.bot: Optional[Bot] = None
        self.send_interval = 0.2  # Ультрабыстрая отправка лотов (0.2с)

    async def push_lot(self, lot: Dict[str, Any]):
        """Добавляет лот в соответствующую очередь топика."""
        flt = lot.get("filter_type")
        if flt not in self.queues:
            return

        ident = lot.get("gift_identifier")
        if ident and ident in self.seen_identifiers:
            return

        if ident:
            self.seen_identifiers.add(ident)

        # Ограничиваем размер очереди до 50 лотов, чтобы не копить устаревшие
        if self.queues[flt].qsize() > 50:
            try:
                self.queues[flt].get_nowait()
            except Exception:
                pass

        await self.queues[flt].put(lot)
        logger.debug(f"Лот добавлен в очередь [{flt}]: {lot.get('gift_name')} (в очереди: {self.queues[flt].qsize()})")

    async def start(self, bot: Bot):
        """Запускает непрерывный цикл диспетчеризации по всем 5 топикам."""
        self.bot = bot
        self.is_running = True
        logger.info("🚀 Высокоскоростной диспетчер топиков запущен!")

        # Порядок ротации строго по кругу: 3 основных раздела + девушки и крафт
        TOPIC_ROTATION = [
            FILTER_CHEAP,
            FILTER_MEDIUM,
            FILTER_EXPENSIVE,
            FILTER_GIRLS,
            FILTER_UPGRADED
        ]

        rot_idx = 0

        while self.is_running:
            target_flt = TOPIC_ROTATION[rot_idx % len(TOPIC_ROTATION)]
            rot_idx += 1

            queue = self.queues[target_flt]

            # Если для данного топика есть лот — немедленно отправляем
            if not queue.empty():
                try:
                    lot = await queue.get()
                    await send_lot_to_group(self.bot, lot)
                    logger.info(f"⚡️ [БЫСТРАЯ ОТПРАВКА] Топик: [{target_flt}] -> {lot.get('gift_name')} (@{lot.get('owner_username')})")
                    await asyncio.sleep(self.send_interval)
                    continue
                except Exception as e:
                    logger.error(f"Ошибка диспетчера при отправке {target_flt}: {e}")
            else:
                # Если в текущем топике пусто, быстро проверяем остальные очереди без долгой паузы
                found_any = False
                for other_flt in TOPIC_ROTATION:
                    if not self.queues[other_flt].empty():
                        try:
                            lot = await self.queues[other_flt].get()
                            await send_lot_to_group(self.bot, lot)
                            logger.info(f"⚡️ [БЫСТРАЯ ОТПРАВКА] Топик: [{other_flt}] -> {lot.get('gift_name')} (@{lot.get('owner_username')})")
                            await asyncio.sleep(self.send_interval)
                            found_any = True
                            break
                        except Exception as e:
                            logger.error(f"Ошибка диспетчера при отправке {other_flt}: {e}")
                
                if not found_any:
                    # Если все очереди пока пусты, мгновенная проверка через 0.05 сек
                    await asyncio.sleep(0.05)

    def stop(self):
        self.is_running = False

topic_dispatcher = TopicDispatcher()
