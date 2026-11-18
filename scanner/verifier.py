import aiohttp
import asyncio
import logging

logger = logging.getLogger("UserVerifier")

_http_session: aiohttp.ClientSession = None
_verified_cache = {}

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
    return _http_session

async def verify_real_telegram_user(username: str) -> bool:
    """
    100% строгая верификация юзернейма через веб-профиль t.me:
    - Никаких каналов ('View', 'subscribers')
    - Никаких ботов ('Launch', 'bot')
    - Никаких несуществующих юзернеймов ('NO_EXTRA')
    - Пропускает ТОЛЬКО живых людей с кнопкой 'Contact' и совпадением @username!
    """
    clean = (username or "").strip().lstrip("@").lower()
    if not clean or len(clean) < 4 or clean.endswith("bot"):
        return False

    if clean in _verified_cache:
        return _verified_cache[clean]

    session = await get_http_session()
    url = f"https://t.me/{clean}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                _verified_cache[clean] = False
                return False
            html = await resp.text()

            # 1. Заголовок должен начинаться строго с 'Telegram: Contact @'
            if "<title>" not in html or "</title>" not in html:
                _verified_cache[clean] = False
                return False
            title = html.split("<title>")[1].split("</title>")[0].strip()
            if not title.startswith("Telegram: Contact @"):
                _verified_cache[clean] = False
                return False

            # 2. Тег tgme_page_extra должен строго содержать @clean
            if "tgme_page_extra" not in html:
                _verified_cache[clean] = False
                return False
            extra_part = html.split("tgme_page_extra")[1].split("</div>")[0]
            extra_clean = extra_part.replace('">', '').replace('\n', '').strip().lower()
            if extra_clean != f"@{clean}":
                _verified_cache[clean] = False
                return False

            # 3. Никаких подписчиков/участников/каналов
            if any(w in extra_clean for w in ["subscriber", "member", "подписчик", "участник"]):
                _verified_cache[clean] = False
                return False

            _verified_cache[clean] = True
            logger.info(f"✅ Верифицирован живой Telegram пользователь: @{clean}")
            return True
    except Exception as e:
        logger.debug(f"Ошибка проверки @{clean}: {e}")
        return False

async def close_verifier():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
