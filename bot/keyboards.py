import re
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from scanner.filters import is_wallet_string

def get_lot_keyboard(lot_id: int, username: str = None, user_id: int = None, gift_link: str = None, owner_name: str = None) -> InlineKeyboardMarkup:
    """
    Инлайн-кнопка нового лота в группе:
    Только [ Занять лот ]
    (никаких кнопок связи и маркета в группе)
    """
    buttons = [
        [InlineKeyboardButton(text="Занять лот", callback_data=f"claim_{lot_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_claimed_group_keyboard(lot_id: int, username: str = None, user_id: int = None, gift_link: str = None) -> InlineKeyboardMarkup:
    """
    Инлайн-кнопка в группе после того, как лот заняли:
    Кнопка 'Занято' с красным фоном (style='danger')
    """
    buttons = [
        [InlineKeyboardButton(text="Занято", callback_data=f"claimed_info_{lot_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_claimed_lot_dm_keyboard(username: str, lot_id: int = None, user_id: int = None, gift_link: str = None) -> InlineKeyboardMarkup:
    """
    Кнопки в личке воркера, который занял этот лот:
    - Написать продавцу (прямой контакт мамонта)
    - Отпустить лот
    """
    clean_username = (username or "").strip().lstrip("@")
    buttons = []
    if clean_username and not clean_username.lower().startswith("market") and not clean_username.startswith("id_") and not is_wallet_string(clean_username):
        buttons.append([
            InlineKeyboardButton(text="Написать продавцу", url=f"https://t.me/{clean_username}")
        ])

    if lot_id:
        buttons.append([
            InlineKeyboardButton(text="Отпустить лот", callback_data=f"release_{lot_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
