"""Tarot card names and randomized three-card spreads."""

from __future__ import annotations

import secrets
from typing import Any

_MAJOR_ARCANA = (
    ("Шут", "00-TheFool.jpg"),
    ("Маг", "01-TheMagician.jpg"),
    ("Верховная Жрица", "02-TheHighPriestess.jpg"),
    ("Императрица", "03-TheEmpress.jpg"),
    ("Император", "04-TheEmperor.jpg"),
    ("Иерофант", "05-TheHierophant.jpg"),
    ("Влюблённые", "06-TheLovers.jpg"),
    ("Колесница", "07-TheChariot.jpg"),
    ("Сила", "08-Strength.jpg"),
    ("Отшельник", "09-TheHermit.jpg"),
    ("Колесо Фортуны", "10-WheelOfFortune.jpg"),
    ("Справедливость", "11-Justice.jpg"),
    ("Повешенный", "12-TheHangedMan.jpg"),
    ("Смерть", "13-Death.jpg"),
    ("Умеренность", "14-Temperance.jpg"),
    ("Дьявол", "15-TheDevil.jpg"),
    ("Башня", "16-TheTower.jpg"),
    ("Звезда", "17-TheStar.jpg"),
    ("Луна", "18-TheMoon.jpg"),
    ("Солнце", "19-TheSun.jpg"),
    ("Суд", "20-Judgement.jpg"),
    ("Мир", "21-TheWorld.jpg"),
)
_RANKS = (
    ("Туз", "A"), ("Двойка", "2"), ("Тройка", "3"), ("Четвёрка", "4"),
    ("Пятёрка", "5"), ("Шестёрка", "6"), ("Семёрка", "7"), ("Восьмёрка", "8"),
    ("Девятка", "9"), ("Десятка", "10"), ("Паж", "P"), ("Рыцарь", "N"),
    ("Королева", "Q"), ("Король", "K"),
)
_SUITS = (
    ("Жезлов", "♣", "Wands"),
    ("Кубков", "♥", "Cups"),
    ("Мечей", "♠", "Swords"),
    ("Пентаклей", "♦", "Pentacles"),
)
_POSITIONS = ("Прошлое", "Настоящее", "Возможное направление")


def draw_spread() -> list[dict[str, Any]]:
    deck: list[dict[str, Any]] = []
    for index, (name, image_file) in enumerate(_MAJOR_ARCANA):
        deck.append(
            {
                "name": name,
                "arcana": "Старший аркан",
                "symbol": ("☾", "✦", "☼", "◇", "♙")[index % 5],
                "image_file": image_file,
            }
        )
    for suit, symbol, image_suit in _SUITS:
        for rank_index, (rank, mark) in enumerate(_RANKS, start=1):
            deck.append(
                {
                    "name": f"{rank} {suit}",
                    "arcana": "Младший аркан",
                    "symbol": symbol if mark in ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10") else mark,
                    "image_file": f"{image_suit}{rank_index:02}.jpg",
                }
            )
    cards = secrets.SystemRandom().sample(deck, 3)
    return [
        {**card, "position": position, "reversed": secrets.randbelow(2) == 1}
        for card, position in zip(cards, _POSITIONS, strict=True)
    ]
