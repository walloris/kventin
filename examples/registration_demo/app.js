const form = document.querySelector("#registration-form");
const statusNode = document.querySelector("#form-status");
const submitButton = form.querySelector("button[type='submit']");

function showStatus(message, kind) {
  statusNode.textContent = message;
  statusNode.className = `form-status ${kind || ""}`.trim();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showStatus("", "");

  if (!form.reportValidity()) {
    showStatus("Проверьте обязательные поля.", "error");
    return;
  }

  const payload = {
    fullName: form.elements.fullName.value.trim(),
    email: form.elements.email.value.trim(),
    password: form.elements.password.value,
    terms: form.elements.terms.checked,
  };

  submitButton.disabled = true;
  submitButton.textContent = "Создаём аккаунт…";

  try {
    const response = await fetch("/api/register", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) {
      showStatus(body.message || "Не удалось создать аккаунт.", "error");
      return;
    }
    form.reset();
    showStatus(`Аккаунт ${body.email} создан.`, "success");
  } catch (error) {
    showStatus("Сервис регистрации недоступен.", "error");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Зарегистрироваться";
  }
});
