import { apiRequest, formatCoins, showToast } from "./api.js";

export function initActivation() {
  const form = document.getElementById("activation-form");
  const codeInput = document.getElementById("activation-code");
  const message = document.getElementById("activation-message");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.textContent = "";
    message.className = "form-message";
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const result = await apiRequest("/api/v1/activation", {
        method: "POST",
        body: { code: codeInput.value.trim() },
      });
      let text = result.message || "Код применён.";
      if (Number.isInteger(result.delta_cents)) {
        const sign = result.delta_cents < 0 ? "−" : "+";
        text += ` Баланс изменён на ${sign}${formatCoins(Math.abs(result.delta_cents))}.`;
      }
      message.textContent = text;
      message.classList.add("is-success");
      codeInput.value = "";
      showToast("Код успешно применён.");
      window.dispatchEvent(new Event("miniapp:market-updated"));
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
    } finally {
      submit.disabled = false;
    }
  });
}
