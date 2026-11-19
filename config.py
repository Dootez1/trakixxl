import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Group / Forum settings
TELEGRAM_GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID", "0"))

# Topic thread IDs (0 if single chat without topics)
TOPIC_CHEAP_ID = int(os.getenv("TOPIC_CHEAP_ID", "0"))
TOPIC_MEDIUM_ID = int(os.getenv("TOPIC_MEDIUM_ID", "0"))
TOPIC_EXPENSIVE_ID = int(os.getenv("TOPIC_EXPENSIVE_ID", "0"))
TOPIC_GIRLS_ID = int(os.getenv("TOPIC_GIRLS_ID", "0"))
TOPIC_UPGRADED_ID = int(os.getenv("TOPIC_UPGRADED_ID", "0"))

# Cooldown
CLAIM_COOLDOWN_SECONDS = int(os.getenv("CLAIM_COOLDOWN_SECONDS", "5"))
CLAIM_ACTION = os.getenv("CLAIM_ACTION", "delete").lower()  # "delete" or "edit"

# Telethon client settings
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "2040")) if os.getenv("TELEGRAM_API_ID") else 2040
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627") if os.getenv("TELEGRAM_API_HASH") else "b18441a1ff607e10a989891a5462e627"
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "session_checker")
TELEGRAM_SESSIONS = os.getenv("TELEGRAM_SESSIONS", "")

# Database settings
DB_PATH = BASE_DIR / "treker.db"
