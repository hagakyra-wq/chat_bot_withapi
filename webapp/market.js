import { apiRequest, formatCoins, showToast } from "./api.js";

const rarityNames = {
  1: "Обычный",
  2: "Необычный",
  3: "Редкий",
  4: "Эпический",
  5: "Элитный",
  6: "Легендарный",
};

let currentPage = 0;
let latestResult;

function confirmPurchase(message) {
  const webApp = window.Telegram?.WebApp;
  if (!webApp?.showConfirm) return Promise.resolve(window.confirm(message));
  return new Promise((resolve) => webApp.showConfirm(message, resolve));
}

function formatLotCount(count) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  const noun = mod100 >= 11 && mod100 <= 14
    ? "лотов"
    : mod10 === 1
      ? "лот"
      : mod10 >= 2 && mod10 <= 4
        ? "лота"
        : "лотов";
  return `${Number(count).toLocaleString("ru-RU")} ${noun}`;
}

function emptyState(text) {
  const state = document.createElement("div");
  state.className = "empty-state";
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/assets/icons.svg#market");
  icon.append(use);
  const label = document.createElement("span");
  label.textContent = text;
  state.append(icon, label);
  return state;
}

function buildLotCard(lot) {
  const card = document.createElement("article");
  card.className = `listing-card rarity-${lot.level}`;
  const art = document.createElement("div");
  art.className = "listing-art";
  if (lot.image_url) {
    art.classList.add("has-image");
    const image = document.createElement("img");
    image.className = "listing-image";
    image.src = lot.image_url;
    image.alt = lot.animated ? `Анимированный баннер ${titleFromLot(lot)}` : `Баннер ${titleFromLot(lot)}`;
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("error", () => {
      image.remove();
      art.classList.add("image-unavailable");
    }, { once: true });
    art.append(image);
  }
  const mark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/assets/icons.svg#mark");
  mark.append(use);
  art.append(mark);

  const content = document.createElement("div");
  content.className = "listing-info";
  const title = document.createElement("h3");
  title.className = "listing-title";
  title.textContent = lot.title || `${rarityNames[lot.level] || "Баннер"} · #${lot.item_id}`;
  const meta = document.createElement("div");
  meta.className = "listing-meta";
  const rarity = document.createElement("span");
  rarity.textContent = `${rarityNames[lot.level] || "Баннер"} · уровень ${lot.level}`;
  const price = document.createElement("strong");
  price.className = "listing-price";
  price.textContent = formatCoins(lot.price_cents);
  meta.append(rarity, price);

  const buy = document.createElement("button");
  buy.className = "button button-primary listing-buy";
  buy.type = "button";
  buy.textContent = "Купить баннер";
  buy.addEventListener("click", async () => {
    const accepted = await confirmPurchase(
      `Купить этот баннер за ${formatCoins(lot.price_cents)}?`,
    );
    if (!accepted) return;
    buy.disabled = true;
    try {
      const result = await apiRequest("/api/v1/market/buy", {
        method: "POST",
        body: { lot_id: lot.lot_id },
      });
      showToast(result.message || "Покупка завершена.");
      await loadMarket();
      window.dispatchEvent(new Event("miniapp:market-updated"));
    } catch (error) {
      showToast(error.message, true);
      buy.disabled = false;
    }
  });

  content.append(title, meta, buy);
  card.append(art, content);
  return card;
}

function titleFromLot(lot) {
  return lot.title || `${rarityNames[lot.level] || "Баннер"} #${lot.item_id}`;
}

function renderMarket(result) {
  latestResult = result;
  const listings = document.getElementById("market-listings");
  listings.replaceChildren();
  if (!result.lots.length) {
    listings.append(emptyState("По этим условиям лотов пока нет."));
  } else {
    for (const lot of result.lots) listings.append(buildLotCard(lot));
  }

  document.getElementById("market-count").textContent =
    formatLotCount(Number(result.total));
  document.getElementById("market-page-label").textContent = `Страница ${result.page + 1}`;
  document.getElementById("market-prev").disabled = result.page <= 0;
  document.getElementById("market-next").disabled =
    (result.page + 1) * result.page_size >= result.total;

  const myLots = document.getElementById("my-lots");
  myLots.replaceChildren();
  if (!result.my_lots.length) {
    myLots.append(emptyState("У тебя пока нет открытых объявлений."));
  } else {
    for (const lot of result.my_lots) {
      const row = document.createElement("div");
      row.className = "my-lot";
      const info = document.createElement("div");
      info.className = "my-lot-info";
      const title = document.createElement("strong");
      title.textContent = lot.title || `${rarityNames[lot.level] || "Баннер"} · #${lot.item_id}`;
      const level = document.createElement("span");
      level.textContent = `${rarityNames[lot.level] || "Баннер"} · лот #${lot.lot_id}`;
      const price = document.createElement("span");
      price.textContent = formatCoins(lot.price_cents);
      info.append(title, level);
      const actions = document.createElement("div");
      actions.className = "my-lot-actions";
      actions.append(price);
      const withdraw = document.createElement("button");
      withdraw.className = "revoke-button";
      withdraw.type = "button";
      withdraw.dataset.lotId = String(lot.lot_id);
      withdraw.textContent = "Снять";
      actions.append(withdraw);
      row.append(info, actions);
      myLots.append(row);
    }
  }

  const itemSelect = document.getElementById("market-item-select");
  const availableItems = result.inventory.filter(
    (item) => !item.is_active && !item.is_transfer_pending && item.source !== "owner",
  );
  itemSelect.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = availableItems.length ? "Выбери баннер" : "Нет доступных баннеров";
  itemSelect.append(placeholder);
  itemSelect.disabled = availableItems.length === 0;
  for (const item of availableItems) {
    const option = document.createElement("option");
    option.value = String(item.item_id);
    option.textContent = `${rarityNames[item.level] || "Баннер"} · #${item.item_id}`;
    itemSelect.append(option);
  }
}

export async function loadMarket() {
  const form = document.getElementById("market-filters");
  const values = new FormData(form);
  const params = new URLSearchParams({
    q: String(values.get("q") || ""),
    level: String(values.get("level") || "all"),
    sort: String(values.get("sort") || "date"),
    page: String(currentPage),
  });
  const result = await apiRequest(`/api/v1/market?${params}`);
  renderMarket(result);
}

export function initMarket() {
  document.getElementById("market-filters").addEventListener("submit", async (event) => {
    event.preventDefault();
    currentPage = 0;
    try {
      await loadMarket();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.getElementById("market-prev").addEventListener("click", async () => {
    currentPage = Math.max(0, currentPage - 1);
    try {
      await loadMarket();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.getElementById("market-next").addEventListener("click", async () => {
    if (!latestResult || (currentPage + 1) * latestResult.page_size >= latestResult.total) return;
    currentPage += 1;
    try {
      await loadMarket();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.getElementById("market-list-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const priceText = String(values.get("price") || "").trim();
    const priceMatch = /^(\d{1,10})(?:\.(\d{1,2}))?$/.exec(priceText);
    if (!priceMatch) {
      showToast("Укажи цену с точностью до копейки, например 25.50.", true);
      return;
    }
    const priceCents = Number(priceMatch[1]) * 100 + Number((priceMatch[2] || "").padEnd(2, "0"));
    if (!Number.isSafeInteger(priceCents) || priceCents <= 0) {
      showToast("Цена должна быть больше нуля.", true);
      return;
    }
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const response = await apiRequest("/api/v1/market/list", {
        method: "POST",
        body: {
          item_id: Number(values.get("item_id")),
          price_cents: priceCents,
          title: String(values.get("title") || "").trim() || null,
        },
      });
      showToast(response.message || "Лот выставлен.");
      form.reset();
      await loadMarket();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  document.getElementById("my-lots").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-lot-id]");
    if (!button) return;
    const lotId = Number(button.dataset.lotId);
    if (!await confirmPurchase(`Снять лот #${lotId} с продажи?`)) return;
    button.disabled = true;
    try {
      const response = await apiRequest("/api/v1/market/withdraw", {
        method: "POST",
        body: { lot_id: lotId },
      });
      showToast(response.message || "Лот снят.");
      await loadMarket();
    } catch (error) {
      showToast(error.message, true);
      button.disabled = false;
    }
  });
}
