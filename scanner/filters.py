from typing import Optional, List
import config
from scanner.gift_catalog_ids import CHEAP_GIFT_IDS, MEDIUM_GIFT_IDS, EXPENSIVE_GIFT_IDS, GIRL_GIFT_IDS

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
