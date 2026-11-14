// ==UserScript==
// @name         AITU Moodle Telegram Auto-Connect
// @namespace    https://lms.astanait.edu.kz/
// @version      1.0.0
// @description  Автоматическая синхронизация сессии Moodle AITU с Telegram-ботом
// @author       AITU Bot Team
// @match        https://lms.astanait.edu.kz/*
// @grant        GM_cookie
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      63.182.255.51
// ==/UserScript==

(function () {
  "use strict";

  const API_BASE = "http://63.182.255.51:8000";

  function getMoodleCookie() {
    return new Promise((resolve) => {
      if (typeof GM_cookie !== "undefined" && GM_cookie.get) {
        GM_cookie.get({ name: "MoodleSession" }, (cookies, error) => {
          if (!error && cookies && cookies.length > 0) {
            resolve(cookies[0].value);
          } else {
            resolve(null);
          }
        });
      } else {
        // Fallback to document.cookie (if not HttpOnly)
        const match = document.cookie.match(/MoodleSession=([^;]+)/);
        resolve(match ? match[1] : null);
      }
    });
  }

  function showBanner(text, type = "info") {
    let b = document.getElementById("aitu-user-script-banner");
    if (!b) {
      b = document.createElement("div");
      b.id = "aitu-user-script-banner";
      b.style.cssText =
        "position:fixed;top:15px;right:15px;z-index:999999;padding:12px 18px;border-radius:10px;font-family:sans-serif;font-size:14px;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,0.2);";
      document.body.appendChild(b);
    }
    b.style.background = type === "success" ? "#10b981" : type === "error" ? "#ef4444" : "#3b82f6";
    b.innerHTML = text;
    setTimeout(() => {
      b.style.opacity = "0";
      setTimeout(() => b.remove(), 500);
    }, 5000);
  }

  async function checkHashAndConnect() {
    const hash = window.location.hash || "";
    const match = hash.match(/#tg_token=([A-Za-z0-9_-]+)/);
    if (!match) return;

    const token = match[1];
    showBanner("⏳ Подключение Telegram-бота...");

    const cookie = await getMoodleCookie();
    if (!cookie) {
      showBanner("❌ MoodleSession не найдена. Войдите в Moodle под своим аккаунтом.", "error");
      return;
    }

    GM_xmlhttpRequest({
      method: "POST",
      url: `${API_BASE}/api/connect-session`,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ token: token, cookie: cookie }),
      onload: function (res) {
        try {
          const data = JSON.parse(res.responseText);
          if (data.ok) {
            GM_setValue("telegram_user_id", data.telegram_user_id);
            GM_setValue("sync_secret", data.sync_secret);
            showBanner(`✅ Telegram-бот успешно подключён! (Студент: ${data.fullname})`, "success");
            history.replaceState(null, "", window.location.pathname + window.location.search);
          } else {
            showBanner(`❌ Ошибка: ${data.detail || "не удалось подключить"}`, "error");
          }
        } catch (e) {
          showBanner("❌ Ошибка ответа сервера", "error");
        }
      },
      onerror: function () {
        showBanner("❌ Не удалось связаться с сервером бота", "error");
      }
    });
  }

  // Silent sync
  async function silentSync() {
    const uid = GM_getValue("telegram_user_id");
    const secret = GM_getValue("sync_secret");
    if (!uid || !secret) return;

    const cookie = await getMoodleCookie();
    if (!cookie) return;

    GM_xmlhttpRequest({
      method: "POST",
      url: `${API_BASE}/api/sync-session`,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ telegram_user_id: uid, sync_secret: secret, cookie: cookie })
    });
  }

  checkHashAndConnect();
  silentSync();
})();
