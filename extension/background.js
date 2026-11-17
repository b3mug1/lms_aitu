const API_BASE = "https://lmsaitu.vercel.app";

async function getMoodleCookie() {
  const cookie = await chrome.cookies.get({
    url: "https://lms.astanait.edu.kz",
    name: "MoodleSession"
  });
  return cookie ? cookie.value : null;
}

async function handleConnect(token) {
  const cookieVal = await getMoodleCookie();
  if (!cookieVal) {
    return {
      ok: false,
      error: "MoodleSession не найдена. Пожалуйста, сначала войдите в Moodle под своим аккаунтом."
    };
  }

  try {
    const res = await fetch(`${API_BASE}/api/connect-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token.trim(), cookie: cookieVal })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      return { ok: false, error: data.detail || data.error || "Ошибка подключения к серверу" };
    }

    // Save session in local storage for background auto-renewal
    await chrome.storage.local.set({
      connected: true,
      telegramUserId: data.telegram_user_id,
      syncSecret: data.sync_secret,
      fullname: data.fullname,
      lastSyncedAt: Date.now()
    });

    return { ok: true, fullname: data.fullname, coursesCount: data.courses_count };
  } catch (err) {
    return { ok: false, error: `Не удалось связаться с сервером бота: ${err.message}` };
  }
}

async function handleSilentSync() {
  const storage = await chrome.storage.local.get(["connected", "telegramUserId", "syncSecret"]);
  if (!storage.connected || !storage.telegramUserId || !storage.syncSecret) {
    return { ok: false, reason: "not_connected" };
  }

  const cookieVal = await getMoodleCookie();
  if (!cookieVal) {
    return { ok: false, reason: "no_cookie" };
  }

  try {
    const res = await fetch(`${API_BASE}/api/sync-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        telegram_user_id: storage.telegramUserId,
        sync_secret: storage.syncSecret,
        cookie: cookieVal
      })
    });
    if (res.ok) {
      await chrome.storage.local.set({ lastSyncedAt: Date.now() });
      return { ok: true };
    }
  } catch (err) {
    console.warn("[AITU Moodle Connect] Silent sync error:", err);
  }
  return { ok: false };
}

// Listen to messages from content.js and popup.js
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "CONNECT") {
    handleConnect(request.token).then(sendResponse);
    return true; // async response
  }
  if (request.action === "SYNC") {
    handleSilentSync().then(sendResponse);
    return true;
  }
  if (request.action === "GET_STATUS") {
    chrome.storage.local.get(["connected", "telegramUserId", "fullname", "lastSyncedAt"]).then(sendResponse);
    return true;
  }
});

// Auto-sync when MoodleSession cookie changes or expires
chrome.cookies.onChanged.addListener((changeInfo) => {
  if (
    changeInfo.cookie.name === "MoodleSession" &&
    changeInfo.cookie.domain.includes("astanait.edu.kz") &&
    !changeInfo.removed
  ) {
    handleSilentSync();
  }
});
