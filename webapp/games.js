import { apiRequest, showToast } from "./api.js";

let currentRoom;
let selectedHandIndex = null;
let selectedTarget = null;
let roomPoll;
let roomPollError = null;
let tarotLoaded = false;
let initialized = false;

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
        `${room.players.length} из ${room.max_players} игроков · ${room.status === "lobby" ? "ожидает игроков" : "партия началась"}`,
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
    identity.append(element("span", "room-player-mark", player.user_id === room.owner_id ? "✦" : "♧"));
    const name = element("strong", "", player.display_name);
    identity.append(name);
    if (player.user_id === room.owner_id) identity.append(element("small", "", "создатель"));
    row.append(identity);
    row.append(element("span", "room-player-status", room.status === "lobby" ? "на месте" : `${player.cards_count} карт`));
    players.append(row);
  }
  if (room.status === "lobby") {
    for (let i = room.players.length; i < room.max_players; i += 1) {
      players.append(element("div", "room-player room-player-open", "Ждём игрока…"));
    }
  }
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
  table.hidden = !game;
  if (!game) return;

  document.getElementById("deck-count").textContent = `В колоде: ${game.deck_count}`;
  const trump = document.getElementById("trump-badge");
  trump.replaceChildren(element("span", "", "Козырь"));
  if (game.trump) trump.append(makePlayingCard(game.trump, { small: true }));
  else trump.append(element("strong", "", suitLabels[game.trump_suit][0]));

  const opponents = document.getElementById("table-opponents");
  opponents.replaceChildren();
  for (const player of room.players) {
    if (player.user_id === window.telegramUserId) continue;
    const node = element("div", "table-opponent");
    node.append(element("strong", "", player.display_name));
    node.append(element("span", "", `${player.cards_count} карт`));
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

  const ownerInLobby = room.is_owner && room.status === "lobby";
  const full = room.players.length === room.max_players;
  const inviteForm = document.getElementById("room-invite-form");
  inviteForm.hidden = !ownerInLobby;
  const startButton = document.getElementById("room-start");
  startButton.hidden = !ownerInLobby;
  startButton.disabled = !full;
  document.getElementById("room-help").textContent = room.status === "lobby"
    ? full ? "Все места заняты. Создатель комнаты может начинать." : `Нужно ещё ${room.max_players - room.players.length} игрока. Отправь код комнаты или пригласи друга.`
    : "Ход и карты игроков хранятся на сервере и обновляются автоматически.";

  renderTable(room);
  const winner = document.getElementById("room-winner");
  winner.hidden = room.status !== "finished";
  if (room.status === "finished") {
    const game = room.game;
    const userLost = game.losers.includes(window.telegramUserId);
    winner.textContent = game.finish_reason === "round_limit"
      ? userLost
        ? "Лимит раундов: у тебя осталось больше всего карт."
        : game.losers.length
          ? "Лимит раундов: у тебя меньше карт, чем у проигравших."
          : "Лимит раундов завершён ничьей."
      : game.loser_id === null
        ? "Ничья — у всех закончились карты."
        : game.loser_id === window.telegramUserId
          ? "Партия завершена. В этот раз дурак — ты."
          : "Партия завершена. Поздравляем, ты не остался с картами!";
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

function renderTarot(reading) {
  const result = document.getElementById("tarot-result");
  result.hidden = !reading || reading.status !== "complete";
  if (!reading || reading.status !== "complete") return;
  const cards = document.getElementById("tarot-cards");
  cards.replaceChildren();
  reading.cards.forEach((card) => {
    const article = element("article", "tarot-card");
    const position = element("span", "tarot-position", card.position);
    const face = element("div", `tarot-card-face${card.reversed ? " is-reversed" : ""}`);
    face.append(element("span", "tarot-card-arcana", card.arcana));
    face.append(element("span", "tarot-card-symbol", card.symbol));
    face.append(element("strong", "tarot-card-name", card.name));
    face.append(element("span", "tarot-card-orientation", card.reversed ? "Перевёрнутая" : "Прямая"));
    article.append(position, face);
    cards.append(article);
  });
  document.getElementById("tarot-interpretation").textContent = reading.interpretation;
}

async function updateTarotAvailability() {
  const status = await apiRequest("/api/v1/games/tarot");
  const label = document.getElementById("tarot-availability");
  const submit = document.getElementById("tarot-submit");
  const textarea = document.getElementById("tarot-question");
  if (status.reading?.status === "pending") {
    label.textContent = "Расклад готовится. Обнови страницу через несколько секунд.";
    submit.disabled = true;
    textarea.disabled = true;
  } else if (status.available) {
    label.textContent = "Новый расклад доступен. Следующий — после полуночи по Москве.";
    submit.disabled = false;
    textarea.disabled = false;
    renderTarot(null);
  } else {
    label.textContent = "Расклад на сегодня уже использован. Возвращайся завтра.";
    submit.disabled = true;
    textarea.disabled = true;
    renderTarot(status.reading);
  }
}

export async function loadTarot() {
  if (tarotLoaded) {
    await updateTarotAvailability();
    return;
  }
  await updateTarotAvailability();
  tarotLoaded = true;
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
    const maxPlayers = Number(new FormData(event.currentTarget).get("max_players"));
    try {
      const room = await apiRequest("/api/v1/games/rooms/create", {
        method: "POST",
        body: { max_players: maxPlayers },
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
    try {
      const room = await apiRequest("/api/v1/games/rooms/join", {
        method: "POST",
        body: { room_code: roomCode },
      });
      renderRoom(room);
      event.currentTarget.reset();
      showToast(`Ты в комнате ${room.room_code}.`);
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.getElementById("rooms-refresh").addEventListener("click", () => {
    loadRooms().catch((error) => showToast(error.message, true));
  });
  document.getElementById("room-back").addEventListener("click", () => {
    currentRoom = null;
    document.getElementById("room-view").hidden = true;
    document.getElementById("room-lobby").hidden = false;
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
  document.getElementById("table-actions").addEventListener("click", (event) => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action) submitAction(action);
  });
  document.getElementById("tarot-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = new FormData(event.currentTarget).get("question");
    const submit = document.getElementById("tarot-submit");
    submit.disabled = true;
    document.getElementById("tarot-availability").textContent = "Карты перемешиваются…";
    try {
      const reading = await apiRequest("/api/v1/games/tarot", {
        method: "POST",
        body: { question },
      });
      renderTarot(reading);
      document.getElementById("tarot-question").value = reading.question;
      await updateTarotAvailability();
    } catch (error) {
      showToast(error.message, true);
      await updateTarotAvailability().catch((statusError) => {
        showToast(statusError.message, true);
      });
    }
  });
}
