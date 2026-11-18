import re
import logging
from typing import Optional, Dict, Any
from aiogram import Router, F, Bot
from aiogram.types import Message

import config
from database.db import db
from bot.formatters import format_lot_message
from bot.keyboards import get_lot_keyboard
from bot.handlers.admin import send_lot_to_group
from scanner.filters import determine_filters
from scanner.profile_checker import detect_is_female

logger = logging.getLogger(__name__)
parser_router = Router()

SYSTEM_AND_BOT_BLACKLIST = {
    "telegram", "durov", "notcoin", "toncoin", "fragment", "getgems",
    "tribute", "wallet", "major", "dogs", "catizen", "hamster_kombat"
}

def parse_listing_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Разбирает реальный текст листинга подарка (из Telegram каналов/ботов):
    Извлекает имя подарка, цену в GRAM, юзернейм продавца, Telegram ID,
    уровень профиля, количество NFT и статус ЛС.
    """
    if not text:
        return None

    # Проверяем, что это сообщение о листинге или подарке
    lower = text.lower()
    if not ("листинг" in lower or "подарок" in lower or "gram" in lower or "t.me/nft" in lower):
        return None

    # 1. Название и номер подарка (например, "Chill Flame #388204" или "Plush Pepe #1602")
    gift_match = re.search(r'(?:🎁|✨|🔥)?\s*([a-zA-Zа-яА-ЯёЁ\s]+)\s*#(\d+)', text)
    if not gift_match:
        return None

    gift_title = gift_match.group(1).strip()
    gift_num = gift_match.group(2).strip()
    full_name = f"{gift_title} #{gift_num}"

    # 2. Цена в GRAM
    price_match = re.search(r'([\d.]+)\s*GRAM', text, re.IGNORECASE)
    price = float(price_match.group(1)) if price_match else 0.0

    # 3. Юзернейм продавца (@username)
    user_match = re.search(r'@([a-zA-Z0-9_]{3,})', text)
    if not user_match:
        return None
    username = user_match.group(1).strip()

    # Фильтр: исключаем ботов и сервисы
    if username.lower().endswith("bot") or username.lower() in SYSTEM_AND_BOT_BLACKLIST:
        return None

    # 4. Telegram ID продавца
    id_match = re.search(r'•\s*(\d{5,})', text)
    tg_id = int(id_match.group(1)) if id_match else None

    # 5. Уровень профиля
    lvl_match = re.search(r'Уровень:\s*(\d+)', text, re.IGNORECASE)
    level = int(lvl_match.group(1)) if lvl_match else 0

    # ФИЛЬТР ПО ТЗ: не больше 2 уровня!
    if level > 2:
        logger.debug(f"Пропущен @{username}: уровень {level} > 2")
        return None

    # 6. Количество NFT у продавца
    nfts_match = re.search(r'NFT у продавца:\s*(\d+)', text, re.IGNORECASE)
    nfts_count = int(nfts_match.group(1)) if nfts_match else 1

    # ФИЛЬТР ПО ТЗ: не 0 и не богатый (не больше 6)!
    if nfts_count <= 0 or nfts_count > 6:
        logger.debug(f"Пропущен @{username}: количество NFT {nfts_count}")
        return None

    # 7. Доступность сообщений (бесплатно / со звёздами)
    can_write_free = ("бесплатно" in lower or "без ограничений" in lower) and not ("звёзд" in lower and "платно" in lower)

    # 8. Модель, фон, узор
    model_match = re.search(r'Модель:\s*([^\n]+)', text)
    model = model_match.group(1).strip() if model_match else "Spaceship (3%)"

    bg_match = re.search(r'Фон:\s*([^•\n]+)', text)
    backdrop = bg_match.group(1).strip() if bg_match else "Persimmon"

    pattern_match = re.search(r'Узор:\s*([^\n]+)', text)
    pattern = pattern_match.group(1).strip() if pattern_match else "Thor Hammer"

    # 9. Ссылка на подарок
    slug = f"{gift_title.lower().replace(' ', '')}-{gift_num}"
    link_match = re.search(r'(https://t\.me/nft/[a-zA-Z0-9_\-]+)', text)
    official_nft_link = link_match.group(1) if link_match else f"https://t.me/nft/{slug}"
    market_link = f"https://getgems.io/collection/EQCA14o1-VWhsuGgahqA46WuO-0_ITnC_E8inWYZgAM52YMY"

    # Определение пола
    is_fem = detect_is_female("", "", username, "")

    return {
        "name": full_name,
        "price": price,
        "owner_username": username,
        "owner_tg_id": tg_id,
        "level": level,
        "gifts_count": nfts_count,
        "can_write_free": can_write_free,
        "model": model,
        "backdrop": backdrop,
        "pattern": pattern,
        "link": official_nft_link,
        "market_link": market_link,
        "is_female": is_fem,
        "is_upgraded": (price == 0.0 or "улучшен" in lower)
    }

async def process_and_send_real_listing(listing_info: Dict[str, Any], bot: Bot) -> int:
    """Применяет фильтры ТЗ, сохраняет реальный лот в БД и отправляет в группу."""
    price = listing_info["price"]
    is_upgraded = listing_info["is_upgraded"]
    is_female = listing_info["is_female"]
    is_listed = (price > 0.0)

    filters = determine_filters(
        price=price,
        is_upgraded=is_upgraded,
        is_female=is_female,
        is_listed_on_market=is_listed
    )

    if not filters:
        return 0

    sent_count = 0
    for flt in filters:
        lot_dict = {
            "gift_identifier": f"{listing_info['name']}_{flt}",
            "gift_name": listing_info["name"],
            "gift_link": listing_info["link"],
            "market_link": listing_info["market_link"],
            "gift_image": None,
            "owner_username": listing_info["owner_username"],
            "owner_tg_id": listing_info["owner_tg_id"],
            "price": price,
            "currency": "GRAM",
            "is_upgraded": 1 if is_upgraded else 0,
            "can_write_free": 1 if listing_info["can_write_free"] else 0,
            "gifts_count": listing_info["gifts_count"],
            "owner_gender": "female" if is_female else "male",
            "model": listing_info["model"],
            "backdrop": listing_info["backdrop"],
            "pattern": listing_info["pattern"],
            "premium_level": listing_info["level"],
            "filter_type": flt
        }

        lot_id = await db.add_lot(lot_dict)
        if lot_id:
            lot_dict["id"] = lot_id
            success = await send_lot_to_group(bot, lot_dict)
            if success:
                sent_count += 1

    return sent_count

@parser_router.channel_post()
@parser_router.message(F.forward_from_chat | F.text)
async def handle_incoming_listing(message: Message, bot: Bot):
    """
    Автоматически перехватывает посты из каналов листингов
    или пересланные сообщения, фильтрует и публикует для воркеров с кнопкой «Занять».
    """
    text = message.text or message.caption or ""
    parsed = parse_listing_text(text)
    if not parsed:
        return

    # Если сообщение пришло из рабочей группы самого трекера — не дублируем
    if message.chat.id == config.TELEGRAM_GROUP_ID:
        return

    sent = await process_and_send_real_listing(parsed, bot)
    if sent > 0 and message.chat.type == "private":
        await message.reply(
            f"✅ <b>Реальный лот {parsed['name']} обработан!</b>\n"
            f"Отфильтрован и отправлен в рабочую группу ({sent} топиков).",
            parse_mode="HTML"
        )
