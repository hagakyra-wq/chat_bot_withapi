"""Mini App room games and daily Tarot readings."""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime
from typing import Any

from telegram.error import TelegramError

import card_game
import database
import llm
from config import MOSCOW_TIMEZONE
from tarot_deck import draw_spread

_ROOM_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{6}$")
_USERNAME_RE = re.compile(r"@?([a-z0-9_]{5,32})", re.IGNORECASE)
_ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _public_room(room: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    state = room["state"]
    game = state.get("game")
    players = game["players"] if game else state["players"]
    own_player = next(
        (player for player in players if int(player["user_id"]) == user_id),
        None,
    )
    if own_player is None or own_player.get("forfeited"):
        return None

    public_players = [
        {
            "user_id": int(player["user_id"]),
            "display_name": str(player["display_name"]),
            "username": player.get("username"),
            "photo_url": player.get("photo_url"),
            "cards_count": len(player["hand"]) if game else 0,
            "has_left": bool(player.get("forfeited")),
        }
        for player in players
    ]
    expires_at = state.get("expires_at")
    response: dict[str, Any] = {
        "room_code": room["room_code"],
        "owner_id": room["owner_id"],
        "max_players": room["max_players"],
        "status": room["status"],
        "version": room["version"],
        "game_type": state.get("game_type", "durak"),
        "mode": state.get("mode", "free"),
        "stake_cents": int(state.get("stake_cents", 0)),
        "expires_at": expires_at,
        "remaining_seconds": max(0, int(expires_at - time.time())) if expires_at else None,
        "players": public_players,
        "is_owner": room["owner_id"] == user_id,
        "finish_reason": state.get("finish_reason"),
        "settlement_payouts": state.get("settlement_payouts", []),
    }
    if game:
        attacker_id = int(game["players"][game["attacker_index"]]["user_id"])
        defender_id = int(game["players"][game["defender_index"]]["user_id"])
        actor_id = (
            attacker_id if game["stage"] in ("attack", "attack_more")
            else defender_id
        )
        own_player = next(player for player in game["players"] if int(player["user_id"]) == user_id)
        response["game"] = {
            "stage": game["stage"],
            "your_hand": own_player["hand"],
            "trump": game["trump"],
            "trump_suit": game["trump_suit"],
            "deck_count": len(game["deck"]) + (game["trump"] is not None),
            "attacker_id": int(game["players"][game["attacker_index"]]["user_id"]),
            "defender_id": int(game["players"][game["defender_index"]]["user_id"]),
            "current_actor_id": actor_id,
            "is_your_turn": (
                user_id == actor_id
                or (game["stage"] == "attack_more" and user_id != defender_id)
            ),
            "can_attack": (
                user_id == attacker_id
                if game["stage"] == "attack"
                else game["stage"] == "attack_more" and user_id != defender_id
            ),
            "table": game["table"],
            "winners": game["winners"],
            "losers": game["losers"],
            "loser_id": game["loser_id"],
            "finish_reason": game["finish_reason"],
            "finish_order": game.get("finish_order", []),
            "placement_order": game.get("placement_order", []),
            "can_finish": (
                user_id == int(game["players"][game["defender_index"]]["user_id"])
                and game["stage"] == "attack_more"
            ),
            "can_take": (
                user_id == int(game["players"][game["defender_index"]]["user_id"])
                and game["stage"] in ("defend", "attack_more")
            ),
        }
    return response


async def list_rooms(user_id: int) -> tuple[int, dict[str, Any]]:
    rooms = await database.list_minigame_rooms(user_id)
    for room in rooms:
        await _expire_room_if_needed(room)
    rooms = await database.list_minigame_rooms(user_id)
    return 200, {
        "rooms": [
            public_room
            for room in rooms
            if (public_room := _public_room(room, user_id)) is not None
        ]
    }


async def get_room(room_code: str, user_id: int) -> tuple[int, dict[str, Any]]:
    normalized = room_code.strip().upper()
    if not _ROOM_CODE_RE.fullmatch(normalized):
        return 400, {"error": "Код комнаты должен состоять из шести символов."}
    room = await database.get_minigame_room(normalized)
    if room is None:
        return 404, {"error": "Комната не найдена."}
    await _expire_room_if_needed(room)
    room = await database.get_minigame_room(normalized)
    if room is None:
        return 404, {"error": "Комната не найдена."}
    response = _public_room(room, user_id)
    if response is None:
        return 403, {"error": "Эта комната доступна только её участникам."}
    response["chat"] = await database.list_minigame_room_messages(normalized)
    if response["mode"] == "coins":
        response["balance_cents"] = await database.get_duel_balance(user_id, None)
    return 200, response


def _valid_photo_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    if not value.startswith(("https://t.me/", "https://telegram.org/")):
        return None
    return value


async def create_room(
    user_id: int,
    first_name: str,
    username: str | None,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    max_players = payload.get("max_players")
    if type(max_players) is not int or max_players not in (2, 3, 4):
        return 400, {"error": "Выбери комнату на двух, трёх или четырёх игроков."}
    game_type = payload.get("game_type", "durak")
    if game_type != "durak":
        return 409, {"error": "Эта игра скоро появится. Сейчас доступен только подкидной дурак."}
    mode = payload.get("mode", "free")
    stake_cents = payload.get("stake_cents", 0)
    if mode not in ("free", "coins"):
        return 400, {"error": "Выбери бесплатный режим или игру на монеты."}
    if type(stake_cents) is not int or stake_cents < 0 or stake_cents > 1_000_000_000_000:
        return 400, {"error": "Укажи корректную сумму ставки."}
    if mode == "free" and stake_cents != 0:
        return 400, {"error": "В бесплатной комнате ставка должна быть нулевой."}
    if mode == "coins" and stake_cents < 1:
        return 400, {"error": "Для игры на монеты укажи ставку больше нуля."}
    if mode == "coins":
        await database.get_duel_balance(user_id, username)
    state = {
        "game_type": game_type,
        "mode": mode,
        "stake_cents": stake_cents,
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + 3600,
        "stakes_escrowed": False,
        "stakes_settled": False,
        "players": [
            {
                "user_id": user_id,
                "display_name": first_name.strip()[:80] or f"Игрок {user_id}",
                "username": username,
                "photo_url": _valid_photo_url(payload.get("photo_url")),
                "hand": [],
            }
        ],
        "game": None,
    }
    for _ in range(5):
        code = "".join(secrets.choice(_ROOM_CODE_ALPHABET) for _ in range(6))
        if await database.create_minigame_room(code, user_id, max_players, state):
            room = await database.get_minigame_room(code)
            if room is None:
                raise RuntimeError("Созданная комната не найдена в базе данных.")
            response = _public_room(room, user_id)
            if response is None:
                raise RuntimeError("Создатель пропал из только что созданной комнаты.")
            if mode == "coins":
                response["balance_cents"] = await database.get_duel_balance(user_id, username)
            return 201, response
    raise RuntimeError("Не удалось подобрать свободный код игровой комнаты.")


async def join_room(
    user_id: int,
    first_name: str,
    username: str | None,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    code = payload.get("room_code")
    if not isinstance(code, str) or not _ROOM_CODE_RE.fullmatch(code.strip().upper()):
        return 400, {"error": "Введи корректный шестизначный код комнаты."}
    normalized = code.strip().upper()
    room = await database.get_minigame_room(normalized)
    if room is not None:
        await _expire_room_if_needed(room)
    room = await database.get_minigame_room(normalized)
    if room is None:
        return 409, {"error": "Комната не найдена. Проверь код и попробуй ещё раз."}
    if room["state"].get("mode") == "coins":
        balance = await database.get_duel_balance(user_id, username)
        if balance < int(room["state"].get("stake_cents", 0)):
            return 409, {"error": "Недостаточно монет для входа в эту комнату."}
    result, joined = await database.join_minigame_room(
        normalized,
        user_id,
        first_name.strip()[:80] or f"Игрок {user_id}",
        username,
        photo_url=_valid_photo_url(payload.get("photo_url")),
    )
    if result not in ("joined", "already_joined") or joined is None:
        messages = {
            "missing": "Комната не найдена. Проверь код и попробуй ещё раз.",
            "started": "Игра уже началась, присоединиться нельзя.",
            "full": "В комнате больше нет свободных мест.",
        }
        return 409, {"error": messages.get(result, "Не удалось войти в комнату.")}
    room = await database.get_minigame_room(normalized)
    if room is None:
        raise RuntimeError("Комната исчезла после успешного входа.")
    response = _public_room(room, user_id)
    if response is None:
        raise RuntimeError("Игрок не найден в комнате после успешного входа.")
    return 200, response


async def start_room(room_code: str, user_id: int) -> tuple[int, dict[str, Any]]:
    room = await database.get_minigame_room(room_code.strip().upper())
    if room is None:
        return 404, {"error": "Комната не найдена."}
    await _expire_room_if_needed(room)
    room = await database.get_minigame_room(room_code.strip().upper())
    if room is None:
        return 404, {"error": "Комната не найдена."}
    if room["owner_id"] != user_id:
        return 403, {"error": "Начать игру может только создатель комнаты."}
    if room["status"] != "lobby":
        return 409, {"error": "Эта партия уже запущена."}
    if len(room["state"]["players"]) != room["max_players"]:
        return 409, {"error": "Подожди, пока все места в комнате будут заняты."}
    room["state"]["game"] = card_game.create_game(room["state"]["players"])
    stake_cents = int(room["state"].get("stake_cents", 0))
    if stake_cents:
        for player in room["state"]["players"]:
            await database.get_duel_balance(int(player["user_id"]), player.get("username"))
    try:
        updated_ok = await database.update_minigame_room(
            room["room_code"],
            user_id,
            room["version"],
            "playing",
            room["state"],
            stake_cents=stake_cents,
        )
    except database.InsufficientMinigameBalanceError:
        return 409, {"error": "У одного из игроков недостаточно монет. Уменьшите ставку."}
    if not updated_ok:
        return 409, {"error": "Комната изменилась. Обнови её и попробуй снова."}
    updated = await database.get_minigame_room(room["room_code"])
    if updated is None:
        raise RuntimeError("Запущенная партия не найдена в базе данных.")
    response = _public_room(updated, user_id)
    if response is None:
        raise RuntimeError("Создатель не найден в запущенной партии.")
    return 200, response


def _ranked_player_ids(game: dict[str, Any]) -> list[int]:
    ranking: list[int] = []
    order = game.get("placement_order") or (
        game.get("finish_order", [])
        + game.get("winners", [])
        + game.get("losers", [])
    )
    for user_id in order:
        normalized = int(user_id)
        if normalized not in ranking:
            ranking.append(normalized)
    for player in game["players"]:
        user_id = int(player["user_id"])
        if user_id not in ranking:
            ranking.append(user_id)
    loser_id = game.get("loser_id")
    if loser_id in ranking:
        ranking.remove(loser_id)
        ranking.append(loser_id)
    return ranking


def _room_payouts(room: dict[str, Any], *, timeout: bool = False) -> list[dict[str, int]]:
    state = room["state"]
    stake_cents = int(state.get("stake_cents", 0))
    players = state.get("players", [])
    if not stake_cents:
        return []
    if timeout:
        return [
            {
                "user_id": int(player["user_id"]),
                "delta_cents": stake_cents * 95 // 100,
            }
            for player in players
        ]

    game = state["game"]
    ranked = _ranked_player_ids(game)
    gross_pool = stake_cents * len(players)
    net_pool = gross_pool - gross_pool * 5 // 100
    winners = ranked[:2]
    amounts = {user_id: 0 for user_id in ranked}
    if len(players) == 2:
        amounts[winners[0]] = net_pool
    elif len(players) == 3:
        first_prize = net_pool * 70 // 100
        if winners:
            amounts[winners[0]] = first_prize
        if len(winners) > 1:
            amounts[winners[1]] = net_pool - first_prize
    else:
        first_prize = net_pool * 2 // 3
        if winners:
            amounts[winners[0]] = first_prize
        if len(winners) > 1:
            amounts[winners[1]] = net_pool - first_prize
    return [
        {"user_id": int(player["user_id"]), "delta_cents": amounts[int(player["user_id"])]}
        for player in players
    ]


async def _expire_room_if_needed(room: dict[str, Any]) -> None:
    expires_at = room["state"].get("expires_at")
    if not isinstance(expires_at, (int, float)) or time.time() < expires_at:
        return
    if room["status"] == "finished":
        return
    state = room["state"]
    payouts = None
    reason = None
    if room["status"] == "lobby":
        state["finish_reason"] = "room_expired"
    else:
        game = state.get("game")
        if game:
            game["status"] = "finished"
            game["stage"] = "finished"
            game["finish_reason"] = "timeout"
            game["winners"] = []
            game["losers"] = []
        state["finish_reason"] = "timeout"
        if int(state.get("stake_cents", 0)):
            payouts = _room_payouts(room, timeout=True)
            reason = "timeout"
            state["settlement_payouts"] = payouts
    requester_id = room["owner_id"]
    if room["status"] == "playing":
        requester_id = next(
            (
                int(player["user_id"])
                for player in room["state"]["game"]["players"]
                if not player.get("forfeited")
            ),
            requester_id,
        )
    await database.update_minigame_room(
        room["room_code"],
        requester_id,
        room["version"],
        "finished",
        state,
        payouts=payouts,
        settlement_reason=reason,
    )


async def leave_room(room_code: str, user_id: int) -> tuple[int, dict[str, Any]]:
    room = await database.get_minigame_room(room_code.strip().upper())
    if room is None:
        return 404, {"error": "Комната не найдена."}
    await _expire_room_if_needed(room)
    room = await database.get_minigame_room(room_code.strip().upper())
    if room is None or room["status"] == "finished":
        return 409, {"error": "Комната уже завершена."}
    players = room["state"].get("game", {}).get("players", room["state"]["players"])
    player = next((item for item in players if int(item["user_id"]) == user_id), None)
    if player is None or player.get("forfeited"):
        return 403, {"error": "Ты не состоишь в этой комнате."}

    state = room["state"]
    status = room["status"]
    payouts = None
    settlement_reason = None
    if status == "playing":
        game = state["game"]
        card_game.forfeit_player(game, user_id)
        if game["status"] == "finished":
            status = "finished"
            payouts = _room_payouts(room)
            settlement_reason = "completed" if payouts else None
            if payouts:
                state["settlement_payouts"] = payouts
    elif user_id == room["owner_id"]:
        player["forfeited"] = True
        state["finish_reason"] = "owner_left"
        status = "finished"
    else:
        state["players"] = [
            item for item in state["players"] if int(item["user_id"]) != user_id
        ]

    updated = await database.update_minigame_room(
        room["room_code"],
        user_id,
        room["version"],
        status,
        state,
        payouts=payouts,
        settlement_reason=settlement_reason,
        remove_player=True,
    )
    if not updated:
        return 409, {"error": "Комната изменилась. Обнови её и попробуй снова."}
    return 200, {"status": "left"}


async def send_room_message(
    room_code: str,
    user_id: int,
    display_name: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    normalized = room_code.strip().upper()
    room = await database.get_minigame_room(normalized)
    if room is None:
        return 404, {"error": "Комната не найдена."}
    await _expire_room_if_needed(room)
    room = await database.get_minigame_room(normalized)
    if room is None or room["status"] == "finished":
        return 409, {"error": "Чат этой комнаты закрыт."}
    text = payload.get("message")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
        return 400, {"error": "Сообщение должно содержать от 1 до 500 символов."}
    player = next(
        (item for item in room["state"]["players"] if int(item["user_id"]) == user_id),
        None,
    )
    if player is None or player.get("forfeited"):
        return 403, {"error": "Писать в чат могут только участники комнаты."}
    message_id = await database.add_minigame_room_message(
        normalized,
        user_id,
        display_name,
        text.strip(),
    )
    if message_id is None:
        return 403, {"error": "Писать в чат могут только участники комнаты."}
    return 201, {"message_id": message_id}


async def invite_player(
    room_code: str,
    user_id: int,
    payload: dict[str, Any],
    bot: Any | None,
) -> tuple[int, dict[str, Any]]:
    room = await database.get_minigame_room(room_code.strip().upper())
    if room is None:
        return 404, {"error": "Комната не найдена."}
    await _expire_room_if_needed(room)
    room = await database.get_minigame_room(room_code.strip().upper())
    if room is None:
        return 404, {"error": "Комната не найдена."}
    if room["owner_id"] != user_id or room["status"] != "lobby":
        return 403, {"error": "Приглашать игроков может создатель комнаты до старта."}
    if len(room["state"]["players"]) >= room["max_players"]:
        return 409, {"error": "В комнате больше нет свободных мест."}
    recipient = payload.get("username")
    if not isinstance(recipient, str):
        return 400, {"error": "Укажи Telegram username игрока."}
    match = _USERNAME_RE.fullmatch(recipient.strip())
    if match is None:
        return 400, {"error": "Укажи корректный username длиной от 5 до 32 символов."}
    recipient_id = await database.find_user_id_by_username(match.group(1))
    if recipient_id is None:
        return 404, {"error": "Игрок не найден. Он должен хотя бы раз открыть бота."}
    if recipient_id == user_id:
        return 400, {"error": "Нельзя пригласить самого себя."}
    if not any(
        int(player["user_id"]) == user_id
        for player in room["state"]["players"]
    ):
        return 403, {"error": "Ты не состоишь в этой комнате."}
    if bot is None:
        return 503, {"error": "Не удалось связаться с ботом для отправки приглашения."}
    message = (
        f"Тебя пригласили в карточную комнату Юбары.\n"
        f"Код комнаты: {room['room_code']}\n"
        f"Открой Mini App бота → «Игры» → «Войти по коду»."
    )
    try:
        await bot.send_message(chat_id=recipient_id, text=message)
    except TelegramError:
        logging.exception(
            "Не удалось отправить приглашение в игровую комнату user_id=%s room=%s.",
            recipient_id,
            room["room_code"],
        )
        return 502, {"error": "Бот не смог доставить приглашение. Игроку может быть закрыто ЛС."}
    return 200, {"message": f"Приглашение отправлено @{match.group(1)}."}


async def perform_action(
    room_code: str,
    user_id: int,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    normalized = room_code.strip().upper()
    room = await database.get_minigame_room(normalized)
    if room is None:
        return 404, {"error": "Комната не найдена."}
    await _expire_room_if_needed(room)
    room = await database.get_minigame_room(normalized)
    if room is None:
        return 404, {"error": "Комната не найдена."}
    if room["status"] != "playing" or room["state"].get("game") is None:
        return 409, {"error": "Партия ещё не началась или уже завершена."}
    action = payload.get("action")
    if action not in ("play", "defend", "take", "finish"):
        return 400, {"error": "Неизвестный ход."}
    card_data = payload.get("card")
    card: dict[str, Any] | None = None
    if card_data is not None:
        if (
            not isinstance(card_data, dict)
            or type(card_data.get("rank")) is not int
            or card_data.get("suit") not in card_game.SUITS
        ):
            return 400, {"error": "Выбрана некорректная карта."}
        card = {"rank": card_data["rank"], "suit": card_data["suit"]}
    target = payload.get("target")
    if target is not None and type(target) is not int:
        return 400, {"error": "Выбрана некорректная карта на столе."}

    try:
        card_game.apply_action(
            room["state"]["game"],
            user_id,
            action,
            card=card,
            target=target,
        )
    except ValueError as error:
        return 409, {"error": str(error)}
    status = (
        "finished"
        if room["state"]["game"]["status"] == "finished"
        else "playing"
    )
    payouts = _room_payouts(room) if status == "finished" and room["state"].get("stake_cents") else None
    if payouts:
        room["state"]["settlement_payouts"] = payouts
    if not await database.update_minigame_room(
        normalized,
        user_id,
        room["version"],
        status,
        room["state"],
        payouts=payouts,
        settlement_reason="completed" if payouts else None,
    ):
        return 409, {"error": "Ход уже изменился. Обнови комнату и попробуй снова."}
    updated = await database.get_minigame_room(normalized)
    if updated is None:
        raise RuntimeError("Комната исчезла после хода.")
    response = _public_room(updated, user_id)
    if response is None:
        raise RuntimeError("Игрок не найден после хода.")
    return 200, response


def _tarot_response(reading: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": reading["question"],
        "cards": reading["cards"],
        "interpretation": reading["interpretation"],
        "status": reading["status"],
        "reading_date": reading["reading_date"],
    }


async def tarot_status(user_id: int) -> tuple[int, dict[str, Any]]:
    today = datetime.now(MOSCOW_TIMEZONE).date().isoformat()
    reading = await database.get_tarot_reading(user_id, today)
    return 200, {
        "reading_date": today,
        "available": reading is None,
        "reading": _tarot_response(reading) if reading else None,
    }


async def draw_tarot(user_id: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    question = payload.get("question")
    if not isinstance(question, str) or not 3 <= len(question.strip()) <= 300:
        return 400, {"error": "Вопрос должен содержать от 3 до 300 символов."}
    question = question.strip()
    today = datetime.now(MOSCOW_TIMEZONE).date().isoformat()
    created, reading = await database.reserve_tarot_reading(
        user_id,
        today,
        question,
        draw_spread(),
    )
    if not created:
        if reading["status"] == "complete":
            return 200, {
                **_tarot_response(reading),
                "already_used": True,
                "message": "Расклад на сегодня уже сделан. Завтра можно будет задать новый вопрос.",
            }
        return 409, {"error": "Расклад уже готовится. Обнови страницу через несколько секунд."}
    try:
        interpretation = await llm.generate_tarot_interpretation(
            question,
            reading["cards"],
        )
        await database.complete_tarot_reading(user_id, today, interpretation)
    except Exception:
        logging.exception("Не удалось подготовить расклад Таро user_id=%s.", user_id)
        await database.remove_pending_tarot_reading(user_id, today)
        return 503, {"error": "Не удалось получить толкование. Попробуй ещё раз немного позже."}
    return 201, {
        **_tarot_response(
            {
                **reading,
                "interpretation": interpretation,
                "status": "complete",
            }
        ),
        "already_used": False,
    }
