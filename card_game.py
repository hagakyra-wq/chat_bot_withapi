"""Server-side rules for a multiplayer game of Durak."""

from __future__ import annotations

import random
from typing import Any

RANKS = (6, 7, 8, 9, 10, 11, 12, 13, 14)
SUITS = ("clubs", "diamonds", "hearts", "spades")
MAX_ROUNDS = 120


def create_game(players: list[dict[str, Any]]) -> dict[str, Any]:
    if not 2 <= len(players) <= 4:
        raise ValueError("Для игры нужно от двух до четырёх игроков.")
    deck = [
        {"rank": rank, "suit": suit}
        for suit in SUITS
        for rank in RANKS
    ]
    random.SystemRandom().shuffle(deck)
    trump = deck.pop()
    game_players = [
        {**player, "hand": [], "forfeited": False}
        for player in players
    ]
    for _ in range(6):
        for player in game_players:
            player["hand"].append(deck.pop())
    return {
        "players": game_players,
        "deck": deck,
        "trump": trump,
        "trump_suit": trump["suit"],
        "discard": [],
        "attacker_index": 0,
        "defender_index": 1,
        "stage": "attack",
        "table": [],
        "defender_limit": 6,
        "status": "playing",
        "rounds_completed": 0,
        "winners": [],
        "losers": [],
        "loser_id": None,
        "finish_reason": None,
        "finish_order": [],
    }


def _player_index(game: dict[str, Any], user_id: int) -> int:
    for index, player in enumerate(game["players"]):
        if int(player["user_id"]) == user_id:
            if player.get("forfeited"):
                raise ValueError("Ты покинул эту партию.")
            return index
    raise ValueError("Игрок не найден в этой партии.")


def _card_in_hand(player: dict[str, Any], card: dict[str, Any]) -> int:
    for index, item in enumerate(player["hand"]):
        if item == card:
            return index
    raise ValueError("Этой карты нет у тебя на руках.")


def _beats(card: dict[str, Any], attack: dict[str, Any], trump_suit: str) -> bool:
    if card["suit"] == attack["suit"]:
        return int(card["rank"]) > int(attack["rank"])
    return card["suit"] == trump_suit and attack["suit"] != trump_suit


def _draw_cards(game: dict[str, Any], player: dict[str, Any]) -> None:
    if player.get("forfeited"):
        return
    while len(player["hand"]) < 6:
        if game["deck"]:
            player["hand"].append(game["deck"].pop())
        elif game["trump"] is not None:
            player["hand"].append(game["trump"])
            game["trump"] = None
        else:
            break


def _finish_if_empty_deck(game: dict[str, Any]) -> bool:
    if game["deck"] or game["trump"] is not None:
        return False
    active = [
        player for player in game["players"]
        if not player.get("forfeited") and player["hand"]
    ]
    finish_order = game.setdefault("finish_order", [])
    finish_order.extend(
        int(player["user_id"])
        for player in game["players"]
        if not player.get("forfeited")
        and not player["hand"]
        and int(player["user_id"]) not in finish_order
    )
    if len(active) > 1:
        return False
    game["status"] = "finished"
    game["stage"] = "finished"
    game["loser_id"] = int(active[0]["user_id"]) if active else None
    game["losers"] = [game["loser_id"]] if game["loser_id"] is not None else []
    if game["loser_id"] is not None:
        finish_order.append(game["loser_id"])
    game["winners"] = [
        int(player["user_id"])
        for player in game["players"]
        if not player.get("forfeited")
        and (not active or int(player["user_id"]) != game["loser_id"])
    ]
    forfeited = [
        int(player["user_id"])
        for player in game["players"]
        if player.get("forfeited")
    ]
    game["placement_order"] = (
        game["winners"]
        + ([game["loser_id"]] if game["loser_id"] is not None else [])
        + list(reversed(forfeited))
    )
    game["finish_reason"] = "last_card"
    return True


def _finish_at_round_limit(game: dict[str, Any]) -> None:
    active = [player for player in game["players"] if not player.get("forfeited")]
    maximum_cards = max(len(player["hand"]) for player in active)
    game["losers"] = [
        int(player["user_id"])
        for player in active
        if len(player["hand"]) == maximum_cards
    ]
    game["winners"] = [
        int(player["user_id"])
        for player in sorted(active, key=lambda item: len(item["hand"]))
        if int(player["user_id"]) not in game["losers"]
    ]
    game["loser_id"] = game["losers"][0] if len(game["losers"]) == 1 else None
    finish_order = game.setdefault("finish_order", [])
    finish_order.extend(
        player_id
        for player_id in game["winners"] + game["losers"]
        if player_id not in finish_order
    )
    game["placement_order"] = game["winners"] + game["losers"] + [
        int(player["user_id"])
        for player in reversed(game["players"])
        if player.get("forfeited")
    ]
    game["status"] = "finished"
    game["stage"] = "finished"
    game["finish_reason"] = "round_limit"


def _next_active_index(game: dict[str, Any], start: int) -> int:
    players = game["players"]
    for offset in range(len(players)):
        index = (start + offset) % len(players)
        if (
            not players[index].get("forfeited")
            and (players[index]["hand"] or game["deck"] or game["trump"] is not None)
        ):
            return index
    return start


def _resolve_round(game: dict[str, Any], *, defender_takes: bool) -> None:
    players = game["players"]
    old_attacker = game["attacker_index"]
    old_defender = game["defender_index"]
    if defender_takes:
        players[old_defender]["hand"].extend(
            entry["attack"] for entry in game["table"]
        )
        players[old_defender]["hand"].extend(
            entry["defense"]
            for entry in game["table"]
            if entry["defense"] is not None
        )
        next_attacker = (old_defender + 1) % len(players)
    else:
        game["discard"].extend(
            card
            for entry in game["table"]
            for card in (entry["attack"], entry["defense"])
            if card is not None
        )
        next_attacker = old_defender

    for offset in range(len(players)):
        _draw_cards(game, players[(old_attacker + offset) % len(players)])
    game["table"] = []
    game["attacker_index"] = _next_active_index(game, next_attacker)
    game["defender_index"] = _next_active_index(
        game,
        (game["attacker_index"] + 1) % len(players),
    )
    game["defender_limit"] = len(players[game["defender_index"]]["hand"])
    game["stage"] = "attack"
    game["rounds_completed"] += 1
    if not _finish_if_empty_deck(game) and game["rounds_completed"] >= MAX_ROUNDS:
        _finish_at_round_limit(game)


def apply_action(
    game: dict[str, Any],
    user_id: int,
    action: str,
    *,
    card: dict[str, Any] | None = None,
    target: int | None = None,
) -> None:
    if game["status"] != "playing":
        raise ValueError("Эта партия уже завершена.")
    player_index = _player_index(game, user_id)
    attacker_index = int(game["attacker_index"])
    defender_index = int(game["defender_index"])
    player = game["players"][player_index]

    if action == "play":
        if game["stage"] not in ("attack", "attack_more"):
            raise ValueError("Сейчас не твой ход атаки.")
        if (
            (game["stage"] == "attack" and player_index != attacker_index)
            or (game["stage"] == "attack_more" and player_index == defender_index)
        ):
            raise ValueError("Сейчас не твой ход атаки.")
        if card is None:
            raise ValueError("Выбери карту для атаки.")
        hand_index = _card_in_hand(player, card)
        if game["stage"] == "attack_more":
            table_ranks = {
                int(existing["rank"])
                for entry in game["table"]
                for existing in (entry["attack"], entry["defense"])
                if existing is not None
            }
            if int(card["rank"]) not in table_ranks:
                raise ValueError(
                    "Подкинуть можно только карту достоинства, уже лежащего на столе."
                )
            if len(game["table"]) >= int(game["defender_limit"]):
                raise ValueError("У защищающегося недостаточно карт для новой атаки.")
        player["hand"].pop(hand_index)
        game["table"].append({"attack": card, "defense": None})
        game["stage"] = "defend"
        return

    if action == "defend":
        if player_index != defender_index or game["stage"] != "defend":
            raise ValueError("Сейчас нельзя отбивать карту.")
        if card is None or target is None or not 0 <= target < len(game["table"]):
            raise ValueError("Выбери карту для отбоя и карту на столе.")
        entry = game["table"][target]
        if entry["defense"] is not None:
            raise ValueError("Эта карта уже отбита.")
        if not _beats(card, entry["attack"], game["trump_suit"]):
            raise ValueError("Эта карта не может отбить выбранную.")
        player["hand"].pop(_card_in_hand(player, card))
        entry["defense"] = card
        if all(item["defense"] is not None for item in game["table"]):
            game["stage"] = "attack_more"
        return

    if action == "take":
        if player_index != defender_index or game["stage"] not in ("defend", "attack_more"):
            raise ValueError("Сейчас нельзя забрать карты.")
        _resolve_round(game, defender_takes=True)
        return

    if action == "finish":
        if (
            player_index != defender_index
            or game["stage"] != "attack_more"
            or not all(item["defense"] is not None for item in game["table"])
        ):
            raise ValueError("Сначала отбей все карты на столе.")
        _resolve_round(game, defender_takes=False)
        return

    raise ValueError("Неизвестное действие.")


def forfeit_player(game: dict[str, Any], user_id: int) -> None:
    if game["status"] != "playing":
        raise ValueError("Эта партия уже завершена.")
    index = _player_index(game, user_id)
    player = game["players"][index]
    player["forfeited"] = True
    player["hand"] = []
    game.setdefault("finish_order", []).append(user_id)

    active = [item for item in game["players"] if not item.get("forfeited")]
    if len(active) <= 1:
        game["status"] = "finished"
        game["stage"] = "finished"
        game["finish_reason"] = "forfeit"
        game["winners"] = [int(item["user_id"]) for item in active]
        game["losers"] = [
            int(item["user_id"])
            for item in game["players"]
            if item.get("forfeited")
        ]
        game["loser_id"] = user_id if len(game["losers"]) == 1 else None
        game["finish_order"].extend(
            player_id
            for player_id in game["losers"] + game["winners"]
            if player_id not in game["finish_order"]
        )
        game["placement_order"] = game["winners"] + [
            player_id
            for player_id in reversed(game["finish_order"])
            if player_id not in game["winners"]
        ]
        return

    game["discard"].extend(
        card
        for entry in game["table"]
        for card in (entry["attack"], entry["defense"])
        if card is not None
    )
    game["table"] = []
    next_attacker = _next_active_index(game, index + 1)
    game["attacker_index"] = next_attacker
    game["defender_index"] = _next_active_index(game, next_attacker + 1)
    game["defender_limit"] = len(game["players"][game["defender_index"]]["hand"])
    game["stage"] = "attack"
    for offset in range(len(game["players"])):
        _draw_cards(game, game["players"][(index + 1 + offset) % len(game["players"])])
