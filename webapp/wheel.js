import { apiRequest, formatCoinAmount, formatCoins, showToast } from "./api.js";

const rarityColors = {
  1: "#8e80c2",
  2: "#6e9eb0",
  3: "#d69a73",
  4: "#a78ece",
  5: "#c98273",
  6: "#f2d28b",
};

let wheelState;
let selectedSpins = 1;
let wheelAngle = 0;
let spinning = false;
let initialized = false;

function updateWheelControls() {
  if (!wheelState) return;
  const cost = wheelState.spin_cost_cents * selectedSpins;
  document.getElementById("wheel-spin").textContent = `Крутить за ${formatCoins(cost)}`;
  document.getElementById("wheel-spin").disabled =
    spinning || wheelState.balance_cents < cost;
}

function renderRarities(rarities) {
  const totalWeight = rarities.reduce((total, rarity) => total + rarity.weight, 0);
  let angle = 0;
  const segments = rarities.map((rarity) => {
    const start = angle;
    angle += 360 * rarity.weight / totalWeight;
    return `${rarityColors[rarity.level]} ${start}deg ${angle}deg`;
  });
  document.getElementById("wheel-disc").style.backgroundImage =
    `conic-gradient(from -90deg, ${segments.join(", ")})`;

  const list = document.getElementById("wheel-odds-list");
  list.replaceChildren();
  for (const rarity of rarities) {
    const row = document.createElement("div");
    row.className = "wheel-odds-row";
    row.style.setProperty("--rarity-color", rarityColors[rarity.level]);
    const dot = document.createElement("span");
    dot.className = "wheel-odds-dot";
    const name = document.createElement("span");
    name.textContent = rarity.name;
    const odds = document.createElement("strong");
    odds.textContent = `${Number(rarity.percentage).toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}%`;
    row.append(dot, name, odds);
    list.append(row);
  }
}

export async function loadWheel() {
  const dashboard = await apiRequest("/api/v1/casino");
  wheelState = dashboard;
  document.getElementById("wheel-balance").textContent =
    formatCoinAmount(dashboard.balance_cents);
  document.getElementById("wheel-single-cost").textContent =
    formatCoins(dashboard.spin_cost_cents);
  document.getElementById("wheel-ten-cost").textContent =
    formatCoins(dashboard.spin_cost_cents * 10);
  renderRarities(dashboard.rarities);
  document.getElementById("wheel-status").textContent =
    "Выбери крутку и узнай, какой баннер выпадет.";
  updateWheelControls();
}

function spinToLevel(level, duration) {
  const index = wheelState.rarities.findIndex((rarity) => rarity.level === level);
  const totalWeight = wheelState.rarities.reduce(
    (total, rarity) => total + rarity.weight,
    0,
  );
  const startWeight = wheelState.rarities
    .slice(0, index)
    .reduce((total, rarity) => total + rarity.weight, 0);
  const selectedRarity = wheelState.rarities[index];
  const startAngle = 360 * startWeight / totalWeight;
  const segmentAngle = 360 * selectedRarity.weight / totalWeight;
  const landingAngle = startAngle + segmentAngle * (0.2 + Math.random() * 0.6);
  const targetAngle = (360 - landingAngle) % 360;
  const currentAngle = ((wheelAngle % 360) + 360) % 360;
  const alignment = (targetAngle - currentAngle + 360) % 360;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fullTurns = reducedMotion ? 0 : selectedSpins === 1 ? 7 : 4;
  wheelAngle += fullTurns * 360 + alignment;

  const disc = document.getElementById("wheel-disc");
  disc.style.transitionDuration = `${reducedMotion ? 0 : duration}ms`;
  disc.style.transform = `rotate(${wheelAngle}deg)`;
  if (reducedMotion) return Promise.resolve();

  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, duration + 250);
    disc.addEventListener("transitionend", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function buildResultCard(result, index) {
  const card = document.createElement("article");
  card.className = "wheel-result";
  card.style.setProperty("--rarity-color", rarityColors[result.level]);
  card.style.animationDelay = `${Math.min(index * 35, 270)}ms`;
  const mark = document.createElement("span");
  mark.className = "wheel-result-mark";
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/assets/icons.svg#fortune");
  icon.append(use);
  mark.append(icon);
  const info = document.createElement("div");
  info.className = "wheel-result-info";
  const rarity = document.createElement("strong");
  rarity.textContent = result.rarity;
  const dice = document.createElement("span");
  dice.textContent = `Кубики: ${result.values.join(" · ")}`;
  info.append(rarity, dice);
  const itemId = document.createElement("span");
  itemId.className = "wheel-result-id";
  itemId.textContent = `#${result.item_id}`;
  card.append(mark, info, itemId);
  return card;
}

async function spin() {
  if (spinning || !wheelState) return;
  const cost = wheelState.spin_cost_cents * selectedSpins;
  if (wheelState.balance_cents < cost) {
    showToast("Недостаточно монет для выбранного числа круток.", true);
    return;
  }

  spinning = true;
  updateWheelControls();
  const machine = document.getElementById("wheel-machine");
  const status = document.getElementById("wheel-status");
  const results = document.getElementById("wheel-results");
  machine.classList.add("is-pending");
  results.replaceChildren();
  status.textContent = "Колесо набирает скорость…";
  try {
    const round = await apiRequest("/api/v1/casino/spin", {
      method: "POST",
      body: { spins: selectedSpins },
    });
    wheelState.balance_cents = round.balance_cents;
    document.getElementById("wheel-balance").textContent =
      formatCoinAmount(round.balance_cents);
    machine.classList.remove("is-pending");

    for (const [index, result] of round.results.entries()) {
      status.textContent = round.results.length > 1
        ? `Крутка ${index + 1} из ${round.results.length}`
        : "Колесо останавливается…";
      await spinToLevel(result.level, selectedSpins === 1 ? 3600 : 850);
      results.append(buildResultCard(result, index));
    }
    status.textContent = round.results.length === 1
      ? "Приз добавлен в инвентарь."
      : `Все ${round.results.length} призов добавлены в инвентарь.`;
    window.dispatchEvent(new Event("miniapp:casino-updated"));
  } catch (error) {
    machine.classList.remove("is-pending");
    status.textContent = "Крутка не завершена.";
    showToast(error.message, true);
  } finally {
    spinning = false;
    updateWheelControls();
  }
}

export function initWheel() {
  if (initialized) return;
  initialized = true;
  for (const option of document.querySelectorAll(".wheel-option")) {
    option.addEventListener("click", () => {
      selectedSpins = Number(option.dataset.spins);
      document.querySelectorAll(".wheel-option").forEach((candidate) => {
        candidate.classList.toggle("is-selected", candidate === option);
      });
      updateWheelControls();
    });
  }
  document.getElementById("wheel-spin").addEventListener("click", spin);
}
