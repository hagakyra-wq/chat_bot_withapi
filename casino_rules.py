"""Shared result rules for Telegram casino spins."""

import secrets
from typing import Any

from yubari_banner import make_digest, pick_preset

RARITY_NAMES = {
    1: "Обычный",
    2: "Необычный",
    3: "Редкий",
    4: "Эпический",
    5: "Элитный",
    6: "Легендарный",
}

# Equivalent counts across 216 dice outcomes, including the existing 30/70 rolls.
RARITY_WEIGHTS = {1: 84, 2: 36, 3: 63, 4: 27, 5: 5, 6: 1}


def get_result_level(values: tuple[int, int, int]) -> int:
    if len(values) != 3 or any(value not in range(1, 7) for value in values):
        raise ValueError("A casino result needs three six-sided dice values.")
    if values == (6, 6, 6):
        return 6
    if len(set(values)) == 1:
        return 5
    if len(set(values)) == 2:
        return 4 if secrets.randbelow(10) < 3 else 3
    return 2 if secrets.randbelow(10) < 3 else 1


def generate_result(user_id: int, values: tuple[int, int, int]) -> dict[str, Any]:
    level = get_result_level(values)
    seed = secrets.token_hex(16)
    preset = (
        pick_preset(make_digest(user_id, item_seed=seed))
        if level == 6
        else None
    )
    return {
        "values": values,
        "level": level,
        "seed": seed,
        "preset": preset,
    }
