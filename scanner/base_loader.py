import asyncio
import logging
import re
from pathlib import Path
from typing import List, Dict, Any

from database.db import db
from scanner.profile_checker import profile_checker
from scanner.ton_scanner import ton_scanner
from scanner.verifier import verify_real_telegram_user

logger = logging.getLogger(__name__)

class BaseManager:
    def __init__(self):
        self.is_processing = False

    async def import_from_text(self, text: str, source: str = "manual_text") -> int:
        """Импортирует юзернеймы из сырого текста (через запятую, пробел или с новой строки)."""
        lines = text.replace(",", " ").replace(";", " ").split()
        clean_usernames = []
        for line in lines:
            u = line.strip().replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
            if u and len(u) >= 4 and u.isalnum() or "_" in u:
                clean_usernames.append(u)

        if clean_usernames:
            return await db.add_targets_to_base(clean_usernames, source=source)
        return 0

    async def import_from_file(self, file_path: Path) -> int:
        """Импортирует юзернеймы из текстового файла."""
        if not file_path.exists():
            return 0
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        return await self.import_from_text(content, source=file_path.name)

    async def run_worker(self, notify_callback):
        """
        Фоновый обработчик очереди загруженных баз:
        Берет людей со статусом 'pending', проверяет их профили на наличие подарков,
        и если подарки найдены — классифицирует и отправляет в трекер.
        """
        self.is_processing = True
        logger.info("Обработчик баз запущен.")

        while self.is_processing:
            try:
                targets = await db.get_pending_targets(limit=20)
                if not targets:
                    await asyncio.sleep(2)
                    continue

                for target in targets:
                    raw_username = target["username"]
                    target_id = target["id"]

                    clean_user = str(raw_username or "").strip().lstrip("@")
                    # Строгая валидация: ТОЛЬКО латиница, цифры и _, от 4 до 32 символов (НИКАКОГО РУССКОГО)
                    if not clean_user or re.search(r'[а-яА-ЯёЁ]', clean_user) or not re.match(r'^[a-zA-Z0-9_]{4,32}$', clean_user):
                        await db.update_target_status(target_id, "invalid_username")
                        continue

                    # Исключаем ботов
                    if clean_user.lower().endswith("bot"):
                        await db.update_target_status(target_id, "is_bot")
                        continue

                    # 100% Верификация: рабочий живой контакт
                    if not await verify_real_telegram_user(clean_user):
                        await db.update_target_status(target_id, "broken_user")
                        continue

                    # Проверяем профиль через SearchRequest без задержек
                    profile = await profile_checker.check_user(clean_user)
                    gifts_count = profile.get("gifts_count", 0)

                    if profile.get("skip_reason") or gifts_count == 0 or not profile.get("can_write_free"):
                        await db.update_target_status(target_id, profile.get("skip_reason") or ("paid_dm" if not profile.get("can_write_free") else "no_gifts"))
                        await asyncio.sleep(0.05)
                        continue

                    from telethon.tl.types import StarGiftUnique, StarGiftAttributeModel, StarGiftAttributePattern, StarGiftAttributeBackdrop
                    from scanner.filters import determine_filters
                    from scanner.ton_scanner import CHEAP_GIFT_IDS, MEDIUM_GIFT_IDS, EXPENSIVE_GIFT_IDS

                    real_gifts = profile.get("real_gifts", [])
                    found_nft = False

                    for item in real_gifts:
                        gift_obj = getattr(item, "gift", None)
                        if isinstance(gift_obj, StarGiftUnique):
                            slug = getattr(gift_obj, "slug", None)
                            if not slug:
                                continue

                            # Проверка на блокчейн и каналы
                            owner_address = getattr(gift_obj, 'owner_address', None)
                            if owner_address:
                                continue

                            from telethon.tl.types import PeerChannel, PeerChat
                            owner_id = getattr(gift_obj, 'owner_id', None)
                            if isinstance(owner_id, (PeerChannel, PeerChat)):
                                continue

                            found_nft = True
                            title = getattr(gift_obj, "title", "Telegram Gift")
                            num = getattr(gift_obj, "num", "")
                            full_name = f"{title} #{num}" if num else title

                            model = "Original"
                            pattern = "Classic"
                            backdrop = "Persimmon"

                            for attr in getattr(gift_obj, "attributes", []):
                                if isinstance(attr, StarGiftAttributeModel):
                                    rarity = f" ({attr.rarity.permille / 10}%)" if hasattr(attr, "rarity") else ""
                                    model = f"{attr.name}{rarity}"
                                elif isinstance(attr, StarGiftAttributePattern):
                                    pattern = attr.name
                                elif isinstance(attr, StarGiftAttributeBackdrop):
                                    backdrop = attr.name

                            gid = getattr(gift_obj, "gift_id", 0)
                            price_gram = 0.0
                            resell = getattr(gift_obj, "resell_amount", None)
                            if resell:
                                if isinstance(resell, (list, tuple)):
                                    for a in resell:
                                        if hasattr(a, "amount"):
                                            price_gram = round(a.amount / 1e9, 2)
                                elif hasattr(resell, "amount"):
                                    price_gram = round(resell.amount / 1e9, 2)

                            # Если подарок в профиле не выставлен на ресейл, оцениваем по флорам
                            if price_gram == 0.0:
                                if gid in CHEAP_GIFT_IDS:
                                    price_gram = 6.46
                                elif gid in MEDIUM_GIFT_IDS:
                                    price_gram = 12.50
                                elif gid in EXPENSIVE_GIFT_IDS:
                                    price_gram = 35.00
                                else:
                                    price_gram = 8.00

                            is_crafted = bool(getattr(gift_obj, "crafted", False))
                            is_female = profile.get("is_female", False)

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

                            for flt in filters:
                                lot_dict = {
                                    "gift_identifier": f"{slug}_{flt}",
                                    "gift_name": full_name,
                                    "gift_link": f"https://t.me/nft/{slug}",
                                    "market_link": f"https://t.me/nft/{slug}",
                                    "gift_image": None,
                                    "model": model,
                                    "backdrop": backdrop,
                                    "pattern": pattern,
                                    "premium_level": profile.get("premium_level", 0),
                                    "owner_username": clean_user,
                                    "owner_tg_id": profile.get("tg_id"),
                                    "price": price_gram,
                                    "currency": "GRAM",
                                    "is_upgraded": 1,
                                    "can_write_free": 1 if profile.get("can_write_free") else 0,
                                    "gifts_count": gifts_count,
                                    "owner_gender": "female" if is_female else "male",
                                    "filter_type": flt
                                }

                                lot_id = await db.add_lot(lot_dict)
                                if lot_id:
                                    lot_dict["id"] = lot_id
                                    lot_dict["restriction_reason"] = profile.get("restriction_reason", "✅ Бесплатно")
                                    logger.info(f"🎯 РЕАЛЬНЫЙ ЛОТ ИЗ БАЗЫ: @{clean_user} с {full_name} [{flt}]")
                                    if notify_callback:
                                        await notify_callback(lot_dict)
                                    await asyncio.sleep(0.05)

                    if found_nft:
                        await db.update_target_status(target_id, "found_nft")
                    else:
                        await db.update_target_status(target_id, "no_nft_gifts")

                    await asyncio.sleep(5.0)

            except Exception as e:
                logger.error(f"Ошибка при обработке базы: {e}")
                await asyncio.sleep(5)

base_manager = BaseManager()
