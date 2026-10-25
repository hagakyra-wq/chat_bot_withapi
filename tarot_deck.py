"""Tarot card names and randomized three-card spreads."""

from __future__ import annotations

import secrets
from typing import Any

_MAJOR_ARCANA = (
    "Шут", "Маг", "Верховная Жрица", "Императрица", "Император",
    "Иерофант", "Влюблённые", "Колесница", "Сила", "Отшельник",
    "Колесо Фортуны", "Справедливость", "Повешенный", "Смерть",
    "Умеренность", "Дьявол", "Башня", "Звезда", "Луна", "Солнце",
    "Суд", "Мир",
)
_RANKS = (
    ("Туз", "A"), ("Двойка", "2"), ("Тройка", "3"), ("Четвёрка", "4"),
    ("Пятёрка", "5"), ("Шестёрка", "6"), ("Семёрка", "7"), ("Восьмёрка", "8"),
    ("Девятка", "9"), ("Десятка", "10"), ("Паж", "P"), ("Рыцарь", "N"),
    ("Королева", "Q"), ("Король", "K"),
)
_SUITS = (
    ("Жезлов", "♣"), ("Кубков", "♥"), ("Мечей", "♠"), ("Пентаклей", "♦"),
)
_POSITIONS = ("Прошлое", "Настоящее", "Возможное направление")


def draw_spread() -> list[dict[str, Any]]:
    deck: list[dict[str, Any]] = []
    for index, name in enumerate(_MAJOR_ARCANA):
        deck.append(
            {
                "name": name,
                "arcana": "Старший аркан",
                "symbol": ("☾", "✦", "☼", "◇", "♙")[index % 5],
            }
        )
    for suit, symbol in _SUITS:
        for rank, mark in _RANKS:
            deck.append(
                {
                    "name": f"{rank} {suit}",
                    "arcana": "Младший аркан",
                    "symbol": symbol if mark in ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10") else mark,
                }
            )
    cards = secrets.SystemRandom().sample(deck, 3)
    return [
        {**card, "position": position, "reversed": secrets.randbelow(2) == 1}
        for card, position in zip(cards, _POSITIONS, strict=True)
    ]
