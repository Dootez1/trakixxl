import aiosqlite
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from config import DB_PATH

MSK = timezone(timedelta(hours=3))

class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path, timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gift_identifier TEXT UNIQUE,
                    gift_name TEXT NOT NULL,
                    gift_link TEXT NOT NULL,
                    gift_image TEXT,
                    owner_username TEXT,
                    owner_tg_id INTEGER,
                    price REAL DEFAULT 0,
                    currency TEXT DEFAULT 'GRAM',
                    is_upgraded INTEGER DEFAULT 0,
                    can_write_free INTEGER DEFAULT 1,
                    gifts_count INTEGER DEFAULT 0,
                    owner_gender TEXT DEFAULT 'unknown',
                    filter_type TEXT NOT NULL,
                    model TEXT DEFAULT 'Original',
                    backdrop TEXT DEFAULT 'Persimmon',
                    pattern TEXT DEFAULT 'Classic',
                    premium_level INTEGER DEFAULT 1,
                    group_message_id INTEGER,
                    group_thread_id INTEGER,
                    status TEXT DEFAULT 'available',
                    claimed_by_id INTEGER,
                    claimed_by_username TEXT,
                    claimed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            for col, col_def in [
                ("model", "TEXT DEFAULT 'Original'"),
                ("backdrop", "TEXT DEFAULT 'Persimmon'"),
                ("pattern", "TEXT DEFAULT 'Classic'"),
                ("premium_level", "INTEGER DEFAULT 1"),
                ("owner_name", "TEXT")
            ]:
                try:
                    await db.execute(f"ALTER TABLE lots ADD COLUMN {col} {col_def}")
                except Exception:
                    pass

            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_claim_timestamp REAL DEFAULT 0,
                    claims_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS seen_slugs (
                    slug TEXT PRIMARY KEY,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS gift_registry (
                    slug TEXT PRIMARY KEY,
                    num INTEGER,
                    owner_id INTEGER,
                    owner_username TEXT,
                    owner_name TEXT,
                    model TEXT,
                    backdrop TEXT,
                    pattern TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_registry_num ON gift_registry(num);")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS seller_cache (
                    name_clean TEXT,
                    tg_id INTEGER,
                    username TEXT,
                    last_seen_ts REAL,
                    PRIMARY KEY (name_clean, tg_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_seller_name ON seller_cache(name_clean);")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS target_base (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    tg_id INTEGER,
                    source TEXT DEFAULT 'manual',
                    status TEXT DEFAULT 'pending',
                    checked_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def register_user(self, user_id: int, username: Optional[str], first_name: Optional[str]):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
            """, (user_id, username, first_name))
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def check_and_update_cooldown(self, user_id: int, cooldown_seconds: int = 5) -> tuple[bool, float]:
        """
        Проверяет, прошел ли кулдаун (по ТЗ 5 секунд).
        Возвращает (can_claim, remaining_seconds).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT last_claim_timestamp FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                now = time.time()
                if row and row["last_claim_timestamp"]:
                    elapsed = now - row["last_claim_timestamp"]
                    if elapsed < cooldown_seconds:
                        return False, round(cooldown_seconds - elapsed, 1)

                await db.execute("""
                    INSERT INTO users (user_id, last_claim_timestamp)
                    VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET last_claim_timestamp = excluded.last_claim_timestamp
                """, (user_id, now))
                await db.commit()
                return True, 0.0

    async def add_lot(self, lot_data: Dict[str, Any]) -> Optional[int]:
        """Добавляет новый лот, если такого еще нет."""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute("""
                    INSERT INTO lots (
                        gift_identifier, gift_name, gift_link, gift_image,
                        owner_username, owner_tg_id, owner_name, price, currency,
                        is_upgraded, can_write_free, gifts_count, owner_gender,
                        filter_type, model, backdrop, pattern, premium_level, status
                    ) VALUES (
                        :gift_identifier, :gift_name, :gift_link, :gift_image,
                        :owner_username, :owner_tg_id, :owner_name, :price, :currency,
                        :is_upgraded, :can_write_free, :gifts_count, :owner_gender,
                        :filter_type, :model, :backdrop, :pattern, :premium_level, 'available'
                    )
                """, lot_data)
                await db.commit()
                return cursor.lastrowid
            except aiosqlite.IntegrityError:
                return None  # Уже существует

    async def update_lot_message(self, lot_id: int, message_id: int, thread_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE lots SET group_message_id = ?, group_thread_id = ? WHERE id = ?
            """, (message_id, thread_id, lot_id))
            await db.commit()

    async def get_lot(self, lot_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def claim_lot(self, lot_id: int, user_id: int, username: Optional[str]) -> tuple[bool, Optional[Dict[str, Any]], str]:
        """
        Атомарно занимает лот.
        Возвращает (success, lot_data, error_message).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)) as cursor:
                lot = await cursor.fetchone()
                if not lot:
                    return False, None, "Лот не найден."
                if lot["status"] != "available":
                    claimed_by = lot["claimed_by_username"] or f"ID {lot['claimed_by_id']}"
                    return False, None, f"❌ Данный лот уже занял другой воркер (@{claimed_by})!\n\n❗️ Этот лот скрыт в избежании кражи мамонта."

            # Атомарное обновление
            now_msk_str = datetime.now(MSK).strftime("%H:%M:%S")
            cursor = await db.execute("""
                UPDATE lots
                SET status = 'claimed',
                    claimed_by_id = ?,
                    claimed_by_username = ?,
                    claimed_at = ?
                WHERE id = ? AND status = 'available'
            """, (user_id, username, now_msk_str, lot_id))
            await db.commit()

            if cursor.rowcount == 0:
                return False, None, "Лот только что был занят другим участником!"

            # Увеличиваем счетчик пользователя
            await db.execute("""
                UPDATE users SET claims_count = claims_count + 1 WHERE user_id = ?
            """, (user_id,))
            await db.commit()

            # Получаем обновленный лот
            async with db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)) as cursor:
                updated_lot = await cursor.fetchone()
                return True, dict(updated_lot), ""

    async def get_user_claimed_lots(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM lots WHERE claimed_by_id = ? ORDER BY id DESC LIMIT ?
            """, (user_id, limit)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def add_targets_to_base(self, usernames: List[str], source: str = "manual") -> int:
        added = 0
        async with aiosqlite.connect(self.db_path) as db:
            for u in usernames:
                clean_u = u.strip().lstrip("@")
                if not clean_u:
                    continue
                try:
                    await db.execute("""
                        INSERT INTO target_base (username, source, status)
                        VALUES (?, ?, 'pending')
                    """, (clean_u, source))
                    added += 1
                except aiosqlite.IntegrityError:
                    pass
            await db.commit()
        return added

    async def get_pending_targets(self, limit: int = 50) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM target_base WHERE status = 'pending' LIMIT ?
            """, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def update_target_status(self, target_id: int, status: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE target_base SET status = ?, checked_at = CURRENT_TIMESTAMP WHERE id = ?
            """, (status, target_id))
            await db.commit()

    async def release_lot(self, lot_id: int, user_id: int) -> tuple[bool, Optional[Dict[str, Any]], str]:
        """Освобождает ранее занятый лот (кнопка 'Отпустить лот')."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)) as cursor:
                lot = await cursor.fetchone()
                if not lot:
                    return False, None, "Лот не найден."
                if lot["claimed_by_id"] != user_id:
                    return False, None, "Вы не являетесь владельцем этого лота."

            cursor = await db.execute("""
                UPDATE lots
                SET status = 'available',
                    claimed_by_id = NULL,
                    claimed_by_username = NULL,
                    claimed_at = NULL
                WHERE id = ? AND claimed_by_id = ?
            """, (lot_id, user_id))
            await db.commit()

            if cursor.rowcount == 0:
                return False, None, "Не удалось освободить лот."

            async with db.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)) as cursor:
                updated_lot = await cursor.fetchone()
                return True, dict(updated_lot), ""

    async def get_top_claimers(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Лидерборд воркеров по количеству занятых лотов."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT user_id, username, first_name, claims_count
                FROM users
                WHERE claims_count > 0
                ORDER BY claims_count DESC
                LIMIT ?
            """, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_seen_slugs(self) -> set:
        """Загружает все ранее обработанные слаги из базы данных."""
        slugs = set()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT slug FROM seen_slugs") as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r[0]:
                        slugs.add(r[0])

            # Также добавляем слаги из таблицы lots
            async with db.execute("SELECT gift_identifier FROM lots") as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r[0]:
                        base_slug = r[0].split("_")[0]
                        slugs.add(base_slug)
        return slugs

    async def is_slug_seen(self, slug: str) -> bool:
        """Строгая проверка дубликата: был ли лот с таким слагом опубликован ранее."""
        if not slug:
            return True
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM seen_slugs WHERE slug = ? LIMIT 1", (slug,)) as cursor:
                if await cursor.fetchone():
                    return True
            async with db.execute("SELECT 1 FROM lots WHERE gift_identifier = ? OR gift_link LIKE ? LIMIT 1", (slug, f"%{slug}%")) as cursor:
                if await cursor.fetchone():
                    return True
        return False

    async def mark_slugs_seen(self, slugs: List[str]):
        """Сохраняет слаги как просмотренные/отправленные навсегда."""
        if not slugs:
            return
        async with aiosqlite.connect(self.db_path) as db:
            for s in slugs:
                if s:
                    await db.execute("INSERT OR IGNORE INTO seen_slugs (slug) VALUES (?)", (s,))
            await db.commit()

    async def record_gift_registry(self, slug: str, num: Optional[int], owner_id: int, owner_username: Optional[str], owner_name: Optional[str], model: str = None, backdrop: str = None, pattern: str = None):
        """Сохраняет или обновляет историю владения подарком по slug/num (Уровень 1)."""
        if not slug:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO gift_registry (slug, num, owner_id, owner_username, owner_name, model, backdrop, pattern, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(slug) DO UPDATE SET
                    num = COALESCE(excluded.num, gift_registry.num),
                    owner_id = excluded.owner_id,
                    owner_username = excluded.owner_username,
                    owner_name = excluded.owner_name,
                    model = COALESCE(excluded.model, gift_registry.model),
                    backdrop = COALESCE(excluded.backdrop, gift_registry.backdrop),
                    pattern = COALESCE(excluded.pattern, gift_registry.pattern),
                    updated_at = CURRENT_TIMESTAMP
            """, (slug, num, owner_id, owner_username, owner_name, model, backdrop, pattern))
            await db.commit()

    async def get_gift_from_registry(self, slug: str, num: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Ищет подарок в реестре истории по slug или num."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if slug:
                async with db.execute("SELECT * FROM gift_registry WHERE slug = ?", (slug,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        return dict(row)
            if num is not None and num > 0:
                async with db.execute("SELECT * FROM gift_registry WHERE num = ? ORDER BY updated_at DESC LIMIT 1", (num,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        return dict(row)
            return None

    async def record_seller_cache(self, raw_name: str, tg_id: int, username: Optional[str] = None):
        """Кэширует соответствие отображаемого имени профиля и его реального tg_id (Уровень 4)."""
        clean = (raw_name or "").strip().lower()
        if not clean or tg_id <= 0:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO seller_cache (name_clean, tg_id, username, last_seen_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name_clean, tg_id) DO UPDATE SET
                    username = COALESCE(excluded.username, seller_cache.username),
                    last_seen_ts = excluded.last_seen_ts
            """, (clean, tg_id, username, time.time()))
            await db.commit()

    async def find_sellers_by_name(self, raw_name: str) -> List[Dict[str, Any]]:
        """Ищет известных пользователей по очищенному имени из кэша."""
        clean = (raw_name or "").strip().lower()
        if not clean:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM seller_cache WHERE name_clean = ? ORDER BY last_seen_ts DESC LIMIT 5", (clean,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]


db = Database()
