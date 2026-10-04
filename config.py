"""Переменные окружения и общие настройки бота."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _required_text(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(
            f"Не задана обязательная переменная окружения {name}. "
            "Добавьте её в Environment сервиса Render или в локальный .env."
        )
    return value


def _required_int(name: str) -> int:
    value = _required_text(name)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Переменная окружения {name} должна быть целым числом.") from exc


TELEGRAM_TOKEN = _required_text("TELEGRAM_TOKEN")
GROQ_API_KEY = _required_text("GROQ_API_KEY")
ADMIN_ID = _required_int("ADMIN_ID")
BACKUP_CHANNEL_ID = _required_int("BACKUP_CHANNEL_ID")

try:
    PORT = int(os.getenv("PORT", "8080"))
except ValueError as exc:
    raise ValueError("Переменная окружения PORT должна быть целым числом.") from exc

if not 1 <= PORT <= 65535:
    raise ValueError("Переменная окружения PORT должна быть в диапазоне 1–65535.")

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "database.db"
DATABASE_BACKUP_PATH = BASE_DIR / "database_backup.db"
DATABASE_ARCHIVE_DIR = BASE_DIR / "database_archives"
DATABASE_SHARD_THRESHOLD_BYTES = 19 * 1024 * 1024
MODEL_NAME = "openai/gpt-oss-120b"

DEFAULT_CONTEXT_SIZE = 20
CONTEXT_CHAR_LIMIT = 80_000
SUMMARY_CONTEXT_CHAR_LIMIT = 120_000
DICE_ANIMATION_DELAY_SECONDS = 4
REACTION_PROBABILITY = 0.25
MAX_TOTAL_MESSAGES_PER_CHAT = 500
TRIGGERS = ("юбара", "yubara")
TRIGGER_THRESHOLD = 0.72
BACKUP_INTERVAL_SECONDS = 10 * 60
BACKUP_MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024
UNKNOWN_GROUP_NOTICE_SECONDS = 10 * 60

AVAILABLE_REACTIONS = (
    "👍", "👎", "❤️", "🔥", "😁", "🤔", "🤯", "😱", "😢", "😭",
    "🎉", "🤩", "👏", "👌", "🗿", "💔", "⚡", "👀", "🫡",
)

SUMMARY_TRIGGERS = (
    "что я пропустил",
    "что пропустил",
    "что тут было",
    "краткое содержание",
    "перескажи",
    "что обсудили",
    "о чем общались",
    "о чём общались",
    "о чем говорили",
    "о чём говорили",
    "вкратце",
)

KNOWN_PEOPLE_TRIGGERS = (
    "что ты о нас знаешь",
    "что знаешь о нас",
    "что ты знаешь о нас",
    "что ты про нас знаешь",
    "что ты знаешь о группе",
    "что знаешь о группе",
    "что ты знаешь про группу",
    "расскажи о нашей группе",
    "расскажи о нас",
    "профиль группы",
    "профиль чата",
    "что ты знаешь об участниках",
    "что знаешь об участниках",
    "что ты знаешь про участников",
)

PROFILE_TRIGGERS = (
    "что ты знаешь обо мне",
    "что знаешь обо мне",
    "что ты обо мне знаешь",
    "что обо мне знаешь",
    "что ты знаешь про меня",
    "что знаешь про меня",
    "что ты помнишь обо мне",
    "расскажи обо мне",
    "мой профиль",
    "профиль обо мне",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
