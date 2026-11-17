document.addEventListener("DOMContentLoaded", () => {
  const statusBadge = document.getElementById("status-badge");
  const userInfoRow = document.getElementById("user-info-row");
  const userFullname = document.getElementById("user-fullname");
  const connectBlock = document.getElementById("connect-block");
  const connectedBlock = document.getElementById("connected-block");
  const tokenInput = document.getElementById("token-input");
  const btnConnect = document.getElementById("btn-connect");
  const btnSync = document.getElementById("btn-sync");
  const resultMsg = document.getElementById("result-msg");

  function showMessage(text, isError = false) {
    resultMsg.textContent = text;
    resultMsg.style.color = isError ? "#dc2626" : "#16a34a";
    resultMsg.style.display = "block";
  }

  function updateUI(status) {
    if (status && status.connected) {
      statusBadge.textContent = "Подключён";
      statusBadge.className = "status-badge status-connected";
      userInfoRow.style.display = "flex";
      userFullname.textContent = status.fullname || "Студент AITU";
      connectBlock.style.display = "none";
      connectedBlock.style.display = "block";
    } else {
      statusBadge.textContent = "Не подключён";
      statusBadge.className = "status-badge status-disconnected";
      userInfoRow.style.display = "none";
      connectBlock.style.display = "block";
      connectedBlock.style.display = "none";
    }
  }

  // Load status
  chrome.runtime.sendMessage({ action: "GET_STATUS" }, updateUI);

  // Connect button click
  btnConnect.addEventListener("click", () => {
    const entered = tokenInput.value.trim();
    const token = entered.match(/(?:#|[?&])tg_token=([A-Za-z0-9_-]+)/)?.[1] || entered;
    if (!token) {
      showMessage("Введите код подключения из бота", true);
      return;
    }
    btnConnect.disabled = true;
    btnConnect.textContent = "Подключение...";

    chrome.runtime.sendMessage({ action: "CONNECT", token }, (res) => {
      btnConnect.disabled = false;
      btnConnect.textContent = "Подключить к Telegram";

      if (res && res.ok) {
        showMessage(`Успешно! Привет, ${res.fullname}!`);
        chrome.runtime.sendMessage({ action: "GET_STATUS" }, updateUI);
      } else {
        showMessage(res ? res.error : "Ошибка подключения", true);
      }
    });
  });

  // Sync button click
  btnSync.addEventListener("click", () => {
    btnSync.disabled = true;
    btnSync.textContent = "Синхронизация...";

    chrome.runtime.sendMessage({ action: "SYNC" }, (res) => {
      btnSync.disabled = false;
      btnSync.textContent = "🔄 Обновить сессию сейчас";

      if (res && res.ok) {
        showMessage("Сессия успешно обновлена на сервере!");
      } else {
        showMessage("Не удалось обновить. Откройте lms.astanait.edu.kz", true);
      }
    });
  });
});
