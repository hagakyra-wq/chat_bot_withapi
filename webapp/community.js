import { apiRequest, formatCoins, showToast } from "./api.js";

const periodLabels = { d: "Сегодня", w: "За неделю", m: "За месяц" };
let initialized = false;
let leaderboardRequest = 0;

function confirmAction(message) {
  const webApp = window.Telegram?.WebApp;
  if (!webApp?.showConfirm) return Promise.resolve(window.confirm(message));
  return new Promise((resolve) => webApp.showConfirm(message, resolve));
}

function renderLeaderboard(entries, mode) {
  const list = document.getElementById("community-ranking");
  list.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement("li");
    empty.className = "community-empty";
    empty.textContent = "За выбранный период данных пока нет.";
    list.append(empty);
    return;
  }

  for (const [index, entry] of entries.entries()) {
    const row = document.createElement("li");
    row.className = "community-rank-row";
    const rank = document.createElement("span");
    rank.className = "community-rank-number";
    rank.textContent = String(index + 1).padStart(2, "0");
    const name = document.createElement("strong");
    name.className = "community-rank-name";
    name.textContent = entry.display_name;
    const value = document.createElement("span");
    value.className = "community-rank-value";
    value.textContent = mode === "coins"
      ? formatCoins(entry.value)
      : `${Number(entry.value).toLocaleString("ru-RU")} сообщ.`;
    row.append(rank, name, value);
    list.append(row);
  }
}

async function loadGroups() {
  const select = document.getElementById("community-group");
  const { groups } = await apiRequest("/api/v1/community/groups");
  select.replaceChildren();
  if (!groups.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Нет подключённых групп";
    select.append(option);
    select.disabled = true;
    document.getElementById("community-load").disabled = true;
    document.getElementById("community-message").textContent =
      "Групповой рейтинг появится после участия в группе с Юбарой.";
    return;
  }

  for (const group of groups) {
    const option = document.createElement("option");
    option.value = String(group.group_id);
    option.textContent = group.title;
    select.append(option);
  }
  select.disabled = false;
  document.getElementById("community-load").disabled = false;
  await loadLeaderboard();
}

async function loadLeaderboard() {
  const groupId = document.getElementById("community-group").value;
  if (!groupId) return;
  const requestId = ++leaderboardRequest;
  const mode = document.getElementById("community-mode").value;
  const period = document.getElementById("community-period").value;
  const query = new URLSearchParams({ group_id: groupId, mode, period });
  const message = document.getElementById("community-message");
  const button = document.getElementById("community-load");
  button.disabled = true;
  message.textContent = "Загружаем рейтинг…";
  try {
    const result = await apiRequest(`/api/v1/community/leaderboard?${query}`);
    if (requestId !== leaderboardRequest) return;
    renderLeaderboard(result.entries, result.mode);
    message.textContent = result.mode === "coins"
      ? "Рейтинг монет участников группы."
      : `Активность · ${periodLabels[result.period]}.`;
  } catch (error) {
    if (requestId !== leaderboardRequest) return;
    document.getElementById("community-ranking").replaceChildren();
    message.textContent = error.message;
    showToast(error.message, true);
  } finally {
    if (requestId === leaderboardRequest) button.disabled = false;
  }
}

function initFeedback() {
  const form = document.getElementById("community-feedback-form");
  const input = document.getElementById("community-feedback-text");
  const counter = document.getElementById("community-feedback-count");
  const message = document.getElementById("community-feedback-message");
  input.addEventListener("input", () => {
    counter.textContent = `${input.value.length} / 3000`;
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    message.textContent = "";
    message.className = "form-message";
    try {
      const result = await apiRequest("/api/v1/feedback", {
        method: "POST",
        body: { message: input.value.trim() },
      });
      message.textContent = result.message;
      message.classList.add("is-success");
      form.reset();
      counter.textContent = "0 / 3000";
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
    } finally {
      button.disabled = false;
    }
  });
}

function initImageGeneration() {
  const form = document.getElementById("community-image-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("community-image-prompt");
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const result = await apiRequest("/api/v1/image", {
        method: "POST",
        body: { prompt: input.value.trim() },
      });
      showToast(result.message);
      input.value = "";
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

function initPrivacy() {
  document.getElementById("community-forget").addEventListener("click", async (event) => {
    const accepted = await confirmAction(
      "Удалить сохранённые профильные данные и счётчики сообщений во всех группах? Это действие нельзя отменить. Архив чата, баланс и баннеры останутся.",
    );
    if (!accepted) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await apiRequest("/api/v1/privacy/forget", {
        method: "POST",
        body: { confirmed: true },
      });
      showToast(result.message);
      window.dispatchEvent(new Event("miniapp:profile-updated"));
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

export function initCommunity() {
  if (initialized) return;
  initialized = true;
  document.getElementById("community-load").addEventListener("click", () => {
    loadLeaderboard().catch((error) => showToast(error.message, true));
  });
  document.getElementById("community-refresh").addEventListener("click", () => {
    loadLeaderboard().catch((error) => showToast(error.message, true));
  });
  document.getElementById("community-group").addEventListener("change", () => {
    loadLeaderboard().catch((error) => showToast(error.message, true));
  });
  document.getElementById("community-mode").addEventListener("change", (event) => {
    document.getElementById("community-period-field").hidden =
      event.currentTarget.value === "coins";
    loadLeaderboard().catch((error) => showToast(error.message, true));
  });
  document.getElementById("community-period").addEventListener("change", () => {
    loadLeaderboard().catch((error) => showToast(error.message, true));
  });
  initFeedback();
  initImageGeneration();
  initPrivacy();
  loadGroups().catch((error) => {
    document.getElementById("community-message").textContent = error.message;
    showToast(error.message, true);
  });
}
