import asyncio
import json
import logging
import time
from typing import Dict, Any, Optional, Callable
import aiohttp
from telethon.tl.functions.payments import GetUniqueStarGiftRequest
from telethon.tl import types

import config
from database.db import db
from scanner.profile_checker import profile_checker, detect_is_female
from scanner.deanonymizer import deanonymize_lot
from scanner.verifier import verify_real_telegram_user
from scanner.filters import (
    determine_filters, FILTER_CHEAP, FILTER_MEDIUM,
    FILTER_EXPENSIVE, FILTER_GIRLS, FILTER_UPGRADED
)

logger = logging.getLogger("SeeScanner")

LIVE_WS_URL = "wss://live.see.tg/v1/ws"

class SeeScanner:
    """
    Сканер живого потока see.tg / @GiftChanges (WebSocket).
    Перехватывает листинги, крафты и передачи в реальном времени.
    Обеспечивает 100% работу 24/7 с фильтрацией реальных людей.
    """
    def __init__(self):
        self.is_running = False
        self.seen_slugs = set()
        self.recent_sellers = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(3)

    async def init(self):
        self.seen_slugs = await db.get_all_seen_slugs()
        logger.info(f"⚡️ [SeeScanner] Загружено {len(self.seen_slugs)} виденных лотов для дедупликации.")

    def _extract_attr_name(self, attr_val: Any, default: str) -> str:
        if isinstance(attr_val, dict):
            name = attr_val.get("name") or default
            permille = attr_val.get("rarity_permille")
            if permille is not None:
                pct = permille / 10.0
                return f"{name} ({pct:g}%)"
            percent = attr_val.get("percent") or attr_val.get("rarity")
            if percent is not None:
                return f"{name} ({percent}%)"
            return name
        if isinstance(attr_val, str) and attr_val:
            return attr_val
        return default

    async def process_event(self, event_data: Dict[str, Any], notify_callback: Callable):
        try:
            ev_type = event_data.get("event")
            if ev_type not in ("listing", "price", "sale", "new", "transfer", "saved_new"):
                return

            gift = event_data.get("gift") or {}
            slug = gift.get("slug")
            num = gift.get("num")
            if not slug or num is None:
                return

            full_slug = f"{slug}-{num}"

            # 1. 100% Защита от дублей
            if full_slug in self.seen_slugs or await db.is_slug_seen(full_slug):
                return
            self.seen_slugs.add(full_slug)

            # 2. Расчет цены в TON (GRAM)
            price_info = event_data.get("price") or {}
            amount = price_info.get("amount", 0)
            curr = str(price_info.get("currency", "")).lower()

            price_gram = 0.0
            if curr in ("gram", "ton"):
                if amount > 1_000_000:
                    price_gram = round(amount / 1e9, 2)
                else:
                    price_gram = round(float(amount), 2)
            elif curr in ("xtr", "stars"):
                price_gram = round(amount / 72.0, 2)
            elif "price_ton" in event_data:
                price_gram = round(float(event_data["price_ton"]), 2)

            if price_gram <= 0:
                raw_ton = gift.get("resellAmountTon")
                if raw_ton and str(raw_ton).isdigit():
                    price_gram = round(int(raw_ton) / 1e9, 2)

            # Если цена не указана (например, при transfer / new), берем примерную оценку или минимальную
            if price_gram <= 0:
                estimate = gift.get("estimate") or {}
                if isinstance(estimate, dict) and estimate.get("ton"):
                    price_gram = round(float(estimate["ton"]), 2)
                else:
                    price_gram = 5.0  # Дефолтная базовая цена

            # Проверяем допустимый диапазон цены (0.1 - 100 GRAM)
            if price_gram < 0.1 or price_gram > 100.0:
                return

            # 3. Атрибуты подарка
            market = event_data.get("market") or "telegram"
            title = gift.get("title") or slug
            full_name = f"{title} #{num}"

            model_name = self._extract_attr_name(gift.get("model"), "Original")
            backdrop_name = self._extract_attr_name(gift.get("backdrop"), "Persimmon")
            pattern_name = self._extract_attr_name(gift.get("pattern"), "Classic")
            is_crafted = bool(gift.get("crafted", False)) or (ev_type == "new")

            # 4. Извлечение реального владельца / продавца
            target_username = None
            target_tg_id = None
            raw_owner_name = gift.get("owner_name") or ""
            user_gifts_count = 1
            is_fem = False
            can_write_free = 1
            prem_level = 0

            # Ограничиваем параллельные запросы к MTProto
            async with self._semaphore:
                # Пытаемся получить и де-анонимизировать через MTProto Telegram
                mtproto_gift = None
                try:
                    from scanner.session_pool import session_pool
                    res = await session_pool.execute_with_retry(
                        lambda c: c(GetUniqueStarGiftRequest(slug=full_slug))
                    )
                    if res and getattr(res, 'gift', None):
                        mtproto_gift = res.gift
                        if getattr(mtproto_gift, 'owner_name', None):
                            raw_owner_name = mtproto_gift.owner_name
                        for attr in getattr(mtproto_gift, 'attributes', []):
                            if isinstance(attr, types.StarGiftAttributeModel):
                                if getattr(attr, 'crafted', False):
                                    is_crafted = True
                                model_name = attr.name
                                if getattr(attr, 'rarity_permille', None) is not None:
                                    model_name = f"{attr.name} ({attr.rarity_permille / 10.0:g}%)"
                            elif isinstance(attr, types.StarGiftAttributePattern):
                                pattern_name = attr.name
                                if getattr(attr, 'rarity_permille', None) is not None:
                                    pattern_name = f"{attr.name} ({attr.rarity_permille / 10.0:g}%)"
                            elif isinstance(attr, types.StarGiftAttributeBackdrop):
                                backdrop_name = attr.name
                                if getattr(attr, 'rarity_permille', None) is not None:
                                    backdrop_name = f"{attr.name} ({attr.rarity_permille / 10.0:g}%)"
                except Exception as me:
                    logger.debug(f"MTProto GetUniqueStarGiftRequest({full_slug}): {me}")

                # Запускаем де-анонимизацию через обновленный каскад с защитой от китов
                if mtproto_gift:
                    try:
                        deanonym_res = await deanonymize_lot(None, mtproto_gift)
                        if deanonym_res is None:
                            await db.mark_slugs_seen([full_slug])
                            return

                        cand_id, cand_user, method, cnt = deanonym_res
                        if cnt > 5:
                            await db.mark_slugs_seen([full_slug])
                            return

                        target_tg_id = cand_id
                        target_username = cand_user
                        user_gifts_count = cnt
                    except Exception as de:
                        logger.debug(f"Ошибка де-анонимизации: {de}")

            # Если событие трансфера/получения (авторизованный поток see.tg)
            if not target_username:
                to_user = event_data.get("to_user") or event_data.get("user") or {}
                if isinstance(to_user, dict) and to_user.get("username"):
                    target_username = to_user.get("username")
                    target_tg_id = to_user.get("id")

            # ВТОРОЙ ЭШЕЛОН ПРОВЕРКИ КИТОВ: ProfileChecker
            if target_username:
                p_data = await profile_checker.check_user(target_username)
                g_cnt = p_data.get("gifts_count", user_gifts_count)
                if p_data.get("is_rich") or g_cnt > 5:
                    logger.info(f"🚫 [КИТ ОТСЕЯН]: @{target_username} имеет {g_cnt} подарков (> 5) / {p_data.get('skip_reason')}!")
                    await db.mark_slugs_seen([full_slug])
                    return
                prem_level = p_data.get("premium_level", 0)
                can_write_free = 1 if p_data.get("can_write_free", True) else 0
                user_gifts_count = g_cnt
                is_fem = p_data.get("is_female", False)

            # СТРОГИЙ ФИЛЬТР: В топики отправляются ТОЛЬКО лоты с доступным @username!
            # Без юзернейма воркеры физически не могут написать в ЛС.
            if not target_username:
                return

            BLACKLISTED_HANDLES = {
                "alexander", "terminator", "telegram", "market", "fragment",
                "wallet", "username", "admin", "channel", "news", "support",
                "gifts", "nft", "ton", "crypto", "official"
            }
            if target_username.lower() in BLACKLISTED_HANDLES:
                return

            logger.info(f"🔥 [ПОДТВЕРЖДЕН ПРОДАВЕЦ]: {full_name} -> @{target_username} (подарков: {user_gifts_count})")

            # Защита от спама одним и тем же пользователем (кулдаун 10 минут)
            seller_key = target_username.lower()
            if time.time() - self.recent_sellers.get(seller_key, 0) < 600:
                logger.debug(f"Продавец @{target_username} уже публиковался за последние 10 минут. Пропускаем.")
                return
            self.recent_sellers[seller_key] = time.time()

            # 5. Определение топика
            filters = determine_filters(
                price=price_gram,
                is_upgraded=True,
                is_crafted=is_crafted,
                is_female=is_fem,
                gift_id=None,
                is_on_chain=(market != "telegram"),
                is_channel=False
            )

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

            if not target_topic or target_topic not in filters:
                return

            # 6. Формирование лота
            stars_price = 0
            if curr in ("xtr", "stars") and amount > 0:
                stars_price = int(amount)
            elif gift.get("resellAmountStars"):
                stars_price = int(gift.get("resellAmountStars"))
            else:
                stars_price = int(round(price_gram * 70))

            lot_dict = {
                "gift_identifier": full_slug,
                "gift_name": full_name,
                "gift_link": f"https://t.me/nft/{full_slug}",
                "market_link": f"https://t.me/nft/{full_slug}",
                "gift_image": None,
                "model": model_name,
                "backdrop": backdrop_name,
                "pattern": pattern_name,
                "premium_level": prem_level,
                "owner_username": target_username,
                "owner_tg_id": target_tg_id,
                "owner_name": raw_owner_name,
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
                self.seen_slugs.add(full_slug)
                await db.mark_slugs_seen([full_slug])

                logger.info(f"🔥 [ЖИВОЙ ЛОТ] В топик [{target_topic}]: {full_name} ({price_gram} GRAM) @{target_username or target_tg_id}")
                if notify_callback:
                    await notify_callback(lot_dict)

        except Exception as e:
            logger.error(f"Ошибка обработки события see.tg: {e}", exc_info=True)

    async def run_loop(self, notify_callback: Callable):
        self.is_running = True
        await self.init()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://see.tg"
        }

        token = getattr(config, "SEETG_TOKEN", "").strip()
        ws_url = f"{LIVE_WS_URL}?token={token}" if token else LIVE_WS_URL
        if token:
            logger.info("🔑 [SeeScanner] Подключение с авторизованным токеном see.tg API!")

        reconnect_delay = 2
        while self.is_running:
            try:
                logger.info(f"🌐 [SeeScanner] Подключение к live WebSocket: {ws_url.split('?')[0]}...")
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    self.session = session
                    async with session.ws_connect(ws_url, headers=headers, heartbeat=25.0) as ws:
                        logger.info("✅ [SeeScanner] WebSocket подключен успешно к live.see.tg!")
                        reconnect_delay = 2

                        async for msg in ws:
                            if not self.is_running:
                                break

                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    raw_text = msg.data.replace('"gift_id":', '"gift_id_str":')
                                    data = json.loads(raw_text)
                                    msg_type = data.get("type")

                                    if msg_type == "hello":
                                        events_list = ["new", "transfer", "saved_new", "listing", "price", "sale"] if token else ["listing", "price", "sale"]
                                        sub_payload = {
                                            "type": "subscribe",
                                            "events": events_list
                                        }
                                        await ws.send_str(json.dumps(sub_payload))
                                        logger.info(f"📡 [SeeScanner] Отправлена подписка на события: {events_list}")
                                    elif msg_type == "event":
                                        asyncio.create_task(self.process_event(data, notify_callback))

                                except Exception as parse_err:
                                    logger.debug(f"Ошибка парсинга WS сообщения: {parse_err}")

                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                logger.warning(f"⚠️ [SeeScanner] Соединение закрыто: {msg}")
                                break

            except asyncio.CancelledError:
                break
            except Exception as conn_err:
                logger.warning(f"⚠️ [SeeScanner] Ошибка WS: {conn_err}. Переподключение через {reconnect_delay}с...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

        logger.info("[SeeScanner] Работа сканера завершена.")

    def stop(self):
        self.is_running = False

see_scanner = SeeScanner()
