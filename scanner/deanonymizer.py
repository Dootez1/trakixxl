import logging
import re
from typing import Optional, Tuple, List, Dict, Any
from telethon import TelegramClient
from telethon.tl.types import (
    StarGiftUnique, PeerUser, User,
    StarGiftAttributeOriginalDetails
)
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import GetSavedStarGiftsRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest

from database.db import db
from scanner.verifier import verify_real_telegram_user
from scanner.session_pool import session_pool
from scanner.filters import is_onchain_or_invalid_owner, is_wallet_string

logger = logging.getLogger("Deanonymizer")

BLACKLISTED_HANDLES = {
    "alexander", "terminator", "telegram", "market", "fragment",
    "wallet", "username", "admin", "channel", "news", "support",
    "gifts", "nft", "ton", "crypto", "official", "bot", "portals", "portal",
    "unlimited", "gifthunter", "giftplayers", "gift_bot", "stars", "getgems",
    "tonnel", "mrkt", "tonviewer"
}

COMMON_FIRST_NAMES = {
    "alexander", "terminator", "michael", "timur", "artem", "dmitry", "ivan",
    "maxim", "anna", "maria", "elena", "sergey", "andrey", "alex", "vlad",
    "david", "daniel", "john", "roman", "nikita", "pavel", "denis", "anton",
    "oleg", "igor", "mark", "victor", "kirill", "stepan", "gleb", "bogdan",
    "yaroslav", "ruslan", "vadim", "arthur", "stanislav", "telegram", "market",
    "admin", "owner", "user", "channel"
}

import time

_showcase_cache: Dict[str, Tuple[float, int]] = {}

async def verify_candidate_showcase(
    peer_id: Any,
    target_slug: str,
    target_num: Optional[int] = None,
    client: Optional[TelegramClient] = None,
    require_presence: bool = False
) -> Tuple[bool, int, Optional[str]]:
    """
    Строгая верификация кандидата:
    1. Проверяет локальный кэш китов (мгновенно отсекает известных дилеров).
    2. Запрашивает витрину пользователя со всеми подарками (exclude_unsaved=False).
    3. Проверяет общее количество подарков: СТРОГО ОТ 1 ДО 5 (0 подарков или >5 исключаются).
    4. Если require_presence=True — проверяет обязательное наличие target_slug на витрине.
    """
    if not peer_id:
        return False, 999, None

    clean_key = str(peer_id).strip().lstrip("@").lower()
    if clean_key in BLACKLISTED_HANDLES or clean_key in COMMON_FIRST_NAMES or clean_key.endswith("bot") or is_wallet_string(clean_key):
        return False, 999, None

    now = time.time()
    if clean_key in _showcase_cache and not require_presence:
        ts, cached_count = _showcase_cache[clean_key]
        if now - ts < 43200:  # 12 часов кэш китов
            if cached_count > 5 or cached_count < 1:
                return False, cached_count, None
            return True, cached_count, None

    async def _fetch_gifts(c: TelegramClient):
        return await c(GetSavedStarGiftsRequest(
            peer=peer_id,
            offset='',
            limit=10,
            exclude_unsaved=False
        ))

    try:
        res = None
        if client:
            try:
                res = await _fetch_gifts(client)
            except FloodWaitError as fwe:
                session_pool.report_floodwait(client, fwe.seconds)
                res = await session_pool.execute_with_retry(_fetch_gifts)
        else:
            res = await session_pool.execute_with_retry(_fetch_gifts)

        if not res:
            return False, 999, None

        count = getattr(res, 'count', 0)
        _showcase_cache[clean_key] = (now, count)

        # Строгий фильтр: максимум 5 подарков, но не менее 1
        if count > 5:
            logger.info(f"🚫 [КИТ ОТСЕЯН]: Кандидат @{clean_key} имеет {count} подарков (> 5)!")
            return False, count, None

        if count < 1:
            logger.debug(f"🚫 [КАНДИДАТ БЕЗ ПОДАРКОВ]: У @{clean_key} 0 подарков.")
            return False, 0, None

        # Проверяем витрину на наличие подарка
        has_gift = False
        for item in getattr(res, 'gifts', []):
            g = getattr(item, 'gift', None)
            if not isinstance(g, StarGiftUnique):
                continue
            if getattr(g, 'slug', None) == target_slug:
                has_gift = True
                break
            if target_num is not None and target_num > 0 and getattr(g, 'num', None) == target_num:
                has_gift = True
                break

        if require_presence and not has_gift:
            return False, count, None

        return True, count, None

    except FloodWaitError as fwe:
        if client:
            session_pool.report_floodwait(client, fwe.seconds)
        return False, 999, None
    except Exception as e:
        logger.debug(f"Верификация витрины не удалась для {peer_id}: {e}")
        return False, 999, None

async def deanonymize_lot(
    client: Optional[TelegramClient],
    gift_obj: StarGiftUnique,
    users_map: Optional[List[User]] = None
) -> Optional[Tuple[int, Optional[str], str, int]]:
    """
    Многоуровневый каскад де-анонимизации анонимного лота MTProto:
    - 100% фильтрация китов: если у продавца > 3 подарков (холдер/дилер с 1000+ подарков) — СТРОГИЙ СТОП!
    - Обходы для извлечения реального @username
    - Возвращает только подтверждённого обычного пользователя (<= 3 подарка) с доступным юзернеймом.
    """
    slug = getattr(gift_obj, 'slug', None)
    num = getattr(gift_obj, 'num', None)
    raw_owner = getattr(gift_obj, 'owner_name', '') or ''
    clean_name = raw_owner.strip().lstrip("@")

    # СТРОГИЙ СТОП: Блокчейн TON, кошельки и каналы исключаются полностью
    if (
        is_onchain_or_invalid_owner(
            owner_name=raw_owner,
            owner_address=getattr(gift_obj, 'owner_address', None),
            owner_id=getattr(gift_obj, 'owner_id', None),
            gift_obj=gift_obj
        )
        or is_wallet_string(raw_owner)
        or getattr(gift_obj, 'gift_address', None)
        or getattr(gift_obj, 'owner_address', None)
    ):
        logger.info(f"🚫 [Deanonymizer - БЛОКЧЕЙН/КОШЕЛЕК ОТСЕЯН]: {slug} (owner: {raw_owner})")
        return None

    # ==============================================================
    # ШАГ 1 (ГЛАВНЫЙ): Проверка реального @username продавца
    # Строго: юзернейм ОБЯЗАН содержать символ '@' (например, @sashka_nft или "Иван @ivannft")!
    # Произвольные имена (Drake, Ksenia, Polina и т.д.) НЕ являются юзернеймами и не опрашиваются!
    # ==============================================================
    possible_handles = []
    # 1. Поиск @username в тексте (например, @sashka_nft или kween @kweenrl)
    for match in re.findall(r'@([a-zA-Z0-9_]{4,32})', raw_owner):
        clean_m = match.lower().strip()
        if clean_m not in BLACKLISTED_HANDLES and not clean_m.endswith("bot") and not is_wallet_string(clean_m):
            possible_handles.append(clean_m)

    possible_handles = list(set(possible_handles))

    for handle in possible_handles:
        try:
            is_valid, cnt, _ = await verify_candidate_showcase(handle, slug, num, client=client)
            if not is_valid or cnt > 5:
                return None  # СТРОГИЙ СТОП: более 5 подарков, запрещено слать!

            logger.info(f"🎯 [Продавец подтвержден]: @{handle} (подарков: {cnt})")
            return (None, handle, "Owner Username Match", cnt)
        except Exception as e:
            logger.debug(f"Проверка @{handle}: {e}")

    # ==============================================================
    # ШАГ 2: users_map в ответе пакета Telegram
    # ==============================================================
    if users_map:
        for u in users_map:
            if not isinstance(u, User) or u.bot:
                continue
            u_first = (u.first_name or "").strip().lower()
            u_user = (u.username or "").strip().lower()
            if clean_name and (u_first == clean_name.lower() or u_user == clean_name.lower()):
                is_valid, cnt, _ = await verify_candidate_showcase(u.id, slug, num, client=client)
                if not is_valid or cnt > 5:
                    logger.info(f"🚫 [КИТ ОТСЕЯН]: Кандидат {u.username or u.id} имеет {cnt} подарков (> 5)!")
                    return None
                if u.username:
                    return (u.id, u.username, "users_map match", cnt)

    # ==============================================================
    # ШАГ 3: Локальный SQLite Seller Cache (история прошлых сделок)
    # ==============================================================
    if clean_name:
        candidates = await db.find_sellers_by_name(clean_name)
        if candidates:
            for cand in candidates:
                cand_id = cand["tg_id"]
                cand_user = cand.get("username")
                is_valid, cnt, _ = await verify_candidate_showcase(cand_id, slug, num, client=client)
                if not is_valid or cnt > 5:
                    logger.info(f"🚫 [КИТ ОТСЕЯН]: Кандидат из кэша {cand_user or cand_id} имеет {cnt} подарков (> 5)!")
                    return None
                if cand_user:
                    return (cand_id, cand_user, "Seller Cache", cnt)

    # ==============================================================
    # ШАГ 4: Gift Registry DB (поиск по сквозной истории подарка)
    # ==============================================================
    if slug:
        reg_entry = await db.get_gift_from_registry(slug=slug, num=num)
        if reg_entry and reg_entry.get("owner_id"):
            cand_id = reg_entry["owner_id"]
            cand_user = reg_entry.get("owner_username")
            is_valid, cnt, _ = await verify_candidate_showcase(cand_id, slug, num, client=client)
            if not is_valid or cnt > 5:
                return None
            if cand_user:
                return (cand_id, cand_user, "Registry DB", cnt)

    # ==============================================================
    # ШАГ 5: Direct Peer (если Telegram вернул реальный owner_id)
    # ==============================================================
    owner_id_obj = getattr(gift_obj, 'owner_id', None)
    if isinstance(owner_id_obj, PeerUser) and owner_id_obj.user_id > 0:
        cand_id = owner_id_obj.user_id
        is_valid, cnt, _ = await verify_candidate_showcase(cand_id, slug, num, client=client)
        if not is_valid or cnt > 5:
            return None
        try:
            ent = await session_pool.execute_with_retry(lambda c: c.get_entity(cand_id))
            if isinstance(ent, User) and ent.username:
                return (cand_id, ent.username, "Direct Peer", cnt)
        except Exception:
            pass

    # ==============================================================
    # ШАГ 6: Original Details (ТОЛЬКО если лот НЕ в блокчейне и реально есть на витрине)
    # ==============================================================
    if not clean_name or clean_name.lower().startswith("market") or clean_name.lower() == "telegram":
        if not getattr(gift_obj, 'owner_address', None) and not getattr(gift_obj, 'gift_address', None):
            for attr in getattr(gift_obj, 'attributes', []):
                if isinstance(attr, StarGiftAttributeOriginalDetails):
                    for orig_id in [getattr(attr, 'recipient_id', None), getattr(attr, 'sender_id', None)]:
                        if isinstance(orig_id, PeerUser) and orig_id.user_id > 0:
                            cand_id = orig_id.user_id
                            # Строго: лот ОБЯЗАН присутствовать в сохраненных подарках кандидата!
                            is_valid, cnt, _ = await verify_candidate_showcase(cand_id, slug, num, client=client, require_presence=True)
                            if is_valid and 1 <= cnt <= 5:
                                try:
                                    ent = await session_pool.execute_with_retry(lambda c: c.get_entity(cand_id))
                                    if isinstance(ent, User) and ent.username and not ent.bot and not getattr(ent, 'deleted', False):
                                        logger.info(f"🎯 [Original Details]: Подтвержден @{ent.username} (ID {cand_id})")
                                        return (cand_id, ent.username, "Original Details", cnt)
                                except Exception:
                                    pass

    return None
