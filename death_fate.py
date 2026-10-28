"""Лёгкие команды про дату и причину смерти без таймеров и озвучки."""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from pathlib import Path

_DEATH_CAUSES_PATH = Path(__file__).with_name("1000.txt")
_DEATH_REASON_RE = re.compile(
    r"^(?:причина\s+смерти|какая\s+причина\s+смерти|какая\s+смерть)$",
    re.IGNORECASE,
)
_DEATH_TIME_RE = re.compile(
    r"^(?:сколько\s+мне\s+осталось|сколько\s+осталось|сколько\s+до\s+смерти|когда\s+я\s+умру)$",
    re.IGNORECASE,
)


def _load_death_causes() -> list[str]:
    if not _DEATH_CAUSES_PATH.exists():
        return ["неизвестная причина смерти"]
    causes: list[str] = []
    for line in _DEATH_CAUSES_PATH.read_text("utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
        if cleaned:
            causes.append(cleaned)
    return causes or ["неизвестная причина смерти"]


def random_death_cause() -> str:
    causes = _load_death_causes()
    return random.choice(causes)


def random_death_deadline(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    future_seconds = random.randint(1, 365 * 5 * 24 * 60 * 60)
    return now + timedelta(seconds=future_seconds)


def format_time_left(deadline: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    remaining = max(deadline - now, timedelta(0))
    total_seconds = int(remaining.total_seconds())
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"{days} дн., {hours} ч., {minutes} мин., {seconds} сек."


def is_death_reason_request(text: str | None) -> bool:
    if text is None:
        return False
    normalized = text.strip().rstrip("?!.")
    return _DEATH_REASON_RE.fullmatch(normalized) is not None


def is_death_time_request(text: str | None) -> bool:
    if text is None:
        return False
    normalized = text.strip().rstrip("?!.")
    return _DEATH_TIME_RE.fullmatch(normalized) is not None


def get_death_time_reply() -> str:
    deadline = random_death_deadline()
    return (
        f"До смерти осталось: {format_time_left(deadline)}\n"
        f"Дата смерти: {deadline.strftime('%d.%m.%Y %H:%M')}"
    )
