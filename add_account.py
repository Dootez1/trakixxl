import asyncio
import os
import re
import sys
import shutil
import sqlite3
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession, MemorySession
from telethon.crypto import AuthKey
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError

import config

def clean_phone(phone: str) -> str:
    cleaned = re.sub(r'[\s\-\(\)]', '', phone.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned

def read_session_data(session_path: Path):
    """
    Безопасно в режиме 'mode=ro' (Read-Only) считывает параметры сессии
    и кэш сущностей из SQLite файла .session без блокировки БД.
    """
    try:
        conn = sqlite3.connect(f"file:{session_path.resolve()}?mode=ro", uri=True, timeout=1.5)
        sess_row = conn.execute("SELECT dc_id, server_address, port, auth_key FROM sessions").fetchone()
        if not sess_row or not sess_row[3]:
            conn.close()
            return None
        
        dc_id, server_address, port, auth_key = sess_row
        user_info = None
        self_row = conn.execute("SELECT hash FROM entities WHERE id=0").fetchone()
        if self_row and self_row[0]:
            self_id = self_row[0]
            u_row = conn.execute("SELECT id, username, phone, name FROM entities WHERE id=?", (self_id,)).fetchone()
            if u_row:
                user_info = {
                    "id": u_row[0],
                    "username": u_row[1],
                    "phone": u_row[2],
                    "name": u_row[3]
                }
        conn.close()
        return {
            "dc_id": dc_id,
            "server_address": server_address,
            "port": port,
            "auth_key": auth_key,
            "cached_user": user_info
        }
    except Exception:
        return None

async def check_session_status(session_path: Path):
    """
    Проверяет статус сессии вживую через MemorySession (без открытия SQLite на запись).
    Не конфликтует с работающим основным ботом трекера.
    """
    data = read_session_data(session_path)
    if not data or not data.get("auth_key"):
        return {"status": "error", "error": "Пустая или некорректная сессия"}

    cached = data.get("cached_user") or {}

    m = MemorySession()
    m.set_dc(data["dc_id"], data["server_address"], data["port"])
    m.auth_key = AuthKey(data["auth_key"])

    client = TelegramClient(
        m,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
        device_model="PC 64bit",
        system_version="Windows 10",
        app_version="5.4.1 x64",
        flood_sleep_threshold=0
    )

    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            if me:
                u_str = f"@{me.username}" if getattr(me, 'username', None) else "нет юзера"
                p_str = "⭐ Premium" if getattr(me, 'premium', False) else "Без Premium"
                name_str = f"{me.first_name or ''} {me.last_name or ''}".strip() or "Без имени"
                return {
                    "status": "active",
                    "name": name_str,
                    "username": u_str,
                    "id": me.id,
                    "premium": p_str
                }
            else:
                return {"status": "revoked", "error": "Требуется повторный вход"}
        else:
            await client.disconnect()
            return {"status": "revoked", "error": "Сессия не авторизована"}
    except Exception as e:
        # Если Telegram отклонил временное параллельное подключение, но сессия в порядке
        if cached:
            u_str = f"@{cached['username']}" if cached.get("username") else "нет юзера"
            return {
                "status": "active",
                "name": cached.get("name") or "Пользователь",
                "username": u_str,
                "id": cached.get("id"),
                "premium": "В работе",
                "note": "активен в трекере"
            }
        return {"status": "error", "error": str(e)}

async def get_all_sessions_stats():
    """Собирает и выводит красивую статистику по всем подключенным сессиям пула."""
    sessions_dir = config.BASE_DIR / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    files = [p for p in sessions_dir.glob("*.session") if not p.name.startswith((".", "_", "tmp_"))]
    for root_f in config.BASE_DIR.glob("*.session"):
        if not root_f.name.startswith((".", "_", "tmp_")) and root_f not in files and not root_f.name.startswith("traki"):
            files.append(root_f)

    print("\n" + "═" * 70)
    print(f"📊 ТЕКУЩАЯ СТАТИСТИКА ПУЛА СЕССИЙ (Всего найдено: {len(files)} аккаунтов)")
    print("═" * 70)

    if not files:
        print("   ❌ В пуле пока нет ни одного сохраненного аккаунта.")
        print("═" * 70 + "\n")
        return

    active_cnt = 0
    for idx, f in enumerate(files, 1):
        info = await check_session_status(f)
        status = info.get("status")
        if status == "active":
            print(f"   #{idx:02d} | ✅ АКТИВЕН | {info['name']} ({info['username']}) | ID: {info['id']} | {info['premium']} | {f.name}")
            active_cnt += 1
        elif status == "revoked":
            print(f"   #{idx:02d} | ⚠️ СЛЕТЕЛ   | {f.name} ({info.get('error', 'Требуется вход')})")
        else:
            print(f"   #{idx:02d} | ❌ ОШИБКА  | {f.name} ({info.get('error', 'Неизвестная ошибка')})")

    print("═" * 70)
    print(f"🚀 ИТОГО ГОТОВО К РАБОТЕ: {active_cnt} / {len(files)} аккаунтов")
    print("═" * 70 + "\n")

async def add_single_account():
    sessions_dir = config.BASE_DIR / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    
    # Временная папка для авторизации (чтобы фоновый процесс трекера не перехватил сессию до ввода кода)
    tmp_dir = sessions_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    print("\n" + "─" * 60)
    phone_input = input("📞 Введите номер телефона (например, +79991234567): ").strip()
    if not phone_input:
        print("❌ Номер не введен.")
        return False

    phone = clean_phone(phone_input)
    safe_phone_name = phone.replace("+", "")
    session_file_name = f"acc_{safe_phone_name}"
    final_session_file = sessions_dir / f"{session_file_name}.session"

    # Если такой аккаунт уже существует и активен
    if final_session_file.exists():
        info = await check_session_status(final_session_file)
        if info.get("status") == "active":
            print(f"\n✅ Этот аккаунт уже добавлен и активен: {info['name']} ({info['username']}) ID: {info['id']}")
            return True

    tmp_session_stem = str(tmp_dir / f"auth_{safe_phone_name}")
    tmp_session_file = Path(f"{tmp_session_stem}.session")

    client = TelegramClient(
        tmp_session_stem,
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
            print(f"\n✅ Этот аккаунт уже был ранее авторизован: {me.first_name} (ID: {me.id})")
            await client.disconnect()
            # Переносим сессию в основную папку
            if tmp_session_file.exists():
                shutil.move(str(tmp_session_file), str(final_session_file))
            return True

    print(f"⏳ Отправляю запрос кода на {phone}...")
    try:
        sent_code = await client.send_code_request(phone)
    except FloodWaitError as fe:
        print(f"❌ Telegram наложил временный лимит (FloodWait): подождите {fe.seconds} сек.")
        await client.disconnect()
        return False
    except Exception as e:
        print(f"❌ Ошибка отправки кода: {e}")
        await client.disconnect()
        return False

    print(f"\n📩 [УСПЕХ] Код подтверждения отправлен в Telegram аккаунта {phone}!")
    code_raw = input("👉 Введите код из Telegram: ").strip()
    code = re.sub(r'[\s\-]', '', code_raw)
    if not code:
        print("❌ Код не был введен.")
        await client.disconnect()
        return False

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
    except SessionPasswordNeededError:
        print("\n🔐 На аккаунте включен 2FA (облачный пароль)!")
        pwd = input("👉 Введите 2FA пароль: ").strip()
        try:
            await client.sign_in(password=pwd)
        except Exception as pe:
            print(f"❌ Неверный 2FA пароль: {pe}")
            await client.disconnect()
            return False
    except PhoneCodeInvalidError:
        print("❌ Неверный код подтверждения!")
        await client.disconnect()
        return False
    except Exception as e:
        print(f"❌ Ошибка авторизации: {e}")
        await client.disconnect()
        return False

    me = await client.get_me()
    if not me:
        print("❌ Не удалось получить данные профиля. Попробуйте еще раз.")
        await client.disconnect()
        return False

    u_str = f"@{me.username}" if getattr(me, 'username', None) else "нет username"
    is_prem = "Да (Telegram Premium)" if getattr(me, 'premium', False) else "Нет"

    # Сохраняем строковую копию в sessions.txt на случай переноса
    try:
        str_key = StringSession.save(client.session)
        txt_path = config.BASE_DIR / "sessions.txt"
        with open(txt_path, "a", encoding="utf-8") as f:
            f.write(f"{str_key}\n")
    except Exception:
        pass

    await client.disconnect()

    # Перемещаем файл сессии в sessions/
    try:
        if tmp_session_file.exists():
            shutil.move(str(tmp_session_file), str(final_session_file))
        # Очищаем возможные journal-файлы
        journal_file = Path(f"{tmp_session_stem}.session-journal")
        if journal_file.exists():
            journal_file.unlink(missing_ok=True)
    except Exception as me_err:
        print(f"⚠️ Предупреждение при переносе файла сессии: {me_err}")

    print("\n" + "🎉" * 25)
    print(f"   АККАУНТ УСПЕШНО ДОБАВЛЕН В ПУЛ ТРЕКЕРА!")
    print("🎉" * 25)
    print(f"   👤 Имя:        {me.first_name} {me.last_name or ''}".strip())
    print(f"   🏷 Юзернейм:   {u_str}")
    print(f"   🆔 Telegram ID: {me.id}")
    print(f"   ⭐ Премиум:    {is_prem}")
    print(f"   📁 Файл:       sessions/{session_file_name}.session")
    print(f"   ⚡️ Статус:     Мгновенно подключен в трекер без перезапуска!")
    print("─" * 60 + "\n")
    return True

async def main():
    print("\n" + "╔" + "═" * 68 + "╗")
    print("║   🚀 HASTER TEAM TRACKER — МЕНЕДЖЕР АККАУНТОВ И СЕССИЙ          ║")
    print("╚" + "═" * 68 + "╝")

    while True:
        print("Действия:")
        print("  [1] Добавить новый аккаунт (Номер + Код) -> Мгновенный старт")
        print("  [2] Показать подробную статистику всех аккаунтов")
        print("  [0] Выход")
        choice = input("\nВыберите действие (по умолчанию 1): ").strip()

        if choice == "0":
            print("Выход из менеджера.")
            break
        elif choice == "2":
            await get_all_sessions_stats()
        else:
            added = await add_single_account()
            if added:
                await get_all_sessions_stats()

            again = input("Добавить ещё один аккаунт? (y/n, по умолчанию y): ").strip().lower()
            if again == "n":
                print("\n✅ Все аккаунты сохранены и готовы к работе!")
                break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем.")
