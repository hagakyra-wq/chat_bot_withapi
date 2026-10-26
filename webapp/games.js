import { apiRequest, formatCoinAmount, showToast } from "./api.js";

let currentRoom;
let selectedHandIndex = null;
let selectedTarget = null;
let roomPoll;
let roomPollError = null;
let initialized = false;
let roomClockTimer;

const suitLabels = {
  clubs: ["♣", "clubs"],
  diamonds: ["♦", "red"],
  hearts: ["♥", "red"],
  spades: ["♠", "spades"],
};
const rankLabels = { 11: "В", 12: "Д", 13: "К", 14: "Т" };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function cardLabel(card) {
  return `${rankLabels[card.rank] || card.rank}${suitLabels[card.suit][0]}`;
}

function makePlayingCard(card, { selected = false, small = false } = {}) {
  const [, suitClass] = suitLabels[card.suit] || ["?", ""];
  const node = element(
    "span",
    `playing-card ${suitClass}${selected ? " is-selected" : ""}${small ? " is-small" : ""}`,
  );
  node.append(element("strong", "playing-card-rank", rankLabels[card.rank] || String(card.rank)));
  node.append(element("span", "playing-card-suit", suitLabels[card.suit]?.[0] || "?"));
  return node;
}

function renderRooms(rooms) {
  const list = document.getElementById("game-room-list");
  list.replaceChildren();
  if (!rooms.length) {
    list.append(element("div", "panel game-empty", "Пока нет активных комнат. Создай стол и пригласи друзей."));
    return;
  }
  for (const room of rooms) {
    const item = element("article", "panel room-list-item");
    const summary = element("div", "room-list-summary");
    summary.append(element("strong", "", `Комната ${room.room_code}`));
    summary.append(
      element(
        "span",
        "",
        `${room.players.length} из ${room.max_players} · ${room.status === "lobby" ? "ожидает игроков" : "партия началась"}`,
      ),
    );
    const button = element("button", "button button-quiet", "Открыть");
    button.type = "button";
    button.addEventListener("click", () => openRoom(room.room_code));
    item.append(summary, button);
    list.append(item);
  }
}

function renderPlayers(room) {
  const players = document.getElementById("room-players");
  players.replaceChildren();
  for (const player of room.players) {
    const row = element("div", "room-player");
    const identity = element("div", "room-player-identity");
    const avatar = makePlayerAvatar(player);
    identity.append(avatar);
    const name = element("strong", "", player.display_name);
    identity.append(name);
    if (player.user_id === room.owner_id) identity.append(element("small", "", "создатель"));
    row.append(identity);
    row.append(element(
      "span",
      `room-player-status${player.has_left ? " is-left" : ""}`,
      player.has_left ? "вышел" : room.status === "lobby" ? "на месте" : `${player.cards_count} карт`,
    ));
    players.append(row);
  }
  if (room.status === "lobby") {
    for (let i = room.players.length; i < room.max_players; i += 1) {
      players.append(element("div", "room-player room-player-open", "Ждём игрока…"));
    }
  }
}

function profileHue(photoUrl, userId) {
  const source = photoUrl || String(userId);
  let hash = 0;
  for (let index = 0; index < source.length; index += 1) {
    hash = (hash * 31 + source.charCodeAt(index)) >>> 0;
  }
  return hash % 360;
}

function makePlayerAvatar(player, small = false) {
  const avatar = element("span", `room-avatar${small ? " room-avatar-small" : ""}`);
  avatar.style.setProperty("--avatar-hue", String(profileHue(player.photo_url, player.user_id)));
  if (player.photo_url) {
    const image = element("img");
    image.alt = "";
    image.referrerPolicy = "no-referrer";
    image.src = player.photo_url;
    const colorProbe = new Image();
    colorProbe.crossOrigin = "anonymous";
    colorProbe.referrerPolicy = "no-referrer";
    colorProbe.addEventListener("load", () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(colorProbe, 0, 0, 1, 1);
        const [red, green, blue] = context.getImageData(0, 0, 1, 1).data;
        avatar.style.setProperty("--avatar-hue", String(rgbHue(red, green, blue)));
      } catch {
        avatar.style.setProperty("--avatar-hue", String(profileHue(player.photo_url, player.user_id)));
      }
    }, { once: true });
    colorProbe.src = player.photo_url;
    image.addEventListener("error", () => image.remove(), { once: true });
    avatar.append(image);
  } else {
    avatar.append(element("span", "room-avatar-initial", player.display_name.slice(0, 1)));
  }
  avatar.append(element("span", "room-avatar-mask"));
  return avatar;
}

function rgbHue(red, green, blue) {
  const [r, g, b] = [red, green, blue].map((value) => value / 255);
  const maximum = Math.max(r, g, b);
  const minimum = Math.min(r, g, b);
  const delta = maximum - minimum;
  if (!delta) return 0;
  let hue;
  if (maximum === r) hue = ((g - b) / delta) % 6;
  else if (maximum === g) hue = (b - r) / delta + 2;
  else hue = (r - g) / delta + 4;
  return Math.round((hue * 60 + 360) % 360);
}

function renderRoomChat(messages) {
  const chat = document.getElementById("room-chat-messages");
  const shouldStickToBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 48;
  chat.replaceChildren();
  if (!messages.length) {
    chat.append(element("p", "room-chat-empty", "Здесь появятся сообщения игроков."));
    return;
  }
  for (const message of messages) {
    const line = element("article", "room-chat-message");
    line.append(element("strong", "", message.display_name));
    line.append(element("p", "", message.message));
    chat.append(line);
  }
  if (shouldStickToBottom) chat.scrollTop = chat.scrollHeight;
}

function addActionButton(container, label, action, disabled = false) {
  const button = element("button", "button button-quiet", label);
  button.type = "button";
  button.dataset.action = action;
  button.disabled = disabled;
  container.append(button);
}

function renderTable(room) {
  const table = document.getElementById("card-table");
  const game = room.game;
  table.hidden = false;
  const idle = document.getElementById("table-idle");
  idle.hidden = Boolean(game);
  for (const selector of [".table-meta", ".table-opponents", ".table-cards", ".table-turn", ".player-hand", ".table-actions"]) {
    const node = table.querySelector(selector);
    node.hidden = !game;
  }
  if (!game) return;

  document.getElementById("deck-count").textContent = `В колоде: ${game.deck_count}`;
  const trump = document.getElementById("trump-badge");
  trump.replaceChildren(element("span", "", "Козырь"));
  if (game.trump) trump.append(makePlayingCard(game.trump, { small: true }));
  else trump.append(element("strong", "", suitLabels[game.trump_suit][0]));

  const opponents = document.getElementById("table-opponents");
  opponents.replaceChildren();
  for (const player of room.players) {
    const node = element("div", "table-opponent");
    if (player.user_id === window.telegramUserId) node.classList.add("is-self");
    const avatar = makePlayerAvatar(player, true);
    node.append(avatar, element("strong", "", player.display_name));
    node.append(element("span", "", player.has_left ? "вышел" : `${player.cards_count} карт`));
    if (player.user_id === game.current_actor_id) node.classList.add("is-active");
    opponents.append(node);
  }

  const tableCards = document.getElementById("table-cards");
  tableCards.replaceChildren();
  if (!game.table.length) {
    tableCards.append(element("p", "table-empty", "Стол пока пуст — пора атаковать"));
  } else {
    game.table.forEach((entry, index) => {
      const pair = element("div", "table-card-pair");
      const attack = makePlayingCard(entry.attack, {
        small: true,
        selected: selectedTarget === index,
      });
      if (game.is_your_turn && game.stage === "defend" && entry.defense === null) {
        attack.classList.add("can-target");
        attack.setAttribute("role", "button");
        attack.setAttribute("tabindex", "0");
        attack.addEventListener("click", () => {
          selectedTarget = index;
          renderRoom(room);
        });
        attack.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectedTarget = index;
            renderRoom(room);
          }
        });
      }
      pair.append(attack);
      if (entry.defense) pair.append(makePlayingCard(entry.defense, { small: true }));
      tableCards.append(pair);
    });
  }

  let turnMessage = "Ход соперника";
  if (game.is_your_turn) turnMessage = game.stage === "defend" ? "Твой ход — отбивайся или забирай карты" : "Твой ход — атакуй";
  else if (game.can_finish) turnMessage = "Можно закончить отбиваться или принять подкид";
  document.getElementById("table-turn").textContent = turnMessage;

  const hand = document.getElementById("player-hand");
  hand.replaceChildren();
  game.your_hand.forEach((card, index) => {
    const button = element(
      "button",
      "hand-card-button",
    );
    button.type = "button";
    button.setAttribute("aria-label", `${cardLabel(card)}${selectedHandIndex === index ? ", выбрана" : ""}`);
    button.append(makePlayingCard(card, { selected: selectedHandIndex === index }));
    button.addEventListener("click", () => {
      selectedHandIndex = selectedHandIndex === index ? null : index;
      renderRoom(room);
    });
    hand.append(button);
  });

  const actions = document.getElementById("table-actions");
  actions.replaceChildren();
  if (game.can_attack) {
    addActionButton(actions, game.stage === "attack" ? "Атаковать выбранной картой" : "Подкинуть карту", "play", selectedHandIndex === null);
  }
  const defender = room.players.some((player) => player.user_id === game.defender_id && player.user_id === window.telegramUserId);
  if (defender && game.stage === "defend") {
    addActionButton(actions, "Отбить выбранной картой", "defend", selectedHandIndex === null || selectedTarget === null);
  }
  if (game.can_take) addActionButton(actions, "Забрать карты", "take");
  if (game.can_finish) addActionButton(actions, "Закончить отбой", "finish");
}

function renderRoom(room) {
  const sameRoom = currentRoom?.room_code === room.room_code;
  if (!sameRoom || currentRoom.version !== room.version) {
    selectedHandIndex = null;
    selectedTarget = null;
  }
  currentRoom = room;
  if (selectedTarget !== null && !room.game?.table[selectedTarget]) selectedTarget = null;
  document.getElementById("room-lobby").hidden = true;
  document.getElementById("room-view").hidden = false;
  document.getElementById("room-code-label").textContent = room.room_code;
  document.getElementById("room-status-label").textContent =
    room.status === "lobby" ? "ОЖИДАНИЕ ИГРОКОВ" : room.status === "finished" ? "ПАРТИЯ ЗАВЕРШЕНА" : "ПАРТИЯ ИДЁТ";
  renderPlayers(room);
  renderRoomChat(room.chat || []);
  window.clearInterval(roomClockTimer);
  const clock = document.getElementById("room-clock");
  const expiresAt = room.expires_at;
  const updateClock = () => {
    if (!expiresAt) {
      clock.textContent = "";
      return;
    }
    const seconds = Math.max(0, Math.floor(expiresAt - Date.now() / 1000));
    clock.textContent = `Осталось ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  };
  updateClock();
  roomClockTimer = window.setInterval(updateClock, 1000);

  const ownerInLobby = room.is_owner && room.status === "lobby";
  const full = room.players.length === room.max_players;
  const inviteForm = document.getElementById("room-invite-form");
  inviteForm.hidden = !ownerInLobby;
  const startButton = document.getElementById("room-start");
  startButton.hidden = !ownerInLobby;
  startButton.disabled = !full;
  document.getElementById("room-share").hidden = room.status !== "lobby";
  document.getElementById("room-chat-form").hidden = room.status === "finished";
  document.getElementById("room-help").textContent = room.status === "lobby"
    ? full
      ? "Все места заняты. Создатель комнаты может начинать."
      : `«Дурак» · ${room.mode === "coins" ? `ставка ${formatCoinAmount(room.stake_cents)} монет` : "бесплатно"} · поделись /room ${room.room_code}`
    : room.status === "finished"
      ? "Комната завершена."
      : `«Дурак» · ${room.mode === "coins" ? `ставка ${formatCoinAmount(room.stake_cents)} монет` : "бесплатно"} · ходы обновляются автоматически.`;

  renderTable(room);
  const winner = document.getElementById("room-winner");
  winner.hidden = room.status !== "finished";
  if (room.status === "finished") {
    const game = room.game;
    if (room.finish_reason === "timeout" || game?.finish_reason === "timeout") {
      winner.textContent = room.mode === "coins"
        ? "Время вышло. Партия завершена, каждому возвращено 95% ставки."
        : "Время комнаты вышло. Партия завершена.";
    } else if (room.finish_reason === "room_expired") {
      winner.textContent = "Время ожидания вышло. Комната закрыта, ставка не списывалась.";
    } else if (room.finish_reason === "owner_left") {
      winner.textContent = "Создатель покинул комнату. Комната закрыта.";
    } else if (!game) {
      winner.textContent = "Комната завершена.";
    } else {
      const winnerId = game.placement_order?.[0] ?? game.winners?.[0];
      const winnerName = room.players.find((player) => player.user_id === winnerId)?.display_name || "Игрок";
      const prefix = game.finish_reason === "round_limit" ? "Лимит раундов. " : "";
      winner.textContent = winnerId === window.telegramUserId
        ? `${prefix}Поздравляем, ${winnerName}! Ты победил.`
        : `${prefix}Поздравляем победителя — ${winnerName}!`;
      const payout = room.settlement_payouts?.find((item) => item.user_id === window.telegramUserId);
      if (payout?.delta_cents) winner.textContent += ` Тебе начислено ${formatCoinAmount(payout.delta_cents)} монет.`;
    }
  }
}

async function openRoom(code) {
  try {
    const room = await apiRequest(`/api/v1/games/rooms/room?code=${encodeURIComponent(code)}`);
    renderRoom(room);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function refreshRoom() {
  if (!currentRoom) return;
  const { room_code: code } = currentRoom;
  try {
    const room = await apiRequest(`/api/v1/games/rooms/room?code=${encodeURIComponent(code)}`);
    roomPollError = null;
    renderRoom(room);
  } catch (error) {
    if (error.message.includes("не найдена")) {
      currentRoom = null;
      document.getElementById("room-view").hidden = true;
      document.getElementById("room-lobby").hidden = false;
      showToast(error.message, true);
    } else if (roomPollError !== error.message) {
      roomPollError = error.message;
      showToast(error.message, true);
    }
  }
}

async function submitAction(action) {
  if (!currentRoom) return;
  const payload = { room_code: currentRoom.room_code, action };
  if (action === "play" || action === "defend") {
    if (selectedHandIndex === null) return;
    payload.card = currentRoom.game.your_hand[selectedHandIndex];
  }
  if (action === "defend") payload.target = selectedTarget;
  try {
    const room = await apiRequest("/api/v1/games/rooms/action", {
      method: "POST",
      body: payload,
    });
    renderRoom(room);
  } catch (error) {
    showToast(error.message, true);
    await refreshRoom();
  }
}

export async function joinRoomByCode(code) {
  try {
    const room = await apiRequest("/api/v1/games/rooms/join", {
      method: "POST",
      body: { room_code: code },
    });
    renderRoom(room);
    showToast(`Ты в комнате ${room.room_code}.`);
  } catch (error) {
    showToast(error.message, true);
  }
}

export async function loadRooms() {
  const { rooms } = await apiRequest("/api/v1/games/rooms");
  roomPollError = null;
  renderRooms(rooms);
  if (currentRoom) await refreshRoom();
}

export function setGamesPageActive(active) {
  window.clearInterval(roomPoll);
  if (active && window.Telegram?.WebApp?.initData) {
    roomPoll = window.setInterval(() => {
      if (currentRoom) refreshRoom();
      else {
        loadRooms().catch((error) => {
          if (roomPollError !== error.message) {
            roomPollError = error.message;
            showToast(error.message, true);
          }
        });
      }
    }, 2500);
  }
}

export function initGames() {
  if (initialized) return;
  initialized = true;
  const telegramUser = window.Telegram?.WebApp?.initDataUnsafe?.user;
  if (telegramUser?.id) window.telegramUserId = telegramUser.id;

  document.getElementById("room-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const maxPlayers = Number(formData.get("max_players"));
    const mode = formData.get("mode");
    const stakeAmount = Number(formData.get("stake") || 0);
    const stakeCents = mode === "coins" ? Math.round(stakeAmount * 100) : 0;
    if (mode === "coins" && (!Number.isFinite(stakeAmount) || stakeCents < 1)) {
      showToast("Укажи положительную ставку в монетах.", true);
      return;
    }
    try {
      const room = await apiRequest("/api/v1/games/rooms/create", {
        method: "POST",
        body: {
          game_type: formData.get("game_type"),
          max_players: maxPlayers,
          mode,
          stake_cents: stakeCents,
        },
      });
      renderRoom(room);
      showToast(`Комната ${room.room_code} создана.`);
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById("room-join-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const roomCode = new FormData(event.currentTarget).get("room_code");
    await joinRoomByCode(roomCode);
    event.currentTarget.reset();
  });
  document.getElementById("room-mode").addEventListener("change", (event) => {
    document.getElementById("room-stake-field").hidden = event.currentTarget.value !== "coins";
  });
  document.getElementById("rooms-refresh").addEventListener("click", () => {
    loadRooms().catch((error) => showToast(error.message, true));
  });
  document.getElementById("room-start").addEventListener("click", async () => {
    if (!currentRoom) return;
    try {
      const room = await apiRequest("/api/v1/games/rooms/start", {
        method: "POST",
        body: { room_code: currentRoom.room_code },
      });
      renderRoom(room);
    } catch (error) {
      showToast(error.message, true);
      await refreshRoom();
    }
  });
  document.getElementById("room-invite-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentRoom) return;
    const username = new FormData(event.currentTarget).get("username");
    try {
      const result = await apiRequest("/api/v1/games/rooms/invite", {
        method: "POST",
        body: { room_code: currentRoom.room_code, username },
      });
      showToast(result.message);
      event.currentTarget.reset();
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById("room-code-label").addEventListener("click", async () => {
    if (!currentRoom || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(currentRoom.room_code);
      showToast("Код комнаты скопирован.");
    } catch {
      showToast(`Код комнаты: ${currentRoom.room_code}`);
    }
  });
  document.getElementById("room-share").addEventListener("click", async () => {
    if (!currentRoom) return;
    try {
      await navigator.clipboard.writeText(`/room ${currentRoom.room_code}`);
      showToast("Команду-приглашение скопировали. Отправь её в группу.");
    } catch {
      showToast(`Отправь в группу: /room ${currentRoom.room_code}`);
    }
  });
  document.getElementById("room-exit").addEventListener("click", () => {
    if (!currentRoom) return;
    if (currentRoom.status === "finished") {
      currentRoom = null;
      window.clearInterval(roomClockTimer);
      document.getElementById("room-view").hidden = true;
      document.getElementById("room-lobby").hidden = false;
      loadRooms().catch((error) => showToast(error.message, true));
      return;
    }
    const leave = async () => {
      try {
        await apiRequest("/api/v1/games/rooms/leave", {
          method: "POST",
          body: { room_code: currentRoom.room_code },
        });
        currentRoom = null;
        window.clearInterval(roomClockTimer);
        document.getElementById("room-view").hidden = true;
        document.getElementById("room-lobby").hidden = false;
        showToast("Ты вышел из комнаты.");
        await loadRooms();
      } catch (error) {
        showToast(error.message, true);
      }
    };
    if (currentRoom.status === "playing" && window.Telegram?.WebApp?.showConfirm) {
      window.Telegram.WebApp.showConfirm(
        "Выход из начавшейся партии засчитывается как поражение.",
        (confirmed) => {
          if (confirmed) leave();
        },
      );
    } else if (currentRoom.status !== "playing" || window.confirm("Выйти из комнаты?")) {
      leave();
    }
  });
  document.getElementById("room-chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentRoom) return;
    const form = event.currentTarget;
    const message = new FormData(form).get("message");
    try {
      await apiRequest("/api/v1/games/rooms/messages", {
        method: "POST",
        body: { room_code: currentRoom.room_code, message },
      });
      form.reset();
      await refreshRoom();
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById("table-actions").addEventListener("click", (event) => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action) submitAction(action);
  });
  const table = document.getElementById("card-table");
  let leanTimer;
  const stopLeaning = () => {
    window.clearTimeout(leanTimer);
    table.classList.remove("is-leaning");
  };
  table.addEventListener("pointerdown", () => {
    window.clearTimeout(leanTimer);
    leanTimer = window.setTimeout(() => table.classList.add("is-leaning"), 180);
  });
  for (const eventName of ["pointerup", "pointercancel", "pointerleave"]) {
    table.addEventListener(eventName, stopLeaning);
  }
  table.addEventListener("contextmenu", (event) => event.preventDefault());
}
