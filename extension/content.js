(function () {
  function showBanner(text, type = "info", autoHideMs = 0) {
    let banner = document.getElementById("aitu-moodle-connect-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "aitu-moodle-connect-banner";
      document.body.appendChild(banner);
    }
    banner.className = `aitu-banner aitu-banner-${type}`;
    banner.textContent = text;
    banner.style.display = "block";

    if (autoHideMs > 0) {
      setTimeout(() => {
        banner.style.opacity = "0";
        setTimeout(() => {
          if (banner.parentNode) banner.parentNode.removeChild(banner);
        }, 500);
      }, autoHideMs);
    }
  }

  // Check URL hash for token
  const hash = window.location.hash || "";
  const match = hash.match(/#tg_token=([A-Za-z0-9_-]+)/);

  if (match) {
    const token = match[1];
    showBanner("Подключение Telegram-бота к вашему Moodle...", "info");

    chrome.runtime.sendMessage({ action: "CONNECT", token: token }, (response) => {
      if (response && response.ok) {
        showBanner(
          `Telegram-бот успешно подключён! Профиль: ${response.fullname} (курсов: ${response.coursesCount}).`,
          "success",
          6000
        );
        // Clear token from URL without reloading
        try {
          history.replaceState(null, "", window.location.pathname + window.location.search);
        } catch (e) {}
      } else {
        const err = response ? response.error : "Неизвестная ошибка";
        showBanner(`Не удалось подключить Telegram-бота: ${err}`, "error", 8000);
      }
    });
  } else {
    // If user is simply browsing Moodle, perform silent background sync
    chrome.runtime.sendMessage({ action: "SYNC" }, () => {});
  }
})();
