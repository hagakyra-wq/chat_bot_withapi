import { apiRequest, formatCoins, showToast } from "./api.js";

const accessLabels = {
  user: "Пользовательский доступ",
  admin: "Администратор",
  autoresponder: "Автоответчик",
};

function renderKeys(keys) {
  const container = document.getElementById("admin-keys");
  container.replaceChildren();
  if (!keys.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Неиспользованных ключей нет.";
    container.append(empty);
    return;
  }

  for (const item of keys) {
    const row = document.createElement("div");
    row.className = "admin-key";
    const copy = document.createElement("div");
    copy.className = "admin-key-copy";
    const key = document.createElement("code");
    key.textContent = item.key;
    const level = document.createElement("span");
    const days = Number.isInteger(item.duration_seconds)
      ? ` · ${Math.round(item.duration_seconds / 86_400)} дн.`
      : " · без срока";
    level.textContent = `${accessLabels[item.access_level] || "Доступ"}${days}`;
    copy.append(key, level);

    const revoke = document.createElement("button");
    revoke.className = "revoke-button";
    revoke.type = "button";
    revoke.dataset.key = item.key;
    revoke.textContent = "Отозвать";
    row.append(copy, revoke);
    container.append(row);
  }
}

export async function loadAdmin() {
  const dashboard = await apiRequest("/api/v1/admin");
  document.getElementById("admin-count").textContent =
    Number(dashboard.admin_count).toLocaleString("ru-RU");
  document.getElementById("admin-fee").textContent = formatCoins(dashboard.market_fee_cents);
  renderKeys(dashboard.access_keys);
}

export function initAdmin() {
  document.getElementById("admin-key-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const rawDuration = String(values.get("duration_seconds"));
    const duration = rawDuration === "permanent" ? null : Number(rawDuration);
    const submit = form.querySelector("button[type=submit]");
    const output = document.getElementById("generated-key");
    submit.disabled = true;
    output.hidden = true;
    try {
      const created = await apiRequest("/api/v1/admin/access-keys", {
        method: "POST",
        body: {
          access_level: String(values.get("access_level")),
          duration_seconds: duration,
        },
      });
      output.textContent = `Новый ключ · скопируй и передай его лично: ${created.key}`;
      output.hidden = false;
      showToast("Одноразовый ключ создан.");
      await loadAdmin();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  document.getElementById("admin-refresh").addEventListener("click", async () => {
    try {
      await loadAdmin();
      showToast("Список обновлён.");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.getElementById("admin-keys").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-key]");
    if (!button) return;
    if (!window.confirm(`Отозвать ключ ${button.dataset.key}?`)) return;
    button.disabled = true;
    try {
      const result = await apiRequest("/api/v1/admin/access-keys/revoke", {
        method: "POST",
        body: { key: button.dataset.key },
      });
      showToast(result.message || "Ключ отозван.");
      await loadAdmin();
    } catch (error) {
      showToast(error.message, true);
      button.disabled = false;
    }
  });
}
