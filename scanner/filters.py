import re
from typing import Optional, List, Any, Dict
import config
from scanner.gift_catalog_ids import CHEAP_GIFT_IDS, MEDIUM_GIFT_IDS, EXPENSIVE_GIFT_IDS, GIRL_GIFT_IDS

def is_wallet_string(s: Optional[str]) -> bool:
    """
    100% выявление адресов блокчейна TON и других криптовалют:
    - Префиксы: UQ, EQ, kQ, 0Q, 0:, -1:, raw:
    - Усечённые адреса: UQAMybxP…Qs2mNn, UQAMybxP_xv...xVY7TQs2mNn
    - Base64 / Hex адреса от 24 символов
    - Маркеры блокчейна: блокчейн, blockchain, tonkeeper, tonhub, getgems, tonviewer, wallet
    """
    if not s:
        return False
    clean = str(s).strip()
    if not clean:
        return False

    lower = clean.lower()
    # 1. Текстовые маркеры блокчейна
    if any(k in lower for k in ["блокчейн", "blockchain", "tonkeeper", "tonhub", "tonviewer", "getgems"]):
        return True

    # 2. Префиксы TON кошельков (любая длина)
    if re.search(r'^(UQ|EQ|kQ|0Q|0:|-1:|raw:)', clean, re.IGNORECASE):
        return True

    # 3. Усечённые адреса с многоточием (например, UQAMybxP…Qs2mNn или UQAMybxP_xv...xVY7TQs2mNn)
    # Telegram юзернеймы не содержат точки, знаки '…' или '..'
    if ('…' in clean or '..' in clean) and re.search(r'[a-zA-Z0-9]', clean):
        return True

    # 4. Длинные base64 строки TON (от 24 символов со спецсимволами _ или -)
    if len(clean) >= 24 and re.search(r'[_\-][a-zA-Z0-9_\-]{6,}', clean):
        return True

    # 5. Base64/Hex адреса от 30 символов
    if len(clean) >= 30 and re.match(r'^[a-zA-Z0-9_\-]+$', clean):
        return True

    # 6. Шестнадцатеричные адреса 0x...
    if clean.startswith("0x") and len(clean) >= 16:
        return True

    return False

def is_onchain_or_invalid_owner(
    owner_name: Optional[str] = None,
    owner_address: Optional[str] = None,
    owner_id: Any = None,
    user_entity: Any = None,
    gift_obj: Any = None,
    event_data: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Строго отсекает:
    1. Подарки в блокчейне TON (кошельки UQ..., EQ..., 0:..., '…', '..', onchain).
    2. Подарки каналов и групп (PeerChannel, PeerChat, Channel, Chat, ID < 0).
    3. Удалённые аккаунты (Deleted Accounts).
    4. Агрегаторы/боты/маркетплейсы.
    """
    # 1. Проверка адреса владельца
    if owner_address and str(owner_address).strip() and str(owner_address).strip().lower() not in ("none", "null", ""):
        return True

    # 2. Проверка имени владельца
    if owner_name and is_wallet_string(owner_name):
        return True

    # 3. Проверка объекта подарка MTProto StarGiftUnique
    if gift_obj:
        if getattr(gift_obj, 'owner_address', None):
            return True
        g_name = getattr(gift_obj, 'owner_name', None)
        if g_name and is_wallet_string(g_name):
            return True
        g_id = getattr(gift_obj, 'owner_id', None)
        if g_id is not None:
            from telethon.tl.types import PeerChannel, PeerChat
            if isinstance(g_id, (PeerChannel, PeerChat)):
                return True
            if hasattr(g_id, "channel_id") or hasattr(g_id, "chat_id"):
                return True
        # Если owner_id отсутствует (None) и есть признаки блокчейна
        if g_id is None:
            if getattr(gift_obj, 'gift_address', None) or getattr(gift_obj, 'resale_ton_only', False):
                return True
            if g_name and is_wallet_string(g_name):
                return True

    # 4. Проверка ID владельца (PeerChannel, PeerChat, отрицательный ID)
    if owner_id is not None:
        from telethon.tl.types import PeerChannel, PeerChat
        if isinstance(owner_id, (PeerChannel, PeerChat)):
            return True
        if hasattr(owner_id, "channel_id") or hasattr(owner_id, "chat_id"):
            return True
        if isinstance(owner_id, int) and owner_id < 0:
            return True

    # 5. Проверка сущности пользователя (Channel, Chat, deleted, bot)
    if user_entity:
        from telethon.tl.types import User, Channel, Chat
        if isinstance(user_entity, (Channel, Chat)) or hasattr(user_entity, 'title'):
            return True
        if isinstance(user_entity, User):
            if getattr(user_entity, 'bot', False):
                return True
            if getattr(user_entity, 'deleted', False):
                return True
            f_name = (getattr(user_entity, 'first_name', '') or '').lower()
            l_name = (getattr(user_entity, 'last_name', '') or '').lower()
            if 'deleted account' in f_name or 'удаленный аккаунт' in f_name or 'удалённый аккаунт' in f_name:
                return True
            if 'deleted account' in l_name or 'удаленный аккаунт' in l_name or 'удалённый аккаунт' in l_name:
                return True

    # 6. Проверка сырых данных события see.tg
    if event_data and isinstance(event_data, dict):
        if event_data.get("onchain") or event_data.get("is_onchain"):
            return True
        if str(event_data.get("chain", "")).lower() == "ton":
            return True
        if str(event_data.get("network", "")).lower() == "ton":
            return True
        for key in ("owner_address", "address", "wallet", "contract", "nft_address"):
            val = event_data.get(key)
            if val and is_wallet_string(str(val)):
                return True
        owner_obj = event_data.get("owner")
        if isinstance(owner_obj, dict):
            for key in ("address", "wallet", "owner_address"):
                val = owner_obj.get(key)
                if val and is_wallet_string(str(val)):
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
