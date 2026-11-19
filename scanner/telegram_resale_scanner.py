import asyncio
import logging
import time
from typing import Dict, Any, Optional, Callable, Set, List
from telethon import types
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetUniqueStarGiftRequest

import config
from database.db import db
from scanner.session_pool import session_pool
from scanner.profile_checker import profile_checker
from scanner.deanonymizer import deanonymize_lot, BLACKLISTED_HANDLES
from scanner.filters import (
    determine_filters, FILTER_CHEAP, FILTER_MEDIUM,
    FILTER_EXPENSIVE, FILTER_GIRLS, FILTER_UPGRADED,
    is_onchain_or_invalid_owner, is_wallet_string
)
from scanner.gift_catalog_ids import (
    CHEAP_GIFT_IDS, MEDIUM_GIFT_IDS, EXPENSIVE_GIFT_IDS, GIRL_GIFT_IDS
)

logger = logging.getLogger("TelegramResaleScanner")

class TelegramResaleScanner:
    """
    Нативный сканер вторичного рынка (ресейла) Telegram MTProto:
    - 100% официальный прямой протокол Telegram (GetResaleStarGiftsRequest).
    - Полностью БЕСПЛАТНО (работает через пул сессий Telethon).
    - Нулевой мусор: строго отсекает блокчейн-кошельки, каналы, группы, ботов и китов (> 5 подарков).
    - Строгая дедупликация: ни один лот не дублируется.
    - В топиках продавец скрыт; контакт передается только в ЛС взявшему воркеру.
    """
    def __init__(self):
        self.is_running = False
        self.seen_slugs: Set[str] = set()
        self.recent_sellers: Dict[str, float] = {}
        # Объединенный список всех коллекций подарков для поочередного опроса
        all_ids = []
        for gid in CHEAP_GIFT_IDS + MEDIUM_GIFT_IDS + EXPENSIVE_GIFT_IDS + GIRL_GIFT_IDS:
            if gid not in all_ids:
                all_ids.append(gid)
        self.catalog_gift_ids: List[int] = all_ids

    async def init(self):
        try:
            self.seen_slugs = await db.get_all_seen_slugs()
            logger.info(f"⚡️ [TelegramResaleScanner] Загружено {len(self.seen_slugs)} ранее виденных слагов.")
        except Exception as e:
            logger.warning(f"Ошибка загрузки seen_slugs: {e}")
            self.seen_slugs = set()

    def _extract_attr_name(self, attr_obj: Any, default_val: str = "Original") -> str:
        if not attr_obj:
            return default_val
        name = getattr(attr_obj, 'name', None) or default_val
        rarity_permille = None
        if hasattr(attr_obj, 'rarity') and hasattr(attr_obj.rarity, 'permille'):
            rarity_permille = attr_obj.rarity.permille
        elif hasattr(attr_obj, 'rarity_permille'):
            rarity_permille = attr_obj.rarity_permille
        if rarity_permille is not None:
            return f"{name} ({rarity_permille / 10.0:g}%)"
        return str(name)

    async def process_resale_item(self, g: types.StarGiftUnique, notify_callback: Callable):
        slug = getattr(g, 'slug', None)
        if not slug:
            return

        if slug in self.seen_slugs or await db.is_slug_seen(slug):
            return

        # 1. СТРОГИЙ ОТСЕВ БЛОКЧЕЙНА И КОШЕЛЬКОВ
        raw_owner_name = getattr(g, 'owner_name', '') or ''
        raw_owner_addr = getattr(g, 'owner_address', None)
        gift_addr = getattr(g, 'gift_address', None)

        if (
            is_onchain_or_invalid_owner(
                owner_name=raw_owner_name,
                owner_address=raw_owner_addr,
                owner_id=getattr(g, 'owner_id', None),
                gift_obj=g
            )
            or is_wallet_string(raw_owner_name)
            or is_wallet_string(raw_owner_addr)
            or gift_addr is not None
            or raw_owner_addr is not None
        ):
            self.seen_slugs.add(slug)
            await db.mark_slugs_seen([slug])
            return

        # 2. РАСЧЕТ ЦЕНЫ (из StarsTonAmount или StarsAmount)
        price_gram = 0.0
        stars_price = 0
        is_crafted = bool(getattr(g, 'crafted', False))

        resell_amount = getattr(g, 'resell_amount', []) or []
        for r in resell_amount:
            if isinstance(r, types.StarsTonAmount):
                price_gram = round(r.amount / 1e9, 2)
            elif isinstance(r, types.StarsAmount):
                stars_price = int(r.amount)

        if price_gram <= 0 and stars_price > 0:
            price_gram = round(stars_price / 72.0, 2)
        elif price_gram > 0 and stars_price <= 0:
            stars_price = int(round(price_gram * 70))

        # Ограничение диапазона: от 0.1 до 100 GRAM
        if price_gram < 0.1 or price_gram > 100.0:
            self.seen_slugs.add(slug)
            return

        # 3. АТРИБУТЫ ПОДАРКА
        title = getattr(g, 'title', '') or 'Telegram Gift'
        num = getattr(g, 'num', 0)
        full_name = f"{title} #{num}" if num else title

        model_name = "Original"
        backdrop_name = "Persimmon"
        pattern_name = "Classic"

        for attr in getattr(g, 'attributes', []):
            if isinstance(attr, types.StarGiftAttributeModel):
                if getattr(attr, 'crafted', False):
                    is_crafted = True
                model_name = self._extract_attr_name(attr, "Original")
            elif isinstance(attr, types.StarGiftAttributePattern):
                pattern_name = self._extract_attr_name(attr, "Classic")
            elif isinstance(attr, types.StarGiftAttributeBackdrop):
                backdrop_name = self._extract_attr_name(attr, "Persimmon")

        # 4. ДЕ-АНОНИМИЗАЦИЯ И ПОЛУЧЕНИЕ ВЛАДЕЛЬЦА ЧЕРЕЗ MTPROTO
        target_username = None
        target_tg_id = None
        user_gifts_count = 1

        # Запрашиваем карточку MTProto
        try:
            res = await session_pool.execute_with_retry(
                lambda c: c(GetUniqueStarGiftRequest(slug=slug))
            )
            if res and getattr(res, 'gift', None):
                mtproto_gift = res.gift
                if (
                    is_onchain_or_invalid_owner(
                        owner_name=getattr(mtproto_gift, 'owner_name', None),
                        owner_address=getattr(mtproto_gift, 'owner_address', None),
                        owner_id=getattr(mtproto_gift, 'owner_id', None),
                        gift_obj=mtproto_gift
                    )
                    or is_wallet_string(getattr(mtproto_gift, 'owner_name', None))
                    or getattr(mtproto_gift, 'gift_address', None)
                    or getattr(mtproto_gift, 'owner_address', None)
                ):
                    self.seen_slugs.add(slug)
                    await db.mark_slugs_seen([slug])
                    return

                deanonym_res = await deanonymize_lot(None, mtproto_gift, users_map=getattr(res, 'users', None))
                if deanonym_res:
                    cand_id, cand_user, _, cnt = deanonym_res
                    if 1 <= cnt <= 5 and cand_user:
                        target_tg_id = cand_id
                        target_username = cand_user
                        user_gifts_count = cnt
        except Exception as de:
            logger.debug(f"MTProto деанонимизация {slug}: {de}")

        # Если подтверждённый продавец с @username не найден — лот не публикуется
        if not target_username:
            self.seen_slugs.add(slug)
            return

        clean_target = target_username.strip().lstrip("@").lower()
        if clean_target in BLACKLISTED_HANDLES or clean_target.endswith("bot") or is_wallet_string(clean_target):
            self.seen_slugs.add(slug)
            return

        # 5. Продавец уже 100% подтверждён (1 <= user_gifts_count <= 5)
        from scanner.profile_checker import detect_is_female
        is_fem = detect_is_female(raw_owner_name, "", clean_target, "")
        can_write_free = 1
        prem_level = 0

        # Защита от спама одним и тем же пользователем (кулдаун 10 минут)
        seller_key = clean_target.lower()
        if time.time() - self.recent_sellers.get(seller_key, 0) < 600:
            return
        self.recent_sellers[seller_key] = time.time()

        # 6. ОПРЕДЕЛЕНИЕ ТОПИКА
        target_topic = None
        if is_crafted:
            target_topic = FILTER_UPGRADED
        elif is_fem and price_gram <= 100.0:
            target_topic = FILTER_GIRLS
        elif 0.1 <= price_gram <= 10.0:
            target_topic = FILTER_CHEAP
        elif 10.01 <= price_gram <= 20.0:
            target_topic = FILTER_MEDIUM
        elif 20.01 <= price_gram <= 100.0:
            target_topic = FILTER_EXPENSIVE

        if not target_topic:
            self.seen_slugs.add(slug)
            return

        # 7. СОХРАНЕНИЕ И ОТПРАВКА ЛОТА
        lot_dict = {
            "gift_identifier": slug,
            "gift_name": full_name,
            "gift_link": f"https://t.me/nft/{slug}",
            "market_link": f"https://t.me/nft/{slug}",
            "gift_image": None,
            "model": model_name,
            "backdrop": backdrop_name,
            "pattern": pattern_name,
            "premium_level": prem_level,
            "owner_username": clean_target,
            "owner_tg_id": target_tg_id,
            "owner_name": "Скрыт" if is_wallet_string(raw_owner_name) else raw_owner_name,
            "price": price_gram,
            "stars_price": stars_price,
            "currency": "GRAM",
            "is_upgraded": 1,
            "is_crafted": 1 if is_crafted else 0,
            "can_write_free": can_write_free,
            "gifts_count": user_gifts_count,
            "owner_gender": "female" if is_fem else "male",
            "filter_type": target_topic
        }

        lot_id = await db.add_lot(lot_dict)
        if lot_id:
            lot_dict["id"] = lot_id
            self.seen_slugs.add(slug)
            await db.mark_slugs_seen([slug])
            logger.info(f"💎 [TG RESALE NATIVE] Топик [{target_topic}]: {full_name} ({price_gram} GRAM) @{clean_target}")
            if notify_callback:
                await notify_callback(lot_dict)

    async def run_loop(self, notify_callback: Callable):
        self.is_running = True
        await self.init()
        logger.info("🚀 [TelegramResaleScanner] Нативный сканер маркета Telegram запущен!")

        cycle_idx = 0
        while self.is_running:
            if not self.catalog_gift_ids:
                await asyncio.sleep(5)
                continue

            gift_id = self.catalog_gift_ids[cycle_idx % len(self.catalog_gift_ids)]
            cycle_idx += 1

            try:
                # Чередуем: один цикл самые дешёвые листинги, второй — самые свежие новинки
                is_sort_price = (cycle_idx % 2 == 0)
                resale = await session_pool.execute_with_retry(
                    lambda c: c(GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset='',
                        limit=10,
                        sort_by_price=is_sort_price if is_sort_price else None
                    ))
                )

                if resale and getattr(resale, 'gifts', None):
                    for g in resale.gifts:
                        if not self.is_running:
                            break
                        if isinstance(g, types.StarGiftUnique):
                            await self.process_resale_item(g, notify_callback)

            except Exception as e:
                logger.debug(f"Ошибка проверки ресейла для gift_id {gift_id}: {e}")

            # Безопасная пауза 0.6 сек (при 4 сессиях это 2.4 сек на сессию)
            await asyncio.sleep(0.6)

    def stop(self):
        self.is_running = False

telegram_resale_scanner = TelegramResaleScanner()
