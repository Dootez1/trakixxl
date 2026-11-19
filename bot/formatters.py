import html
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from scanner.filters import is_wallet_string

MSK = timezone(timedelta(hours=3))

# Точные кастомные эмодзи из оригинального набора (как на фото 2)
E_HEADER_GIFT = '<tg-emoji emoji-id="5276422526350681413">🎁</tg-emoji>'
E_CART        = '<tg-emoji emoji-id="5278613311858959074">🛒</tg-emoji>'
E_USER        = '<tg-emoji emoji-id="5275979556308674886">👤</tg-emoji>'
E_PRICE       = '<tg-emoji emoji-id="5276398496008663230">👝</tg-emoji>'
E_TON_DIAMOND = '<tg-emoji emoji-id="5193179982775476271">💎</tg-emoji>'
E_STAR_YELLOW = '<tg-emoji emoji-id="5242644275014951846">⭐</tg-emoji>'
E_RIBBON      = '<tg-emoji emoji-id="5276111746812112286">⭐️</tg-emoji>'
E_LEVEL_9     = '<tg-emoji emoji-id="5242286371095211663">9️⃣</tg-emoji>'
E_LOCK_GREEN  = '<tg-emoji emoji-id="5278602437001767574">🔓</tg-emoji>'
E_CHECK_BLUE  = '<tg-emoji emoji-id="5278411813468269386">✅</tg-emoji>'

def format_lot_message(lot: Dict[str, Any], show_claim_info: bool = False, is_dm: bool = False) -> str:
    """
    Форматирует карточку NFT-подарка в точности как на фото 2:
    🎁 НОВЫЙ ЛИСТИНГ
    —————————
    🛒 Вид подарка: Instant Ramen
    👤 Владелец подарка: @username
    👝 Цена подарка: 11.52 TON 💎 | 800 STARS ⭐
    —————————
    ⭐️ Модель подарка: Sacred Grove (2.5%)
    ⭐️ Фон подарка: Tomato (1%)
    ⭐️ Узор подарка: Snowflakes (0.5%)
    —————————
    9️⃣ Уровень аккаунта владельца подарка: 1 level
    🔓 Сообщение владельцу подарка: Free
    ✅ Статус подарка: Ordinary
    """
    # Ссылка на подарок на маркете
    gift_url = lot.get("gift_link") or lot.get("market_link") or "https://t.me/nft"
    gift_name = html.escape(str(lot.get("gift_name", "NFT Gift")))

    # Владелец подарка
    username = lot.get("owner_username")
    user_id = lot.get("owner_tg_id")
    owner_name = lot.get("owner_name")
    if username and not str(username).lower().startswith("market") and not str(username).startswith("id_") and not is_wallet_string(username):
        clean_u = str(username).strip().lstrip("@")
        owner_display = f'<a href="https://t.me/{clean_u}">@{clean_u}</a>'
    elif user_id and int(user_id) > 0:
        disp_name = html.escape(str(owner_name or f"ID: {user_id}"))
        owner_display = f'<a href="tg://user?id={user_id}">{disp_name}</a>'
    elif owner_name and str(owner_name).strip() and not str(owner_name).lower().startswith("market") and not is_wallet_string(owner_name):
        owner_display = html.escape(str(owner_name).strip())
    else:
        owner_display = "Скрыт"

    # Цена
    price_val = lot.get("price", 0)
    stars_price = lot.get("stars_price")
    if not stars_price or stars_price <= 0:
        stars_price = int(price_val * 72) if price_val > 0 else 0

    if price_val > 0:
        price_str = f"{price_val:g} TON {E_TON_DIAMOND} | {stars_price} STARS {E_STAR_YELLOW}"
    else:
        price_str = f"Крафт NFT | {stars_price} STARS {E_STAR_YELLOW}"

    # Характеристики (модель, фон, узор)
    model_name = html.escape(str(lot.get("model", "Original (1%)")))
    backdrop_name = html.escape(str(lot.get("backdrop", "Persimmon (1%)")))
    pattern_name = html.escape(str(lot.get("pattern", "Thor Hammer (1%)")))

    # Уровень аккаунта владельца подарка
    prem_level = lot.get("premium_level", 0)
    level_num = prem_level if prem_level and prem_level > 0 else 1
    level_str = f"{level_num} level"

    # Сообщение владельцу подарка
    can_write = lot.get("can_write_free", 1)
    msg_status = "Free" if can_write else "Paid"

    # Статус подарка
    is_upgraded = lot.get("is_upgraded", 0)
    status_str = "Upgraded" if is_upgraded else "Ordinary"

    # Невидимый анкор на картинку для отображения карточки под текстом
    image_anchor = ""
    gift_img = lot.get("gift_image")
    if gift_img:
        image_anchor = f'<a href="{gift_img}">&#8288;</a>'

    lines = [
        f"{image_anchor}{E_HEADER_GIFT} <b>НОВЫЙ ЛИСТИНГ</b>",
        "—————————",
        f"{E_CART} <b>Вид подарка:</b> <a href=\"{gift_url}\"><b>{gift_name}</b></a>",
        f"{E_PRICE} <b>Цена подарка:</b> {price_str}",
        "—————————",
        f"{E_RIBBON} <b>Модель подарка:</b> {model_name}",
        f"{E_RIBBON} <b>Фон подарка:</b> {backdrop_name}",
        f"{E_RIBBON} <b>Узор подарка:</b> {pattern_name}",
        "—————————",
        f"{E_LEVEL_9} <b>Уровень аккаунта владельца подарка:</b> {level_str}",
        f"{E_LOCK_GREEN} <b>Сообщение владельцу подарка:</b> {msg_status}",
        f"{E_CHECK_BLUE} <b>Статус подарка:</b> {status_str}"
    ]

    # В личных сообщениях воркеру после занятия показываем владельца:
    if is_dm:
        lines.insert(3, f"{E_USER} <b>Владелец подарка:</b> {owner_display}")

    if show_claim_info and lot.get("claimed_by_username"):
        lines.append(f"\n🔒 <b>Занял: @{lot.get('claimed_by_username')}</b>")

    return "\n".join(lines)
