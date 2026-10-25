import { apiRequest, formatCoinAmount } from "./api.js";

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
}
