import { apiRequest, formatCoinAmount, showToast } from "./api.js";

const rarityNames = {
  1: "Обычный",
  2: "Необычный",
  3: "Редкий",
  4: "Эпический",
  5: "Элитный",
  6: "Легендарный",
};

let inventoryItems = [];
let inventoryIndex = 0;

function formatBannerCount(count) {
  const lastTwo = count % 100;
  const last = count % 10;
  const noun = lastTwo >= 11 && lastTwo <= 14
    ? "баннеров"
    : last === 1
      ? "баннер"
      : last >= 2 && last <= 4
        ? "баннера"
        : "баннеров";
  return `${count.toLocaleString("ru-RU")} ${noun}`;
}

function renderInventory() {
  const art = document.getElementById("inventory-art");
  const title = document.getElementById("inventory-title");
  const status = document.getElementById("inventory-status");
  const description = document.getElementById("inventory-description");
  const position = document.getElementById("inventory-position");
  const activate = document.getElementById("inventory-activate");
  const previous = document.getElementById("inventory-prev");
  const next = document.getElementById("inventory-next");
  const count = document.getElementById("inventory-count");
  const level = document.getElementById("inventory-level");
  const item = inventoryItems[inventoryIndex];

  count.textContent = formatBannerCount(inventoryItems.length);
  art.querySelector("img")?.remove();
  art.classList.remove("image-unavailable", "rarity-1", "rarity-2", "rarity-3", "rarity-4", "rarity-5", "rarity-6");
  if (!item) {
    title.textContent = "Коллекция пока пуста";
    status.textContent = "";
    description.textContent = "Полученные и купленные баннеры появятся здесь.";
    position.textContent = "0 / 0";
    level.textContent = "";
    previous.disabled = true;
    next.disabled = true;
    activate.disabled = true;
    return;
  }

  art.classList.add(`rarity-${item.level}`);
  const image = document.createElement("img");
  image.src = item.image_url;
  image.alt = item.animated
    ? `Анимированный баннер: ${rarityNames[item.level]}`
    : `Баннер: ${rarityNames[item.level]}`;
  image.decoding = "async";
  image.addEventListener("error", () => {
    image.remove();
    art.classList.add("image-unavailable");
  }, { once: true });
  art.prepend(image);
  title.textContent = `${rarityNames[item.level] || "Баннер"} · #${item.item_id}`;
  status.textContent = item.is_active ? "Активный" : "";
  description.textContent = item.animated
    ? "Легендарный баннер с анимацией."
    : `Баннер ${rarityNames[item.level]?.toLowerCase() || ""} уровня ${item.level}.`;
  position.textContent = `${inventoryIndex + 1} / ${inventoryItems.length}`;
  level.textContent = `УРОВЕНЬ ${item.level}`;
  previous.disabled = inventoryIndex === 0;
  next.disabled = inventoryIndex === inventoryItems.length - 1;
  activate.disabled = item.is_active || item.is_transfer_pending;
  activate.textContent = item.is_active ? "Уже активен" : "Сделать активным";
}

function addDetail(container, label, value) {
  const cell = document.createElement("div");
  cell.className = "detail-cell";
  const title = document.createElement("span");
  title.textContent = label;
  const text = document.createElement("strong");
  text.textContent = value || "Не указано";
  cell.append(title, text);
  container.append(cell);
}

export async function loadProfile() {
  const profile = await apiRequest("/api/v1/profile");
  const previousItemId = inventoryItems[inventoryIndex]?.item_id;
  inventoryItems = profile.inventory || [];
  const previousIndex = inventoryItems.findIndex((item) => item.item_id === previousItemId);
  inventoryIndex = previousIndex >= 0
    ? previousIndex
    : Math.max(0, inventoryItems.findIndex((item) => item.is_active));
  if (inventoryIndex < 0) inventoryIndex = 0;
  document.getElementById("profile-name").textContent =
    profile.callsign || profile.display_name || "Профиль";
  document.getElementById("profile-username").textContent =
    profile.username ? `@${profile.username.replace(/^@/, "")}` : `ID ${profile.user_id}`;
  document.getElementById("profile-balance").textContent =
    formatCoinAmount(profile.balance_cents);

  const details = document.getElementById("profile-details");
  details.replaceChildren();
  addDetail(details, "Возраст", profile.age ? `${profile.age}` : "");
  addDetail(details, "Пол", profile.gender === "неизвестен" ? "" : profile.gender);
  const notes = String(profile.notes || "").split("\n").map((note) => note.trim()).filter(Boolean);
  addDetail(details, "Факты", notes.length ? `${notes.length}` : "Пока нет");
  const notesPanel = document.getElementById("profile-notes");
  const notesList = document.getElementById("profile-note-list");
  notesList.replaceChildren();
  notesPanel.hidden = notes.length === 0;
  for (const note of notes) {
    const item = document.createElement("li");
    item.textContent = note;
    notesList.append(item);
  }

  const activity = document.getElementById("profile-activity");
  activity.replaceChildren();
  for (const [period, label] of [["d", "Сегодня"], ["w", "За неделю"], ["m", "За месяц"]]) {
    const card = document.createElement("article");
    card.className = "panel stat-card";
    const title = document.createElement("span");
    title.className = "stat-label";
    title.textContent = label;
    const value = document.createElement("strong");
    value.className = "stat-value";
    value.textContent = Number(profile.activity?.[period] || 0).toLocaleString("ru-RU");
    card.append(title, value);
    activity.append(card);
  }
  renderInventory();
}

function initInventory() {
  document.getElementById("inventory-prev").addEventListener("click", () => {
    inventoryIndex = Math.max(0, inventoryIndex - 1);
    renderInventory();
  });
  document.getElementById("inventory-next").addEventListener("click", () => {
    inventoryIndex = Math.min(inventoryItems.length - 1, inventoryIndex + 1);
    renderInventory();
  });
  document.getElementById("inventory-activate").addEventListener("click", async (event) => {
    const item = inventoryItems[inventoryIndex];
    if (!item || item.is_active || item.is_transfer_pending) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await apiRequest("/api/v1/inventory/activate", {
        method: "POST",
        body: { item_id: item.item_id },
      });
      showToast(result.message || "Баннер установлен.");
      await loadProfile();
    } catch (error) {
      showToast(error.message, true);
      button.disabled = false;
    }
  });
}

initInventory();
