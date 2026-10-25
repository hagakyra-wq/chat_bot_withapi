import { apiRequest, showToast } from "./api.js";
import { initActivation } from "./activation.js";
import { initAdmin, loadAdmin } from "./admin.js";
import { initMarket, loadMarket } from "./market.js";
import { loadProfile } from "./profile.js";

const webApp = window.Telegram?.WebApp;
const pageTitles = {
  home: "Главная",
  profile: "Профиль",
  market: "Площадка",
  activation: "Активация",
  admin: "Управление",
};

let isAdmin = false;
let activePage = "home";
let marketInitialized = false;
let activationInitialized = false;
let adminInitialized = false;

function showPage(pageName) {
  if (!pageTitles[pageName] || (pageName === "admin" && !isAdmin)) return;
  activePage = pageName;
  for (const [name] of Object.entries(pageTitles)) {
    const page = document.getElementById(`page-${name}`);
    page.hidden = name !== pageName;
    page.classList.toggle("is-visible", name === pageName);
  }
  document.getElementById("page-heading").textContent = pageTitles[pageName];
  document.querySelectorAll("[data-page]").forEach((button) => {
    const active = button.dataset.page === pageName;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (webApp?.BackButton) {
    if (pageName === "home") webApp.BackButton.hide();
    else webApp.BackButton.show();
  }

  const loaders = {
    profile: loadProfile,
    market: loadMarket,
    admin: loadAdmin,
  };
  const loader = loaders[pageName];
  if (loader) {
    loader().catch((error) => showToast(error.message, true));
  }
}

async function connectTelegram() {
  const accountName = document.getElementById("account-name");
  const accountRole = document.getElementById("account-role");
  if (!webApp?.initData) {
    accountName.textContent = "Открой через Telegram";
    accountRole.textContent = "Аккаунт не подтверждён";
    return;
  }

  try {
    const session = await apiRequest("/api/session", {
      method: "POST",
      body: { initData: webApp.initData },
    });
    accountName.textContent = session.first_name || `ID ${session.user_id}`;
    isAdmin = session.role === "admin";
    accountRole.textContent = isAdmin ? "Администратор" : "Пользователь";
    if (isAdmin) {
      document.querySelectorAll(".admin-nav, .admin-card").forEach((item) => {
        item.hidden = false;
      });
      initAdmin();
      adminInitialized = true;
    }
  } catch (error) {
    accountName.textContent = "Не удалось проверить";
    accountRole.textContent = "Открой Mini App из Telegram";
    showToast(error.message, true);
  }
}

function initNavigation() {
  document.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      const pageName = button.dataset.page;
      if (pageName === "market" && !marketInitialized) {
        initMarket();
        marketInitialized = true;
      }
      if (pageName === "activation" && !activationInitialized) {
        initActivation();
        activationInitialized = true;
      }
      if (pageName === "admin" && (!isAdmin || !adminInitialized)) return;
      showPage(pageName);
    });
  });
  webApp?.BackButton?.onClick(() => showPage("home"));
}

webApp?.ready();
webApp?.expand();
initNavigation();
connectTelegram();
window.addEventListener("miniapp:market-updated", () => {
  if (activePage === "profile") loadProfile().catch((error) => showToast(error.message, true));
});
