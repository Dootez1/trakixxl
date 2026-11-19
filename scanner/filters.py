import re
from typing import Optional, List, Any
import config
from scanner.gift_catalog_ids import CHEAP_GIFT_IDS, MEDIUM_GIFT_IDS, EXPENSIVE_GIFT_IDS, GIRL_GIFT_IDS

def is_onchain_or_invalid_owner(
    owner_name: Optional[str] = None,
    owner_address: Optional[str] = None,
    owner_id: Any = None,
    user_entity: Any = None
) -> bool:
    """
    Строго отсекает:
    1. Подарки в блокчейне TON (кошельки UQ..., EQ..., 0:...).
    2. Подарки каналов и групп (PeerChannel, PeerChat, Channel, Chat).
    3. Удалённые аккаунты (Deleted Accounts).
    4. Агрегаторы/боты/маркетплейсы.
    """
    if owner_address and str(owner_address).strip():
        return True

    if owner_name:
        clean = str(owner_name).strip()
        if (clean.startswith(("UQ", "EQ", "0:", "-1:")) and len(clean) >= 28) or len(clean) >= 44:
            return True
        if re.search(r'[_\-][a-zA-Z0-9_\-]{20,}', clean):
            return True

    if owner_id:
        from telethon.tl.types import PeerChannel, PeerChat
        if isinstance(owner_id, (PeerChannel, PeerChat)):
            return True
        if hasattr(owner_id, "channel_id") or hasattr(owner_id, "chat_id"):
            return True

    if user_entity:
        from telethon.tl.types import User, Channel, Chat
        if isinstance(user_entity, (Channel, Chat)):
            return True
        if isinstance(user_entity, User):
            if getattr(user_entity, 'bot', False):
                return True
            if getattr(user_entity, 'deleted', False):
                return True

    return False

FILTER_CHEAP = "cheap"
FILTER_MEDIUM = "medium"
FILTER_EXPENSIVE = "expensive"
FILTER_GIRLS = "girls"
FILTER_UPGRADED = "upgraded"

FILTER_NAMES = {
    FILTER_CHEAP: "🟢 Дешёвые подарки",
    FILTER_MEDIUM: "🟡 Средние подарки",
    FILTER_EXPENSIVE: "🔴 Дорогие подарки",
    FILTER_GIRLS: "🌸 Подарки девушек",
    FILTER_UPGRADED: "✨ Улучшенные подарки"
}

def determine_filters(
    price: float,
    is_upgraded: bool,
    is_crafted: bool,
    is_female: bool,
    gift_id: Optional[int] = None,
    is_on_chain: bool = False,
    is_channel: bool = False
) -> List[str]:
    """
    Строгая классификация по 5 топикам форума:
    1. Подарки в блокчейне TON (owner_address) — исключаются.
    2. Подарки каналов и групп — исключаются.
    3. Дешёвые: строго цена от 0.1 до 5.0 GRAM
    4. Средние: строго цена от 5.01 до 20.0 GRAM
    5. Дорогие: строго цена более 20.0 GRAM (до 100 GRAM)
    6. Девушки: строго подтвержденный женский профиль (цена до 100 GRAM)
    7. Улучшенные: строго скрафченные подарки (is_crafted=True)
    """
    if is_on_chain or is_channel:
        return []

    matched = []

    # 1. Топик "Подарки девушек" (Строго реальные девушки с ценой <= 100 GRAM)
    if is_female and (price == 0 or price <= 100.0):
        matched.append(FILTER_GIRLS)

    # 2. Топик "Улучшенные подарки" (Строго скрафченные NFT за звёзды)
    if is_crafted:
        matched.append(FILTER_UPGRADED)

    # 3. Ценовые категории СТРОГО по реальной цене продажи в GRAM:
    # Дешман (0-10), Средняк (10-20), Жир (20-100)
    if price > 0:
        if price <= 10.0:
            matched.append(FILTER_CHEAP)
        elif 10.0 < price <= 20.0:
            matched.append(FILTER_MEDIUM)
        else:
            matched.append(FILTER_EXPENSIVE)
    elif gift_id:
        if gift_id in CHEAP_GIFT_IDS:
            matched.append(FILTER_CHEAP)
        elif gift_id in MEDIUM_GIFT_IDS:
            matched.append(FILTER_MEDIUM)
        elif gift_id in EXPENSIVE_GIFT_IDS:
            matched.append(FILTER_EXPENSIVE)
        else:
            matched.append(FILTER_CHEAP)

    return matched

def get_topic_id_for_filter(filter_type: str) -> int:
    """Возвращает ID топика форума для заданного фильтра из config."""
    mapping = {
        FILTER_CHEAP: config.TOPIC_CHEAP_ID,
        FILTER_MEDIUM: config.TOPIC_MEDIUM_ID,
        FILTER_EXPENSIVE: config.TOPIC_EXPENSIVE_ID,
        FILTER_GIRLS: config.TOPIC_GIRLS_ID,
        FILTER_UPGRADED: config.TOPIC_UPGRADED_ID,
    }
    return mapping.get(filter_type, 0)
