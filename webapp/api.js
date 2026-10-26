export async function apiRequest(path, options = {}) {
  const webApp = window.Telegram?.WebApp;
  if (!webApp?.initData) {
    throw new Error(
      "Telegram не передал данные авторизации. Закрой страницу и открой Mini App кнопкой в Telegram.",
    );
  }

  const headers = new Headers(options.headers || {});
  headers.set("X-Telegram-Init-Data", webApp.initData);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");

  const response = await fetch(path, {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || data.message || `Ошибка сервера: ${response.status}`);
  }
  return data;
}

export function formatCoinAmount(cents) {
  return (Number(cents) / 100).toLocaleString("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

export function formatCoins(cents) {
  return `${formatCoinAmount(cents)} монет`;
}

let toastTimer;

export function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.add("is-visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 3200);
}
