import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional, Callable, Any, Set
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionRevokedError, UserDeactivatedError

import config

logger = logging.getLogger("SessionPool")

class SessionPool:
    """
    Профессиональный пул сессий Telethon с горячим подключением:
    - Поддержка файлов .session в папке sessions/ и корне проекта
    - Поддержка строковых ключей StringSession из sessions.txt
    - Горячее авто-обнаружение новых сессий на лету без перезапуска
    - Авто-ротация и нулевой простой при FloodWait
    """
    def __init__(self):
        self.clients: List[TelegramClient] = []
        self.loaded_sources: Set[str] = set()
        self.cooldowns: dict[int, float] = {}  # index -> timestamp
        self._current_index = 0
        self.is_initialized = False
        self._last_refresh = 0

    async def init(self) -> int:
        """Первичная инициализация пула."""
        await self.refresh_pool()
        return len(self.clients)

    async def refresh_pool(self):
        """Сканирует и подключает новые сессии на лету без остановки бота."""
        session_paths = []

        # 1. Папка sessions/
        sessions_dir = config.BASE_DIR / "sessions"
        if sessions_dir.exists():
            for p in sessions_dir.glob("*.session"):
                if not p.name.startswith((".", "_", "tmp_")):
                    session_paths.append(p)

        # 2. Сессии в корне проекта
        for p in config.BASE_DIR.glob("*.session"):
            if not p.name.startswith((".", "_", "tmp_")) and p not in session_paths:
                session_paths.append(p)

        # 3. Дефолтная сессия из config
        default_session = config.BASE_DIR / f"{config.TELEGRAM_SESSION_NAME}.session"
        if default_session.exists() and default_session not in session_paths:
            session_paths.append(default_session)

        # Подключаем новые .session файлы
        for path in session_paths:
            src_key = f"file:{path.resolve()}"
            if src_key in self.loaded_sources:
                continue

            try:
                stem_path = str(path.with_suffix(""))
                client = TelegramClient(
                    stem_path,
                    config.TELEGRAM_API_ID,
                    config.TELEGRAM_API_HASH,
                    device_model="PC 64bit",
                    system_version="Windows 10",
                    app_version="5.4.1 x64",
                    flood_sleep_threshold=0
                )
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    if me:
                        username = f"@{me.username}" if getattr(me, 'username', None) else (me.first_name or "User")
                        logger.info(f"✅ [НОВАЯ СЕССИЯ ПОДКЛЮЧЕНА]: {path.name} -> {username} (ID: {me.id})")
                        self.clients.append(client)
                        self.loaded_sources.add(src_key)
                    else:
                        self.loaded_sources.add(src_key)
                        await client.disconnect()
                else:
                    self.loaded_sources.add(src_key)
                    await client.disconnect()
            except Exception as e:
                logger.error(f"Ошибка загрузки сессии {path.name}: {e}")

        # 4. Проверяем строковые сессии StringSession в sessions.txt
        txt_path = config.BASE_DIR / "sessions.txt"
        if txt_path.exists():
            try:
                content = txt_path.read_text(encoding="utf-8")
                for line_idx, line in enumerate(content.splitlines(), 1):
                    key = line.strip()
                    if key and not key.startswith("#") and len(key) > 40:
                        src_key = f"str:{key[:20]}"
                        if src_key in self.loaded_sources:
                            continue

                        try:
                            client = TelegramClient(
                                StringSession(key),
                                config.TELEGRAM_API_ID,
                                config.TELEGRAM_API_HASH,
                                device_model="PC 64bit",
                                system_version="Windows 10",
                                app_version="5.4.1 x64",
                                flood_sleep_threshold=0
                            )
                            await client.connect()
                            if await client.is_user_authorized():
                                me = await client.get_me()
                                username = f"@{me.username}" if me.username else me.first_name
                                logger.info(f"✅ [НОВАЯ StringSession #{line_idx} ПОДКЛЮЧЕНА]: {username} (ID: {me.id})")
                                self.clients.append(client)
                                self.loaded_sources.add(src_key)
                            else:
                                await client.disconnect()
                        except Exception as se:
                            logger.error(f"Ошибка загрузки StringSession #{line_idx}: {se}")
            except Exception as e:
                logger.error(f"Ошибка чтения sessions.txt: {e}")

        self.is_initialized = len(self.clients) > 0
        self._last_refresh = time.time()

    def get_client(self, exclude_clients: Optional[Set[TelegramClient]] = None) -> Optional[TelegramClient]:
        """
        Возвращает следующий доступный клиент не в кулдауне (Round-Robin).
        Если exclude_clients указан, пропускает эти клиенты (для ротации при ошибке).
        """
        if not self.clients:
            return None

        # Раз в 30 секунд запускаем фоновую проверку новых сессий
        if time.time() - self._last_refresh > 30:
            self._last_refresh = time.time()
            try:
                asyncio.create_task(self.refresh_pool())
            except Exception:
                pass

        now = time.time()
        num_clients = len(self.clients)
        excluded = exclude_clients or set()

        # 1. Ищем свободный клиент не в кулдауне и не исключенный
        for _ in range(num_clients):
            idx = self._current_index % num_clients
            self._current_index += 1

            client = self.clients[idx]
            if client in excluded:
                continue

            cool_until = self.cooldowns.get(idx, 0)
            if now >= cool_until:
                return client

        # 2. Если все доступные клиенты в кулдауне, возвращаем None
        return None

    def report_floodwait(self, client: TelegramClient, seconds: int):
        """Регистрирует кулдаун для клиента, поймавшего FloodWait."""
        try:
            idx = self.clients.index(client)
            effective_seconds = max(int(seconds) + 2, 5)
            self.cooldowns[idx] = time.time() + effective_seconds
            logger.warning(f"⏳ Сессия #{idx + 1} в кулдауне на {effective_seconds} сек. Переключение на другую сессию!")
        except ValueError:
            pass

    async def execute_with_retry(self, operation: Callable[[TelegramClient], Any], max_retries: int = 4) -> Any:
        """
        Выполняет MTProto операцию с автоматическим переключением сессий при FloodWait.
        Пробует до max_retries разных сессий из пула без потери лота.
        """
        failed_clients: Set[TelegramClient] = set()
        total = len(self.clients)
        retries = min(max_retries, total) if total > 0 else 1

        for _ in range(retries):
            client = self.get_client(exclude_clients=failed_clients)
            if not client:
                break

            try:
                if not client.is_connected():
                    await client.connect()
                return await operation(client)
            except FloodWaitError as fwe:
                self.report_floodwait(client, fwe.seconds)
                failed_clients.add(client)
            except Exception as e:
                err_str = str(e).lower()
                if "flood" in err_str:
                    self.report_floodwait(client, 30)
                    failed_clients.add(client)
                else:
                    raise e
        return None

    async def close(self):
        """Отключает все клиенты пула."""
        for c in self.clients:
            try:
                if c.is_connected():
                    await c.disconnect()
            except Exception:
                pass
        self.clients = []
        self.loaded_sources.clear()
        self.is_initialized = False

session_pool = SessionPool()
