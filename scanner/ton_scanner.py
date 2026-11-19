import asyncio
import logging
import random
import re
import time
from typing import Dict, Any, List, Optional
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl import functions, types
from telethon.tl.types import (
    User, StarGiftUnique, PeerUser, PeerChannel, PeerChat,
    StarsTonAmount, StarsAmount
)
from aiogram.exceptions import TelegramBadRequest

import config
from database.db import db
from scanner.session_pool import session_pool
from scanner.filters import (
    determine_filters, FILTER_CHEAP, FILTER_MEDIUM,
    FILTER_EXPENSIVE, FILTER_GIRLS, FILTER_UPGRADED,
    is_onchain_or_invalid_owner
)
from scanner.profile_checker import profile_checker, detect_is_female
from scanner.verifier import verify_real_telegram_user
from scanner.deanonymizer import deanonymize_lot, verify_candidate_showcase

logger = logging.getLogger(__name__)

TARGET_GIFT_CHATS = [
    "funstat_chat",     # funstat / telelog чат
    "tggifts_chat",     # Telegram Gifts Chat | Покупка и продажа НФТ
    "nft_gifts_chat",   # Nft gifts chat 💬
    "nft_gifts_market", # NFT Gifts Market
    "gifts_otc",        # Gifts OTC
]

from scanner.gift_catalog_ids import CHEAP_GIFT_IDS, MEDIUM_GIFT_IDS, EXPENSIVE_GIFT_IDS, GIRL_GIFT_IDS

GIRL_KEYWORDS = [
    "bouquet", "love", "heart", "flower", "lollipop", "sweet", "ribbon",
    "pink", "balloon", "teddy", "cat", "bunny", "cookie", "rose", "perfume", "sakura",
    "posy", "muffin", "stilettos", "ring"
]

class TonScanner:
    def __init__(self, bot_instance=None):
        self.bot = bot_instance
        self.client = None
        self.is_running = False
        self.seen_slugs = set()
        self.seen_users = set()
        self.initialized_gift_ids = set()
        self.cached_entities = {}
        self.offset_dict = {}
        self.recent_sellers = {}

    async def init_client(self):
        self.client = profile_checker.client
        if not self.client or not await self.client.is_user_authorized():
            logger.error("Сессия Telethon не авторизована!")
            return False

        # 1. Загружаем все ранее опубликованные лоты из SQLite (100% защита от повторок)
        self.seen_slugs = await db.get_all_seen_slugs()
        logger.info(f"🛡 Загружено {len(self.seen_slugs)} ранее виденных лотов (защита от повторок).")

        for ch in TARGET_GIFT_CHATS:
            try:
                ent = await self.client.get_entity(ch)
                self.cached_entities[ch] = ent
                logger.info(f"Подключен источник пользователей: {getattr(ent, 'title', ch)}")
            except Exception as e:
                logger.warning(f"Не удалось подключить источник {ch}: {e}")

        logger.info("✅ Сканнер Telegram и официального Resale готов к работе!")
        return True

    async def check_and_publish_user(self, sender: User, notify_callback):
        """Проверяет профиль пользователя из чата."""
        if not isinstance(sender, User) or not sender.username or sender.bot:
            return

        clean_user = str(sender.username).strip().lstrip("@")
        # Строгая проверка: юзернейм ТОЛЬКО на латинице, никаких русских букв
        if not clean_user or re.search(r'[а-яА-ЯёЁ]', clean_user) or not re.match(r'^[a-zA-Z0-9_]{4,32}$', clean_user):
            return

        # Исключаем ботов по имени
        if clean_user.lower().endswith("bot"):
            return

        # 100% Верификация: рабочий живой профиль, по которому переходит
        if not await verify_real_telegram_user(clean_user):
            return

        clean_lower = clean_user.lower()
        if time.time() - self.recent_sellers.get(clean_lower, 0) < 1800:
            return

        user_key = sender.id
        if user_key in self.seen_users:
            return
        self.seen_users.add(user_key)

        username = clean_user

        try:
            res = await self.client(functions.payments.GetSavedStarGiftsRequest(
                peer=sender,
                offset='',
                limit=15,
                exclude_unsaved=False
            ))

            if not res or res.count == 0 or res.count > 5:
                return

            profile_data = await profile_checker.check_user(sender)
            if profile_data.get("is_rich") or profile_data.get("skip_reason") or not profile_data.get("can_write_free"):
                return

            for item in res.gifts:
                gift_obj = getattr(item, 'gift', None)
                if not isinstance(gift_obj, StarGiftUnique):
                    continue

                slug = getattr(gift_obj, 'slug', None)
                if not slug or slug in self.seen_slugs or await db.is_slug_seen(slug):
                    continue

                if is_onchain_or_invalid_owner(
                    owner_name=getattr(gift_obj, 'owner_name', None),
                    owner_address=getattr(gift_obj, 'owner_address', None),
                    owner_id=getattr(gift_obj, 'owner_id', None)
                ):
                    continue

                if getattr(gift_obj, 'owner_address', None):
                    continue
                if isinstance(getattr(gift_obj, 'owner_id', None), (PeerChannel, PeerChat)):
                    continue

                self.seen_slugs.add(slug)

                title = getattr(gift_obj, 'title', 'Telegram Gift')
                num = getattr(gift_obj, 'num', '')
                full_name = f"{title} #{num}" if num else title

                model = "Original"
                pattern = "Classic"
                backdrop = "Persimmon"

                for attr in getattr(gift_obj, 'attributes', []):
                    if isinstance(attr, types.StarGiftAttributeModel):
                        rarity = f" ({attr.rarity.permille / 10}%)" if hasattr(attr, 'rarity') else ""
                        model = f"{attr.name}{rarity}"
                    elif isinstance(attr, types.StarGiftAttributePattern):
                        pattern = attr.name
                    elif isinstance(attr, types.StarGiftAttributeBackdrop):
                        backdrop = attr.name

                # Непрерывное пополнение Gift Registry DB и Seller Cache (Обучение де-анонимизатора)
                try:
                    num_val = int(num) if str(num).isdigit() else None
                    await db.record_gift_registry(
                        slug=slug,
                        num=num_val,
                        owner_id=sender.id,
                        owner_username=sender.username,
                        owner_name=getattr(sender, 'first_name', None),
                        model=model,
                        backdrop=backdrop,
                        pattern=pattern
                    )
                    if getattr(sender, 'first_name', None):
                        await db.record_seller_cache(sender.first_name, sender.id, sender.username)
                    if getattr(sender, 'last_name', None):
                        await db.record_seller_cache(f"{sender.first_name} {sender.last_name}", sender.id, sender.username)
                except Exception as e_reg:
                    logger.debug(f"Ошибка записи в реестр подарков: {e_reg}")

                price_gram = 0.0
                resell = getattr(gift_obj, "resell_amount", None)
                if resell:
                    if isinstance(resell, (list, tuple)):
                        for a in resell:
                            if hasattr(a, "amount"):
                                price_gram = round(a.amount / 1e9, 2)
                    elif hasattr(resell, "amount"):
                        price_gram = round(resell.amount / 1e9, 2)

                gid = getattr(gift_obj, "gift_id", 0)
                if price_gram == 0.0:
                    if gid in CHEAP_GIFT_IDS:
                        price_gram = 6.46
                    elif gid in MEDIUM_GIFT_IDS:
                        price_gram = 14.00
                    elif gid in EXPENSIVE_GIFT_IDS:
                        price_gram = 35.00
                    else:
                        price_gram = 8.00

                is_crafted = bool(getattr(gift_obj, "crafted", False))
                is_female = profile_data.get("is_female", False)

                filters = determine_filters(
                    price=price_gram,
                    is_upgraded=True,
                    is_crafted=is_crafted,
                    is_female=is_female,
                    gift_id=gid,
                    is_on_chain=False,
                    is_channel=False
                )

                if not filters:
                    continue

                # Выбираем строго один наиболее приоритетный топик (Исключаем дубли по разным топикам!)
                flt = filters[0]
                lot_dict = {
                    "gift_identifier": slug,
                    "gift_name": full_name,
                    "gift_link": f"https://t.me/nft/{slug}",
                    "market_link": f"https://t.me/nft/{slug}",
                    "gift_image": None,
                    "model": model,
                    "backdrop": backdrop,
                    "pattern": pattern,
                    "premium_level": profile_data.get("premium_level", 0),
                    "owner_username": username,
                    "owner_tg_id": sender.id,
                    "price": price_gram,
                    "currency": "GRAM",
                    "is_upgraded": 1,
                    "can_write_free": 1 if profile_data.get("can_write_free") else 0,
                    "gifts_count": res.count,
                    "owner_gender": "female" if is_female else "male",
                    "filter_type": flt
                }

                lot_id = await db.add_lot(lot_dict)
                if lot_id:
                    lot_dict["id"] = lot_id
                    self.seen_slugs.add(slug)
                    await db.mark_slugs_seen([slug])
                    self.recent_sellers[clean_lower] = time.time()
                    logger.info(f"🎯 [СВЕЖИЙ ЛОТ ИЗ ПРОФИЛЯ]: @{username} с {full_name} [{flt}]")
                    if notify_callback:
                        await notify_callback(lot_dict)
                    await asyncio.sleep(0.05)

        except Exception as e:
            logger.debug(f"Ошибка проверки пользователя @{username}: {e}")

    async def resale_stream_worker(self, notify_callback):
        """
        Сбалансированный поток официального ресейла Telegram:
        Строго равномерно (Round-Robin) отправляет лоты во ВСЕ 5 ТОПИКОВ:
        1. Дешёвые (1-5 GRAM)
        2. Улучшенные (1-100 GRAM / крафт)
        3. Девушки (1-100 GRAM)
        4. Средние (5-20 GRAM)
        5. Дорогие (20-100 GRAM)
        """
        logger.info("🚀 Запущен сбалансированный поток официального ресейла по всем 5 темам!")

        CATEGORY_CONFIGS = [
            (FILTER_CHEAP, CHEAP_GIFT_IDS, True),                                  # Дешёвые (флор рынка ~6-7 GRAM)
            (FILTER_UPGRADED, MEDIUM_GIFT_IDS + CHEAP_GIFT_IDS + EXPENSIVE_GIFT_IDS, False), # Все улучшенные NFT (1-100 GRAM)
            (FILTER_GIRLS, GIRL_GIFT_IDS + CHEAP_GIFT_IDS + MEDIUM_GIFT_IDS, False),# Подарки девушек (1-100 GRAM)
            (FILTER_MEDIUM, MEDIUM_GIFT_IDS, False),                               # Средние (5-20 GRAM)
            (FILTER_EXPENSIVE, EXPENSIVE_GIFT_IDS, False)                          # Дорогие (20-100 GRAM)
        ]

        NON_USERS = {
            "holder", "anonymous", "telegram", "owner", "admin", "channel",
            "group", "gift", "market", "resale", "unrealministr", "sftau",
            "forrestwpotoke", "sklinfild", "kknbb", "muhammaddofficial", "fueqx"
        }

        cat_idx = 0

        while self.is_running:
            target_topic, pool_ids, sort_price = CATEGORY_CONFIGS[cat_idx % len(CATEGORY_CONFIGS)]
            cat_idx += 1

            shuffled_ids = list(pool_ids)
            random.shuffle(shuffled_ids)

            published_for_this_topic = False

            for gift_id in shuffled_ids[:30]:
                if not self.is_running:
                    break

                try:
                    # Запрашиваем верхушку листинга (offset='', sort_by_price=False -> СВЕЖИЕ ПОСТУПЛЕНИЯ)
                    resale = await self.client(functions.payments.GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset='',
                        limit=20,
                        sort_by_price=False
                    ))

                    if not resale or not getattr(resale, 'gifts', None):
                        await asyncio.sleep(0.3)
                        continue

                    sorted_gifts = sorted(
                        resale.gifts,
                        key=lambda g: 0 if (getattr(g, 'owner_name', '') or '').startswith('@') else 1
                    )

                    for rg in sorted_gifts:
                        if not self.is_running:
                            break
                        if not isinstance(rg, StarGiftUnique):
                            continue

                        slug = getattr(rg, 'slug', None)
                        if not slug or slug in self.seen_slugs or await db.is_slug_seen(slug):
                            continue

                        # Фильтр 1: Исключаем блокчейн TON
                        if getattr(rg, 'owner_address', None):
                            continue

                        # Фильтр 2: Исключаем каналы и группы
                        if isinstance(getattr(rg, 'owner_id', None), (PeerChannel, PeerChat)):
                            continue

                        # Определение цены в GRAM
                        price_gram = 0.0
                        for a in getattr(rg, 'resell_amount', []):
                            if isinstance(a, StarsTonAmount) or 'ton' in type(a).__name__.lower():
                                price_gram = round(a.amount / 1e9, 2)
                                break

                        if price_gram == 0.0:
                            for a in getattr(rg, 'resell_amount', []):
                                if hasattr(a, 'amount'):
                                    price_gram = round(a.amount / 72.0, 2)
                                    break

                        raw_owner = getattr(rg, 'owner_name', '') or ''
                        title = getattr(rg, 'title', 'Telegram Gift')

                        target_username = None
                        target_tg_id = 0
                        clean_owner = None
                        user_gifts_count = 1

                        num = getattr(rg, 'num', '')
                        num_val = int(num) if str(num).isdigit() else None

                        if raw_owner.startswith("@"):
                            cand = raw_owner.lstrip("@").strip()
                            cand_lower = cand.lower()
                            if (not re.match(r'^[a-zA-Z0-9_]{4,32}$', cand)
                                or re.search(r'[а-яА-ЯёЁ]', cand)
                                or cand_lower.endswith("bot")
                                or cand_lower in NON_USERS):
                                continue

                            # 1. 100% Верификация через веб-профиль t.me
                            if not await verify_real_telegram_user(cand):
                                continue

                            # 2. СТРОГАЯ ПРОВЕРКА ВИТРИНЫ:
                            # Убеждаемся, что этот @username РЕАЛЬНО владеет этим подарком И у него <= 3 подарков!
                            is_valid, cnt, _ = await verify_candidate_showcase(self.client, cand, slug, num_val)
                            if not is_valid:
                                logger.debug(f"Лот {slug} пропущен: у @{cand} нет этого подарка в витрине или подарков > 3")
                                continue

                            target_username = cand
                            clean_owner = cand
                            user_gifts_count = cnt
                        else:
                            # АНОНИМНЫЙ ЛОТ ИЛИ ИМЯ БЕЗ '@' -> МНОГОУРОВНЕВЫЙ ДЕ-АНОНИМИЗАТОР
                            deanonym_result = await deanonymize_lot(
                                self.client,
                                rg,
                                users_map=getattr(resale, 'users', [])
                            )
                            if deanonym_result:
                                target_tg_id, target_username, method, cnt = deanonym_result
                                clean_owner = target_username or f"id_{target_tg_id}"
                                user_gifts_count = cnt
                                logger.info(f"🔓 [ЛОТ ДЕ-АНОНИМИЗИРОВАН]: {title} #{num} -> {clean_owner} ({method}, подарков: {cnt})")
                            else:
                                # Анонимный лот не подтвержден на 100% — пропускаем
                                continue

                        seller_key = (target_username or str(target_tg_id)).lower()

                        # Строгая защита от повтора одного и того же продавца:
                        now_ts = time.time()
                        if now_ts - self.recent_sellers.get(seller_key, 0) < 300:
                            continue

                        # СТРОГАЯ ПРОВЕРКА ПРОФИЛЯ ПРОДАВЦА:
                        # 1. Бесплатные ЛС (без звёзд и платного премиума)
                        seller_target = target_username or target_tg_id
                        profile_data = await profile_checker.check_user(seller_target)
                        if profile_data.get("is_rich"):
                            continue
                        if profile_data.get("skip_reason") and any(w in profile_data["skip_reason"].lower() for w in ["канал", "бот", "богатый", "> 3"]):
                            continue
                        if not profile_data.get("can_write_free", True):
                            continue

                        # Определение пола: ТОЛЬКО реальные девушки по имени/профилю (никаких @alex)
                        is_fem = detect_is_female(clean_owner, None, clean_owner, None)

                        is_crafted = bool(getattr(rg, 'crafted', False))

                        filters = determine_filters(
                            price=price_gram,
                            is_upgraded=True,
                            is_crafted=is_crafted,
                            is_female=is_fem,
                            gift_id=gift_id,
                            is_on_chain=False,
                            is_channel=False
                        )

                        if target_topic not in filters:
                            continue

                        self.seen_slugs.add(slug)

                        num = getattr(rg, 'num', '')
                        full_name = f"{title} #{num}" if num else title

                        model = "Original"
                        pattern = "Classic"
                        backdrop = "Persimmon"

                        for attr in getattr(rg, 'attributes', []):
                            if isinstance(attr, types.StarGiftAttributeModel):
                                rarity = f" ({attr.rarity.permille / 10}%)" if hasattr(attr, 'rarity') else ""
                                model = f"{attr.name}{rarity}"
                            elif isinstance(attr, types.StarGiftAttributePattern):
                                pattern = attr.name
                            elif isinstance(attr, types.StarGiftAttributeBackdrop):
                                backdrop = attr.name

                        lot_dict = {
                            "gift_identifier": slug,
                            "gift_name": full_name,
                            "gift_link": f"https://t.me/nft/{slug}",
                            "market_link": f"https://t.me/nft/{slug}",
                            "gift_image": None,
                            "model": model,
                            "backdrop": backdrop,
                            "pattern": pattern,
                            "premium_level": profile_data.get("premium_level", 1),
                            "owner_username": target_username,
                            "owner_tg_id": target_tg_id,
                            "price": price_gram,
                            "currency": "GRAM",
                            "is_upgraded": 1,
                            "can_write_free": 1,
                            "gifts_count": user_gifts_count,
                            "owner_gender": "female" if is_fem else "male",
                            "filter_type": target_topic
                        }

                        # Сохраняем в реестр де-анонимизированные данные
                        try:
                            num_val = int(num) if str(num).isdigit() else None
                            if target_tg_id > 0 or target_username:
                                await db.record_gift_registry(
                                    slug=slug,
                                    num=num_val,
                                    owner_id=target_tg_id,
                                    owner_username=target_username,
                                    owner_name=clean_owner,
                                    model=model,
                                    backdrop=backdrop,
                                    pattern=pattern
                                )
                        except Exception:
                            pass

                        lot_id = await db.add_lot(lot_dict)
                        if lot_id:
                            lot_dict["id"] = lot_id
                            self.seen_slugs.add(slug)
                            await db.mark_slugs_seen([slug])
                            self.recent_sellers[seller_key] = time.time()
                            logger.info(f"⚡️ [СВЕЖИЙ ЛИСТИНГ] В ТОПИК [{target_topic}]: {full_name} ({price_gram} GRAM) @{clean_owner}")
                            if notify_callback:
                                await notify_callback(lot_dict)

                            published_for_this_topic = True
                            await asyncio.sleep(0.05)
                            break

                    if published_for_this_topic:
                        break

                except FloodWaitError as fe:
                    logger.warning(f"⏳ FloodWait {fe.seconds}s на сессии! Переключение на следующую сессию пула...")
                    session_pool.report_floodwait(self.client, fe.seconds)
                    new_client = session_pool.get_client()
                    if new_client:
                        self.client = new_client
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.debug(f"Ошибка ресейла для {gift_id}: {e}")

                await asyncio.sleep(0.4)

            # Если тема не нашла лот с первой попытки, делаем короткую паузу
            if not published_for_this_topic:
                await asyncio.sleep(0.5)

    async def run_loop(self, notify_callback):
        self.is_running = True
        logger.info("Сканнер запущен на полную мощность.")

        connected = await self.init_client()
        if not connected:
            return

        asyncio.create_task(self.resale_stream_worker(notify_callback))

        valid_chats = list(self.cached_entities.values())
        if valid_chats:
            @self.client.on(events.NewMessage(chats=valid_chats))
            async def on_new_chat_message(event):
                try:
                    sender = await event.get_sender()
                    if sender and isinstance(sender, User) and not sender.bot:
                        if sender.first_name:
                            await db.record_seller_cache(sender.first_name, sender.id, sender.username)
                        if sender.last_name:
                            await db.record_seller_cache(f"{sender.first_name} {sender.last_name}", sender.id, sender.username)
                        await self.check_and_publish_user(sender, notify_callback)
                except Exception as e:
                    logger.debug(f"Ошибка события: {e}")

        while self.is_running:
            for ch_key, ent in list(self.cached_entities.items()):
                if not self.is_running:
                    break
                try:
                    async for msg in self.client.iter_messages(ent, limit=40):
                        if not self.is_running:
                            break
                        sender = await msg.get_sender()
                        if sender and isinstance(sender, User):
                            await self.check_and_publish_user(sender, notify_callback)
                        await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Ошибка обхода чата {ch_key}: {e}")

                await asyncio.sleep(1)

            await asyncio.sleep(2)

    def stop(self):
        self.is_running = False

ton_scanner = TonScanner()
