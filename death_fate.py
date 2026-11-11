"""Шутливые функции про смерть и оставшееся время."""

import hashlib
import re
from datetime import datetime, timedelta, timezone

_DEATH_REASONS = (
    "от недосыпа и слишком честной жизни",
    "от удара тостером в ранний понедельник",
    "от долгой беседы с безмолвным кодом",
    "от переедания печенья в момент дедлайна",
    "от слишком красивого заката и отсутствия зонта",
    "от бесконечной переписки с друзьями в 3 часа ночи",
    "от удара по самолюбию от одного левого комментария",
    "от слишком активного дня без кофе",
    "от исчезновения всех сил после последнего коммита",
    "от любопытства и ещё одного 'только один вопрос'",
)

_DEATH_REASON_RE = re.compile(
    r"(?:причин[а-я]*\s+смерт[иы]|от\s+чег[оа]\s+я\s+умру|какая\s+причина\s+смерти|что\s+меня\s+убьёт|кто\s+меня\s+убьёт|почему\s+я\s+умру|смерть\s+от)"
    , re.IGNORECASE,
)
_DEATH_TIME_RE = re.compile(
    r"(?:скольк[оо]\s+мне\s+остал(?:о|ось)|скольк[оо]\s+остал(?:о|ось)|когда\s+я\s+умру|когда\s+умру|когда\s+мне\s+умереть|до\s+смерт[иы]|смерть\s+через|осталось\s+до\s+смерт[иы])",
    re.IGNORECASE,
)

_DEATH_DEADLINES: dict[tuple[int, int], datetime] = {}


def _stable_seed(*parts: object) -> int:
    payload = ":".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def random_death_cause(user_id: int, chat_id: int = 0) -> str:
    seed = _stable_seed("death_cause", user_id, chat_id)
    return _DEATH_REASONS[seed % len(_DEATH_REASONS)]


def is_death_reason_request(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return bool(_DEATH_REASON_RE.search(normalized))


def is_death_time_request(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return bool(_DEATH_TIME_RE.search(normalized))


def format_time_left(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "почти мгновенно"
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if not parts or seconds and not days and not hours:
        parts.append(f"{seconds} сек")
    return " ".join(parts) if parts else "меньше секунды"


def _death_deadline(chat_id: int, user_id: int) -> datetime:
    key = (chat_id, user_id)
    if key not in _DEATH_DEADLINES:
        seed = _stable_seed("death_deadline", chat_id, user_id)
        days = 7 + (seed % 83)
        hours = (seed >> 8) % 24
        minutes = (seed >> 16) % 60
        _DEATH_DEADLINES[key] = datetime.now(timezone.utc) + timedelta(
            days=days,
            hours=hours,
            minutes=minutes,
        )
    return _DEATH_DEADLINES[key]


def get_death_time_reply(chat_id: int, user_id: int) -> str:
    deadline = _death_deadline(chat_id, user_id)
    remaining = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
    if remaining == 0:
        return "Тебе осталось совсем немного — кажется, смерть уже в дверях."
    return (
        f"До смерти осталось: {format_time_left(remaining)}. "
        f"По предварительной сводке, тебя настигнет {random_death_cause(user_id, chat_id)}."
    )
