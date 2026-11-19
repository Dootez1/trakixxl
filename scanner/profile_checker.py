import re
import asyncio
import logging
from typing import Optional, Dict, Any, List
import config

logger = logging.getLogger(__name__)

MALE_ROOTS = [
    "alex", "алекс", "sash", "саш", "dmitr", "дмит", "dima", "дим",
    "vlad", "влад", "artem", "артём", "артем", "danil", "данил", "dany", "даня",
    "maks", "макс", "maxim", "serg", "серг", "andr", "андр", "mikh", "мих", "mish", "миш",
    "nikit", "никит", "egor", "егор", "kirill", "кирилл", "oleg", "олег", "anton", "антон",
    "rom", "ром", "pavel", "павел", "pash", "паш", "igor", "игор", "timur", "тимур",
    "denis", "денис", "gleb", "глеб", "ivan", "иван", "vany", "ваня", "ily", "иль",
    "stepan", "степан", "kosty", "кост", "konstantin", "bogdan", "богдан", "yaroslav", "ярослав",
    "slava", "слава", "stas", "стас", "arthur", "артур", "ruslan", "руслан", "vadim", "вадим"
]

FEMALE_ROOTS = [
    # Точные женские имена (латиница)
    "vika", "viktoria", "valeria", "lerka", "lera", "nastia", "nastya", "anastasia",
    "alina", "dasha", "daria", "katya", "katerina", "ekaterina", "polina", "kristina",
    "sonya", "sofia", "sophia", "masha", "maria", "olya", "olga", "liza", "elizaveta",
    "elena", "diana", "kira", "milana", "arina", "angelina", "karina", "ksenia", "ksyusha",
    "yulia", "julia", "veronika", "tanya", "tatiana", "natasha", "natalia", "irina",
    "sveta", "svetlana", "oksana", "snezhana", "kamilla", "violetta", "varvara",
    "girl", "princess", "queen", "babe", "baby", "barbie", "lady",
    # Точные женские имена (кириллица)
    "вика", "виктория", "валерия", "лерка", "лера", "настя", "анастасия",
    "алина", "даша", "дарья", "катя", "катерина", "екатерина", "полина", "кристина",
    "соня", "софия", "маша", "мария", "оля", "ольга", "лиза", "елизавета",
    "лена", "елена", "диана", "кира", "милана", "арина", "ангелина", "карина", "ксения", "ксюша",
    "юля", "юлия", "вероника", "таня", "татьяна", "наташа", "наталья", "ирина",
    "света", "светлана", "оксана", "снежана", "камилла", "виолетта", "варвара",
    "девушка", "принцесса", "мисс"
]

FEMALE_ENDINGS = ("ова", "ева", "ина", "ына", "ская", "цкая", "овна", "евна", "ична", "ova", "eva", "ina", "skaya")
FEMALE_SYMBOLS = ["♀", "🎀", "🌸", "💅", "she/her", "девушка", "beauty", "girl", "queen", "princess"]

def detect_is_female(first_name: Optional[str], last_name: Optional[str], username: Optional[str], bio: Optional[str]) -> bool:
    """Точное определение девушек: исключает мужские имена (@alex и т.д.) и пропускает только подтверждённых девушек."""
    text_to_check = f"{first_name or ''} {last_name or ''} {username or ''} {bio or ''}".lower()

    # 1. Если есть мужские корни (Alex, Sasha, Dima, Vlad и т.д.) — СТРОГО НЕ ДЕВУШКА
    for mroot in MALE_ROOTS:
        if mroot in text_to_check:
            return False

    # 2. Поиск точных корней женских имён
    for root in FEMALE_ROOTS:
        if root in text_to_check:
            return True

    # 3. Проверка женских окончаний фамилий / отчеств
    tokens = re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', text_to_check)
    for t in tokens:
        if any(t.endswith(end) for end in FEMALE_ENDINGS):
            return True

    # 4. Проверка женских эмодзи и символов
    if any(sym in text_to_check for sym in FEMALE_SYMBOLS):
        return True

    return False

from scanner.session_pool import session_pool

class ProfileChecker:
    def __init__(self):
        self._is_ready = False
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}  # key -> (timestamp, data)

    @property
    def client(self):
        return session_pool.get_client()

    async def init(self):
        """Инициализация пула Telethon сессий."""
        count = await session_pool.init()
        self._is_ready = count > 0
        if self._is_ready:
            logger.info(f"✅ Пул сессий подключен ({count} активных аккаунтов). Полный доступ к MTProto.")
        else:
            logger.warning("Сессии не найдены. Для подключения запустите auth_session.py.")

    async def check_user(self, username_or_id: str) -> Dict[str, Any]:
        """
        ВЖИВУЮ проверяет профиль через серверы Telegram:
        - Существует ли пользователь
        - Реальные видимые подарки (<= 3 подарка)
        - Доступность бесплатных сообщений (contact_require_premium)
        - Быстрое in-memory кэширование (без повторных запросов)
        - Без тяжелого GetFullUserRequest (исключает FloodWait)
        """
        import time
        from telethon.tl.types import User

        if isinstance(username_or_id, User):
            entity = username_or_id
            clean_user = entity.username or str(entity.id)
        else:
            clean_user = str(username_or_id).strip().lstrip("@")
            entity = None

        cache_key = clean_user.lower()
        now = time.time()
        if cache_key in self._cache:
            ts, cached_res = self._cache[cache_key]
            if now - ts < 3600:  # Кэш 1 час
                return dict(cached_res)

        result = {
            "username": clean_user if not clean_user.isdigit() else None,
            "tg_id": entity.id if entity else (int(clean_user) if clean_user.isdigit() else None),
            "can_write_free": True,
            "restriction_reason": "Бесплатно",
            "gifts_count": 0,
            "is_female": False,
            "gender": "unknown",
            "premium_level": 0,
            "usernames_count": 1,
            "is_rich": False,
            "skip_reason": "",
            "real_gifts": []
        }

        if not self._is_ready or not self.client:
            result["skip_reason"] = "Сессия не авторизована"
            return result

        try:
            from telethon.tl.functions.payments import GetSavedStarGiftsRequest
            from telethon.errors import FloodWaitError

            if entity is None:
                async def _get_ent(c):
                    if isinstance(username_or_id, int) or (isinstance(username_or_id, str) and str(username_or_id).isdigit()):
                        return await c.get_entity(int(username_or_id))
                    else:
                        return await c.get_entity(clean_user)

                try:
                    entity = await session_pool.execute_with_retry(_get_ent)
                except Exception as e_ent:
                    result["skip_reason"] = f"Профиль @{clean_user} не найден: {e_ent}"
                    result["is_rich"] = True
                    result["gifts_count"] = 999
                    return result

                if not entity:
                    result["skip_reason"] = "Сессии пула временно в кулдауне"
                    result["is_rich"] = True
                    result["gifts_count"] = 999
                    return result

            from scanner.filters import is_onchain_or_invalid_owner
            if not isinstance(entity, User) or entity.bot or getattr(entity, 'deleted', False):
                result["skip_reason"] = "Не личный профиль (канал, бот или удалённый аккаунт)"
                result["is_rich"] = True
                result["gifts_count"] = 999
                return result

            if entity.username and is_onchain_or_invalid_owner(owner_name=entity.username):
                result["skip_reason"] = "Владелец является адресом кошелька TON"
                result["is_rich"] = True
                result["gifts_count"] = 999
                return result

            result["tg_id"] = entity.id
            if entity.username:
                result["username"] = entity.username

            # 1. Проверка юзернеймов (не больше 1)
            usernames_list = getattr(entity, 'usernames', None)
            usernames_count = len(usernames_list) if usernames_list else (1 if entity.username else 0)
            result["usernames_count"] = usernames_count
            if usernames_count > 1:
                result["is_rich"] = True
                result["skip_reason"] = f"Больше 1 юзернейма ({usernames_count})"
                self._cache[cache_key] = (now, dict(result))
                return result

            # 2. Быстрые свойства профиля
            is_premium = bool(getattr(entity, 'premium', False))
            result["premium_level"] = 1 if is_premium else 0

            # 3. Проверка ограничений на ЛС (только с Premium)
            if getattr(entity, 'contact_require_premium', False):
                result["can_write_free"] = False
                result["restriction_reason"] = "Только с Premium"

            # 4. РЕАЛЬНЫЕ ПОДАРКИ (exclude_unsaved=False считает ВСЕ подарки на аккаунте)
            async def _get_gifts(c):
                return await c(GetSavedStarGiftsRequest(
                    peer=entity,
                    offset="",
                    limit=10,
                    exclude_unsaved=False
                ))

            try:
                saved_gifts = await session_pool.execute_with_retry(_get_gifts)
                if saved_gifts:
                    visible_count = getattr(saved_gifts, 'count', 0)
                    result["gifts_count"] = visible_count
                    result["real_gifts"] = getattr(saved_gifts, 'gifts', [])

                    # СТРОГИЙ ФИЛЬТР: Не более 5 подарков в профиле!
                    if visible_count > 5:
                        result["is_rich"] = True
                        result["skip_reason"] = f"Слишком богатый: {visible_count} подарков (максимум 5)"
                        self._cache[cache_key] = (now, dict(result))
                        return result
                else:
                    # Все сессии в кулдауне
                    result["is_rich"] = True
                    result["gifts_count"] = 999
                    result["skip_reason"] = "Пул сессий временно в кулдауне"
                    return result
            except Exception as e_gifts:
                logger.debug(f"Ошибка получения подарков @{clean_user}: {e_gifts}")
                result["is_rich"] = True
                result["gifts_count"] = 999
                return result

            # 5. Определение пола
            is_fem = detect_is_female(entity.first_name, entity.last_name, entity.username, None)
            result["is_female"] = is_fem
            result["gender"] = "female" if is_fem else "male"

            self._cache[cache_key] = (now, dict(result))

        except Exception as e:
            err_str = str(e).lower()
            if "flood wait" in err_str or "wait of" in err_str:
                result["skip_reason"] = ""
                result["can_write_free"] = True
                result["is_rich"] = False
                result["gifts_count"] = 1
                return result
            logger.debug(f"Ошибка проверки @{clean_user}: {e}")
            result["skip_reason"] = f"Ошибка доступа к профилю: {e}"

        return result

    async def close(self):
        await session_pool.close()

profile_checker = ProfileChecker()
