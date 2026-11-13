# Moodle LMS → Telegram Notifier (`lms.astanait.edu.kz`)

Персональный сервис на Python (`httpx` + `BeautifulSoup4` + `aiogram` + `SQLite`), который регулярно проверяет ваш аккаунт Moodle ([`https://lms.astanait.edu.kz`](https://lms.astanait.edu.kz)) и отправляет уведомления в Telegram-бот:

- **Выставлена или обновлена оценка** (балл, максимум, процент, комментарий преподавателя)
- **Добавлено новое задание / тест / активность**
- **Изменён дедлайн** существующего задания
- **Напоминания о дедлайнах**: **за 5 дней, 3 дня, 2 дня, 1 день, 12 часов и 3 часа** (без дублей и с пропуском уже сданных работ)

## Особенности `lms.astanait.edu.kz`

На сервере `lms.astanait.edu.kz`:
- внешние эндпоинты `/login/token.php` и `/webservice/rest/server.php` закрыты `403 Forbidden`, поэтому сервис работает через **веб-сессию (`MoodleWebClient`: внутренний AJAX + HTML-парсинг через `BeautifulSoup4`)**;
- поддерживаются два способа входа:
  1. **Через Microsoft / OpenID Connect (`/auth/oidc/`)** — используйте `MOODLE_AUTH_MODE=cookie` и вставьте значение cookie `MoodleSession` в `MOODLE_SESSION_COOKIE` (или обновляйте его прямо в Telegram-боте командой `/setcookie <значение>`). Таймаут неактивной сессии на сайте — 25 минут (`sessiontimeout: 1500`), поэтому опрос каждые 5 минут (`POLL_INTERVAL_SECONDS=300`) автоматически поддерживает сессию живой пока сервис запущен.
  2. **Через логин и пароль Moodle** (если для вашей учётной записи включён локальный вход на `/login/index.php`) — используйте `MOODLE_AUTH_MODE=web` (или `auto`) с `MOODLE_USERNAME` и `MOODLE_PASSWORD`.

## Быстрый старт (Windows PowerShell)

1. Перейдите в каталог проекта и скопируйте `.env.example` в `.env`:
   ```powershell
   cd C:\Users\Пользователь\.gemini\antigravity\scratch\moodle_tg_notifier
   Copy-Item .env.example .env
   ```
2. Заполните в `.env`:
   - `TELEGRAM_BOT_TOKEN` (получить у `@BotFather`)
   - `TELEGRAM_CHAT_ID` (узнать у `@userinfobot` или написать `/start` вашему боту)
   - `MOODLE_BASE_URL=https://lms.astanait.edu.kz` (уже прописано)
   - Либо `MOODLE_AUTH_MODE=cookie` + `MOODLE_SESSION_COOKIE` (из браузера: `F12` → `Application` → `Cookies` → `https://lms.astanait.edu.kz` → `MoodleSession`), либо `MOODLE_AUTH_MODE=web` + `MOODLE_USERNAME` + `MOODLE_PASSWORD`.
3. Запустите сервис:
   ```powershell
   .\.venv\Scripts\python.exe -m app.main
   ```
   Разовая проверка без запуска фонового демона:
   ```powershell
   .\.venv\Scripts\python.exe -m app.main --once
   ```

## Команды Telegram-бота

После `/start` бот показывает постоянное меню: «Сроки сдачи», «Предметы», «Последние оценки», «Проверить Moodle», «Состояние» и «Помощь». Все основные действия доступны без ввода команд.

«Последние оценки» группирует результаты по предметам. Регистры и нулевые записи без даты оценки или комментария скрыты из этой сводки, но доступны в карточке предмета. Время обновления оценки меняется только при изменении результата; время проверки Moodle не выдаётся за дату выставления оценки.

- `/check` — проверить Moodle прямо сейчас
- `/deadlines` — ближайшие невыполненные дедлайны (`/deadlines all` — вместе со сданными)
- `/grades` — выбрать курс и посмотреть его оценки и задания (`/grades recent` — последние оценки списком)
- `/courses` — выбрать курс и открыть его оценки и задания
- `/setcookie <MoodleSession>` — обновить cookie сессии при входе через OpenID Connect без перезапуска
- `/status` — статус синхронизации и активный режим
- `/help` — список команд

## Запуск тестов

```powershell
.\.venv\Scripts\pytest.exe -v
```

## Continuous SQLite run on Windows

The local process uses the SQLite database in `data/lms_notifier.db` and polls Moodle according to `POLL_INTERVAL_SECONDS` (300 seconds by default). Start it with:

```powershell
.\.venv\Scripts\python.exe -m app.main
```

Polling removes any Telegram webhook without dropping queued updates. Do not run the local poller and the Vercel webhook against the same bot token at the same time. To keep the bot available, Windows must remain powered on, awake, and connected to the internet. Configure the command in Windows Task Scheduler to start at user logon.

Vercel's serverless functions require persistent PostgreSQL. Its local filesystem is not suitable for this SQLite database; use the local poller or another host with persistent disk when keeping SQLite.
