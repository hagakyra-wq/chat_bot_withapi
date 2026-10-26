import { apiRequest, showToast } from "./api.js";
import { initGames, joinRoomByCode, loadRooms, setGamesPageActive } from "./games.js";

const webApp = window.Telegram?.WebApp;
const pageTitles = {
  home: "Игры",
  rooms: "Игровая комната",
  tarot: "Скоро",
};
let activePage = "home";

function showPage(pageName) {
  if (!pageTitles[pageName]) return;
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
  setGamesPageActive(pageName === "rooms");
  if (webApp?.BackButton) {
    if (pageName === "home") webApp.BackButton.hide();
    else webApp.BackButton.show();
  }

  const loaders = {
    rooms: loadRooms,
  };
  const loader = loaders[pageName];
  if (loader) loader().catch((error) => showToast(error.message, true));
}

async function connectTelegram() {
  const accountName = document.getElementById("account-name");
  const accountRole = document.getElementById("account-role");
  try {
    webApp?.ready();
    for (let attempt = 0; webApp && !webApp.initData && attempt < 10; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 150));
    }
    if (!webApp?.initData) {
      throw new Error(
        "Telegram не передал данные авторизации. Закрой страницу и открой Mini App кнопкой внутри Telegram.",
      );
    }
    const session = await apiRequest("/api/session", {
      method: "POST",
      body: { initData: webApp.initData },
    });
    window.userRole = session.role || "user";
    accountName.textContent = session.first_name || `ID ${session.user_id}`;
    accountRole.textContent = window.userRole === "admin" ? "Администратор" : "Игровой профиль";
    const startParam = webApp.initDataUnsafe?.start_param || "";
    const queryRoom = new URLSearchParams(window.location.search).get("room") || "";
    const roomMatch = /^room_([A-HJ-NP-Z2-9]{6})$/i.exec(startParam)
      || /^([A-HJ-NP-Z2-9]{6})$/i.exec(queryRoom);
    if (roomMatch) {
      showPage("rooms");
      await joinRoomByCode(roomMatch[1]);
    }
  } catch (error) {
    accountName.textContent = "Не удалось проверить";
    accountRole.textContent = "Открой Mini App из Telegram";
    showToast(error.message, true);
  }
}

webApp?.expand();
initGames();
document.querySelectorAll("[data-page]").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    showPage(button.dataset.page);
  });
});
webApp?.BackButton?.onClick(() => showPage("home"));
connectTelegram();
