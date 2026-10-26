import { apiRequest, formatCoinAmount, showToast } from "./api.js";
import {
  isGameSoundEnabled,
  playGameSound,
  setGameSoundEnabled,
  unlockGameAudio,
} from "./sounds.js";

let currentRoom;
let selectedHandIndex = null;
let selectedTarget = null;
let roomPoll;
let roomPollError = null;
let initialized = false;
let roomClockTimer;
let room3d;
let room3dRoomCode = null;
let room3dLoadPromise;
let room3dErrorRoom = null;
let inviteGroupsRoomCode = null;
let inviteGroupsError = null;
let gamesPageActive = false;

async function enterRoomFullscreen(notify = false) {
  const webApp = window.Telegram?.WebApp;
  let lastError;
  try {
    if (webApp?.requestFullscreen && !webApp.isFullscreen) {
      await webApp.requestFullscreen();
      if (webApp.isFullscreen) return;
    }
  } catch (error) {
    lastError = error;
  }
  if (document.fullscreenElement) return;
  try {
    if (document.documentElement.requestFullscreen) {
      await document.documentElement.requestFullscreen();
      if (document.fullscreenElement) return;
    }
  } catch (error) {
    lastError = error;
  }
  if (webApp?.expand) {
    webApp.expand();
    if (!notify) return;
  }
  if (lastError) console.warn("Не удалось включить полноэкранный режим.", lastError);
  if (notify) showToast("Полноэкранный режим недоступен. Mini App раскрыт насколько позволяет Telegram.", true);
}

async function exitRoomFullscreen() {
  const webApp = window.Telegram?.WebApp;
  if (webApp?.isFullscreen && webApp.exitFullscreen) {
    await webApp.exitFullscreen();
  } else if (document.fullscreenElement && document.exitFullscreen) {
    await document.exitFullscreen();
  } else {
    webApp?.expand();
  }
}

function updateFullscreenButton() {
  const button = document.getElementById("room-fullscreen");
  if (!button) return;
  const active = Boolean(
    window.Telegram?.WebApp?.isFullscreen || document.fullscreenElement,
  );
  button.textContent = active ? "Свернуть" : "На весь экран";
  button.setAttribute("aria-label", active ? "Выйти из полного экрана" : "На весь экран");
}

function updateSoundButton() {
  const button = document.getElementById("room-sound");
  if (button) button.textContent = isGameSoundEnabled() ? "Звук: вкл." : "Звук: выкл.";
}

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

async function loadRoomInviteGroups(roomCode) {
  if (inviteGroupsRoomCode === roomCode) return;
  inviteGroupsRoomCode = roomCode;
  const select = document.getElementById("room-invite-group");
  const submit = document.querySelector("#room-invite-form button[type=submit]");
  select.disabled = true;
  submit.disabled = true;
  select.replaceChildren(element("option", "", "Загружаем группы…"));
  try {
    const { groups } = await apiRequest("/api/v1/community/groups");
    if (currentRoom?.room_code !== roomCode) return;
    select.replaceChildren();
    if (!groups.length) {
      select.append(element("option", "", "Нет подключённых групп"));
      inviteGroupsRoomCode = roomCode;
      return;
    }
    for (const group of groups) {
      const option = element("option", "", group.title);
      option.value = String(group.group_id);
      select.append(option);
    }
    select.disabled = false;
    submit.disabled = false;
    inviteGroupsError = null;
  } catch (error) {
    inviteGroupsRoomCode = roomCode;
    select.replaceChildren(element("option", "", "Не удалось загрузить группы"));
    if (inviteGroupsError !== error.message) {
      inviteGroupsError = error.message;
      showToast(error.message, true);
    }
  }
}

function syncRoomScene(room, hasGameScene) {
  const container = document.getElementById("card-table");
  if (!hasGameScene || !room.game) {
    room3d?.destroy();
    room3d = null;
    room3dRoomCode = null;
    room3dErrorRoom = null;
    return;
  }
  if (room3d && room3dRoomCode !== room.room_code) {
    room3d.destroy();
    room3d = null;
  }
  if (room3d) {
    room3d.update(room, selectedTarget, selectedHandIndex);
    return;
  }
  if (room3dLoadPromise || room3dErrorRoom === room.room_code) return;

  const roomCode = room.room_code;
  room3dLoadPromise = import("./room3d.js")
    .then(({ createRoomScene }) => {
      if (!currentRoom || currentRoom.room_code !== roomCode || !currentRoom.game) return;
      room3d = createRoomScene(container, {
        onAction: (action, cardIndex) => submitAction(action, cardIndex),
        onSelectCard: (index) => {
          selectedHandIndex = index;
          renderRoom(currentRoom);
        },
        onTarget: (index) => {
          selectedTarget = index;
          renderRoom(currentRoom);
        },
        onToast: (message) => showToast(message, true),
      });
      room3dRoomCode = roomCode;
      room3d.update(currentRoom, selectedTarget, selectedHandIndex);
      room3d.setPaused(!gamesPageActive);
    })
    .catch((error) => {
      room3dErrorRoom = roomCode;
      console.error("Не удалось загрузить 3D-комнату.", error);
      showToast(`Не удалось загрузить 3D-комнату: ${error.message}`, true);
    })
    .finally(() => {
      room3dLoadPromise = null;
      if (currentRoom?.game && currentRoom.room_code !== roomCode) {
        syncRoomScene(currentRoom, true);
      }
    });
}

function renderTable(room) {
  const table = document.getElementById("card-table");
  const actions = document.getElementById("table-actions");
  const game = room.game;
  const hasGameScene = room.status === "playing" || (room.status === "finished" && Boolean(game));
  table.hidden = !hasGameScene;
  actions.hidden = !hasGameScene;
  if (!hasGameScene || !game) {
    actions.replaceChildren();
    syncRoomScene(room, false);
    return;
  }

  document.getElementById("deck-count").textContent = `В колоде: ${game.deck_count}`;
  const trump = document.getElementById("trump-badge");
  trump.replaceChildren(element("span", "", "Козырь"));
  if (game.trump) trump.append(makePlayingCard(game.trump, { small: true }));
  else trump.append(element("strong", "", suitLabels[game.trump_suit][0]));

  let turnMessage = "Ход соперника";
  if (game.is_your_turn) turnMessage = game.stage === "defend" ? "Твой ход — отбивайся или забирай карты" : "Твой ход — атакуй";
  else if (game.can_finish) turnMessage = "Можно закончить отбиваться или принять подкид";
  document.getElementById("table-turn").textContent = turnMessage;

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
  syncRoomScene(room, hasGameScene);
}

function renderRoom(room) {
  const previousRoom = currentRoom;
  const sameRoom = currentRoom?.room_code === room.room_code;
  if (!sameRoom || currentRoom.version !== room.version) {
    selectedHandIndex = null;
    selectedTarget = null;
  }
  currentRoom = room;
  const hasGameScene = room.status === "playing" || (room.status === "finished" && Boolean(room.game));
  const wasPlaying = previousRoom?.room_code === room.room_code
    && (previousRoom.status === "playing" || previousRoom.status === "finished");
  document.querySelector(".app-shell").classList.toggle("is-room-playing", hasGameScene);
  document.body.classList.toggle("is-room-playing", hasGameScene);
  if (hasGameScene && !wasPlaying) void enterRoomFullscreen();
  if (wasPlaying && previousRoom.status !== "finished" && room.status === "finished") {
    playGameSound("win");
  } else if (
    wasPlaying
    && room.status === "playing"
    && JSON.stringify(previousRoom.game?.table) !== JSON.stringify(room.game?.table)
  ) {
    playGameSound("card");
  }
  if (selectedTarget !== null && !room.game?.table[selectedTarget]) selectedTarget = null;
  document.getElementById("room-lobby").hidden = true;
  document.getElementById("room-view").hidden = false;
  const roomContent = document.getElementById("room-content");
  const playerPanel = document.getElementById("room-player-panel");
  const chat = playerPanel.querySelector(".room-chat");
  roomContent.classList.toggle("is-playing", hasGameScene);
  playerPanel.hidden = hasGameScene;
  if (hasGameScene) roomContent.append(chat);
  else playerPanel.append(chat);
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
  if (ownerInLobby) loadRoomInviteGroups(room.room_code);
  const startButton = document.getElementById("room-start");
  const closeButton = document.getElementById("room-close");
  startButton.hidden = !ownerInLobby;
  startButton.disabled = !full;
  closeButton.hidden = !(room.is_owner || window.userRole === "admin") || room.status === "finished";
  closeButton.disabled = room.status === "finished";
  document.getElementById("room-share").hidden = room.status !== "lobby";
  document.getElementById("room-chat-form").hidden = room.status === "finished";
  document.getElementById("room-fullscreen").hidden = !hasGameScene;
  document.getElementById("room-sound").hidden = !hasGameScene;
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
    } else if (room.finish_reason === "owner_closed") {
      winner.textContent = "Создатель закрыл комнату.";
    } else if (room.finish_reason === "admin_closed") {
      winner.textContent = "Администратор закрыл комнату.";
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
      document.querySelector(".app-shell").classList.remove("is-room-playing");
      document.body.classList.remove("is-room-playing");
      void exitRoomFullscreen().catch((fullscreenError) => {
        console.error("Не удалось выйти из полного экрана.", fullscreenError);
      });
      syncRoomScene(null, false);
      document.getElementById("room-view").hidden = true;
      document.getElementById("room-lobby").hidden = false;
      showToast(error.message, true);
    } else if (roomPollError !== error.message) {
      roomPollError = error.message;
      showToast(error.message, true);
    }
  }
}

async function submitAction(action, cardIndex = selectedHandIndex) {
  if (!currentRoom) return;
  const payload = { room_code: currentRoom.room_code, action };
  if (action === "play" || action === "defend") {
    if (cardIndex === null || cardIndex < 0 || cardIndex >= currentRoom.game.your_hand.length) return;
    payload.card = currentRoom.game.your_hand[cardIndex];
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
  gamesPageActive = active;
  room3d?.setPaused(!active);
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
    unlockGameAudio();
    void enterRoomFullscreen();
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
    const groupId = Number(new FormData(event.currentTarget).get("group_id"));
    if (!Number.isSafeInteger(groupId)) {
      showToast("Выбери подключённую группу.", true);
      return;
    }
    try {
      const result = await apiRequest("/api/v1/games/rooms/invite-group", {
        method: "POST",
        body: { room_code: currentRoom.room_code, group_id: groupId },
      });
      showToast(result.message);
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
      document.querySelector(".app-shell").classList.remove("is-room-playing");
      document.body.classList.remove("is-room-playing");
      void exitRoomFullscreen().catch((error) => console.error("Не удалось выйти из полного экрана.", error));
      syncRoomScene(null, false);
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
        document.querySelector(".app-shell").classList.remove("is-room-playing");
        document.body.classList.remove("is-room-playing");
        void exitRoomFullscreen().catch((error) => console.error("Не удалось выйти из полного экрана.", error));
        syncRoomScene(null, false);
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
  document.getElementById("room-close").addEventListener("click", async () => {
    if (!currentRoom) return;
    const confirmText = currentRoom.is_owner
      ? "Закрыть комнату и завершить текущую сессию?"
      : "Закрыть комнату как администратор?";
    const shouldClose = window.Telegram?.WebApp?.showConfirm
      ? await new Promise((resolve) => {
          window.Telegram.WebApp.showConfirm(confirmText, resolve);
        })
      : window.confirm(confirmText);
    if (!shouldClose) return;
    try {
      const result = await apiRequest("/api/v1/games/rooms/close", {
        method: "POST",
        body: { room_code: currentRoom.room_code },
      });
      showToast(result.message || "Комната закрыта.");
      currentRoom = null;
      document.querySelector(".app-shell").classList.remove("is-room-playing");
      document.body.classList.remove("is-room-playing");
      void exitRoomFullscreen().catch((error) => console.error("Не удалось выйти из полного экрана.", error));
      syncRoomScene(null, false);
      window.clearInterval(roomClockTimer);
      document.getElementById("room-view").hidden = true;
      document.getElementById("room-lobby").hidden = false;
      await loadRooms();
    } catch (error) {
      showToast(error.message, true);
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
  document.getElementById("room-join-form").addEventListener("submit", () => {
    unlockGameAudio();
    void enterRoomFullscreen();
  }, { capture: true });
  document.getElementById("room-fullscreen").addEventListener("click", async () => {
    try {
      if (window.Telegram?.WebApp?.isFullscreen || document.fullscreenElement) {
        await exitRoomFullscreen();
      } else {
        await enterRoomFullscreen(true);
      }
    } catch (error) {
      showToast("Не удалось изменить режим экрана.", true);
      console.error("Не удалось изменить полноэкранный режим.", error);
    }
    updateFullscreenButton();
  });
  document.getElementById("room-sound").addEventListener("click", () => {
    const enabled = !isGameSoundEnabled();
    setGameSoundEnabled(enabled);
    updateSoundButton();
    if (enabled) {
      void unlockGameAudio().then(() => playGameSound("join"));
    }
  });
  document.getElementById("card-table").addEventListener("pointerdown", unlockGameAudio);
  document.addEventListener("fullscreenchange", updateFullscreenButton);
  window.Telegram?.WebApp?.onEvent?.("fullscreenChanged", updateFullscreenButton);
  updateSoundButton();
  updateFullscreenButton();
}
