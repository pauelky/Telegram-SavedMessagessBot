from .shared import *
from .state import *

def html_escape(s: Optional[str]) -> str:
    """Lightweight safe HTML escape used for building messages shown to users.
    Uses html.escape but ensures None -> empty string.
    """
    if s is None:
        return ""
    # html.escape covers & < > and quotes when requested; keep behavior conservative
    return html.escape(str(s), quote=False)


# ----------------------------
# AI Assistant / FastAPI helpers
# ----------------------------

AI_APP_HOST = os.getenv("AI_APP_HOST", "0.0.0.0")
AI_APP_PORT = int(os.getenv("AI_APP_PORT", "8000"))
AI_WEBAPP_URL = os.getenv("AI_WEBAPP_URL", f"http://localhost:{AI_APP_PORT}/miniapp")
AI_MAX_RESULT_ROWS = int(os.getenv("AI_MAX_RESULT_ROWS", "40"))
AI_MAX_ANSWER_CHARS = int(os.getenv("AI_MAX_ANSWER_CHARS", "700"))
AI_MAX_QUESTION_CHARS = int(os.getenv("AI_MAX_QUESTION_CHARS", "700"))
AI_ALLOW_LOCAL_USER_ID = os.getenv("AI_ALLOW_LOCAL_USER_ID", "0") == "1"
AI_INITDATA_MAX_AGE_SEC = int(os.getenv("AI_INITDATA_MAX_AGE_SEC", "86400"))
AI_OPENROUTER_RETRIES = int(os.getenv("AI_OPENROUTER_RETRIES", "3"))
AI_AVATAR_CACHE_TTL_SEC = int(os.getenv("AI_AVATAR_CACHE_TTL_SEC", "900"))
AI_AVATAR_MAX_BYTES = int(os.getenv("AI_AVATAR_MAX_BYTES", "2097152"))
AI_ALLOWED_TABLES = {"messages", "deleted_messages"}
AI_FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "ATTACH",
    "DETACH",
    "REPLACE",
    "PRAGMA",
}
AI_SCHEMA_DESCRIPTION = (
    "messages(id INTEGER, user_id INTEGER, chat_id INTEGER, text TEXT, date TEXT); "
    "deleted_messages(id INTEGER, user_id INTEGER, chat_id INTEGER, text TEXT, date TEXT, "
    "chat_title TEXT, sender_username TEXT, content_type TEXT)"
)


def _is_https_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme.lower() == "https" and bool(parsed.netloc)
    except Exception:
        return False


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def build_start_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📱 По номеру", callback_data="auth_phone"),
            InlineKeyboardButton("🗝️ По QR-коду", callback_data="auth_qr"),
        ]
    ]

    if _is_https_url(AI_WEBAPP_URL):
        rows.append([InlineKeyboardButton("🤖 AI-ассистент", web_app=WebAppInfo(url=AI_WEBAPP_URL))])
    else:
        logger.warning("AI_WEBAPP_URL=%r is not a public HTTPS URL; AI button is disabled in /start", AI_WEBAPP_URL)

    return InlineKeyboardMarkup(rows)
AI_SYSTEM_PROMPT = (
    "Ты — генератор SQL-запросов для SQLite. "
    f"В базе доступны только таблицы: {AI_SCHEMA_DESCRIPTION}. "
    "Можно использовать только SELECT-запросы. "
    "Запрещены INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/DETACH/PRAGMA/REPLACE. "
    "Данные в таблицах уже отфильтрованы только под текущего пользователя. "
    "Выдавай только один рабочий SQL-запрос без пояснений и markdown. "
    "Используй LIMIT, если подходит по задаче."
)
AI_RESULT_PROMPT = (
    "Ты помощник для обычного пользователя Telegram. "
    "Объясни результат простым языком без технических терминов и без упоминания SQL. "
    "Если данных нет, так и скажи. Дай короткий, полезный вывод."
)
MINIAPP_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Message Control Center</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #050805;
      --bg-soft: #091109;
      --surface: rgba(11, 19, 12, 0.94);
      --surface-strong: rgba(14, 24, 15, 0.98);
      --line: rgba(63, 119, 72, 0.34);
      --line-strong: rgba(82, 180, 100, 0.44);
      --text: #ebffee;
      --muted: #8da794;
      --accent: #43ff7e;
      --accent-soft: rgba(67, 255, 126, 0.12);
      --danger: #ff5f7d;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.38);
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      color: var(--text);
      font-family: "Inter", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(67,255,126,0.12), transparent 32%),
        radial-gradient(circle at top right, rgba(67,255,126,0.08), transparent 28%),
        linear-gradient(180deg, #040704 0%, #060a06 55%, #091109 100%);
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image: linear-gradient(rgba(67,255,126,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(67,255,126,0.03) 1px, transparent 1px);
      background-size: 28px 28px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.45), transparent 92%);
    }
    .shell {
      width: min(1160px, calc(100% - 24px));
      margin: 0 auto;
      padding: 18px 0 40px;
      display: grid;
      gap: 14px;
    }
    .panel {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 22px;
      box-shadow: inset 0 0 0 1px rgba(67,255,126,0.04), var(--shadow);
      backdrop-filter: blur(14px);
    }
    .hero {
      padding: 20px;
      display: grid;
      gap: 14px;
    }
    .hero-top {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .hero h1 {
      margin: 0 0 8px;
      font-family: "Share Tech Mono", monospace;
      color: var(--accent);
      font-size: clamp(1.25rem, 4vw, 1.8rem);
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .hero p {
      margin: 0;
      max-width: 720px;
      color: var(--muted);
      line-height: 1.55;
    }
    .badge-row, .action-row, .quick-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .badge, .chip {
      border: 1px solid var(--line);
      background: rgba(8, 15, 9, 0.84);
      color: #c3e7cb;
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 0.86rem;
      line-height: 1;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
      gap: 14px;
    }
    .stack {
      display: grid;
      gap: 14px;
    }
    .card {
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      flex-wrap: wrap;
    }
    .eyebrow {
      margin: 0 0 6px;
      color: var(--muted);
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.14em;
    }
    .card h2 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
    }
    .muted {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }
    .profile {
      display: grid;
      gap: 18px;
      align-content: start;
      position: sticky;
      top: 12px;
    }
    .profile-head {
      display: grid;
      grid-template-columns: 76px minmax(0, 1fr);
      gap: 14px;
      align-items: center;
    }
    .avatar {
      width: 76px;
      height: 76px;
      border-radius: 22px;
      border: 1px solid var(--line-strong);
      background: linear-gradient(180deg, rgba(67,255,126,0.18), rgba(67,255,126,0.06));
      display: grid;
      place-items: center;
      overflow: hidden;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.04);
    }
    .avatar img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: none;
    }
    .avatar-fallback {
      font-family: "Share Tech Mono", monospace;
      font-size: 1.45rem;
      color: var(--accent);
      letter-spacing: 0.08em;
    }
    .profile-name {
      margin: 0;
      font-size: 1.18rem;
      font-weight: 700;
      line-height: 1.25;
    }
    .profile-username {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 0.94rem;
    }
    .profile-status {
      display: grid;
      gap: 10px;
    }
    .status-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .status-tile, .stat {
      border: 1px solid var(--line);
      background: var(--surface-strong);
      border-radius: 16px;
      padding: 12px 14px;
    }
    .status-label, .stat-label {
      color: var(--muted);
      font-size: 0.8rem;
      margin-bottom: 8px;
    }
    .status-value {
      color: var(--text);
      font-size: 0.95rem;
      font-weight: 600;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .stat-value {
      font-family: "Share Tech Mono", monospace;
      font-size: 1.35rem;
      color: var(--accent);
      font-weight: 700;
    }
    .stat-note {
      margin-top: 8px;
      color: #b3ceb8;
      font-size: 0.84rem;
    }
    .assistant-area {
      display: grid;
      gap: 12px;
    }
    textarea {
      width: 100%;
      min-height: 132px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(7, 13, 8, 0.92);
      color: var(--text);
      font: inherit;
      padding: 14px 15px;
      resize: vertical;
      outline: none;
      transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
    }
    textarea:focus {
      border-color: var(--line-strong);
      box-shadow: 0 0 0 3px rgba(67,255,126,0.10);
      transform: translateY(-1px);
    }
    button {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(9, 16, 10, 0.95);
      color: var(--text);
      font: inherit;
      font-weight: 600;
      padding: 11px 14px;
      cursor: pointer;
      transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease, color 0.18s ease;
    }
    button:hover { border-color: var(--line-strong); }
    button:active { transform: translateY(1px); }
    button:disabled { opacity: 0.65; cursor: not-allowed; transform: none; }
    .btn-primary {
      background: linear-gradient(180deg, rgba(18, 43, 24, 0.95), rgba(11, 28, 15, 0.98));
      color: var(--accent);
      border-color: rgba(67,255,126,0.42);
    }
    .btn-primary:hover {
      background: linear-gradient(180deg, rgba(21, 51, 28, 0.98), rgba(14, 34, 18, 1));
    }
    .btn-secondary {
      color: #c8e7ce;
    }
    .btn-danger {
      background: linear-gradient(180deg, rgba(41, 12, 19, 0.95), rgba(28, 8, 13, 0.98));
      border-color: rgba(255, 95, 125, 0.28);
      color: #ff9ab0;
    }
    .chip {
      cursor: pointer;
      background: rgba(12, 21, 13, 0.92);
      padding: 10px 14px;
    }
    .chip:hover {
      border-color: var(--line-strong);
      color: var(--accent);
    }
    .result, .notice {
      border: 1px solid var(--line);
      background: rgba(7, 12, 7, 0.92);
      border-radius: 18px;
      padding: 14px 15px;
      line-height: 1.6;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .notice {
      color: #cbe9d1;
    }
    .notice.danger {
      border-color: rgba(255, 95, 125, 0.28);
      background: rgba(38, 10, 16, 0.72);
      color: #ffc0cd;
    }
    .notice.success {
      border-color: rgba(67,255,126,0.30);
      background: rgba(10, 28, 14, 0.74);
      color: #cbffd8;
    }
    details {
      border: 1px dashed var(--line);
      background: rgba(7, 12, 7, 0.78);
      border-radius: 16px;
      padding: 10px 12px;
    }
    summary {
      cursor: pointer;
      color: #a9d8b2;
      user-select: none;
      font-size: 0.92rem;
    }
    pre {
      margin: 10px 0 0;
      padding: 12px;
      border-radius: 14px;
      background: #040804;
      border: 1px solid rgba(47, 79, 53, 0.42);
      max-height: 260px;
      overflow: auto;
      font-size: 0.82rem;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .footer-note {
      color: var(--muted);
      font-size: 0.82rem;
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      .profile { position: static; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 580px) {
      .shell { width: min(100% - 16px, 100%); }
      .hero, .card { padding: 16px; }
      .status-grid, .stats-grid { grid-template-columns: 1fr; }
      .profile-head { grid-template-columns: 64px minmax(0, 1fr); }
      .avatar { width: 64px; height: 64px; border-radius: 18px; }
      .action-row button { width: 100%; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel hero">
      <div class="hero-top">
        <div>
          <h1>Message Control Center</h1>
          <p>Личный центр управления архивом сообщений. Здесь можно посмотреть свою статистику, задать вопрос ассистенту простыми словами и безопасно завершить текущую сессию.</p>
        </div>
        <div class="badge-row">
          <span class="badge" id="identity-pill">Пользователь не определен</span>
          <span class="badge">Показ строк в деталях: {{MAX_ROWS}}</span>
        </div>
      </div>
    </section>

    <section class="layout">
      <aside class="panel card profile">
        <div>
          <p class="eyebrow">Профиль</p>
          <div class="profile-head">
            <div class="avatar">
              <img id="profile-avatar" alt="Аватар пользователя" />
              <span class="avatar-fallback" id="profile-avatar-fallback">U</span>
            </div>
            <div>
              <h2 class="profile-name" id="profile-name">Загрузка профиля...</h2>
              <p class="profile-username" id="profile-username">@username</p>
            </div>
          </div>
        </div>

        <div class="profile-status">
          <div class="status-grid">
            <div class="status-tile">
              <div class="status-label">ID пользователя</div>
              <div class="status-value" id="profile-id">—</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Источник входа</div>
              <div class="status-value" id="profile-source">Telegram Mini App</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Сессия</div>
              <div class="status-value" id="profile-session">Проверяется...</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Watcher</div>
              <div class="status-value" id="profile-watcher">Проверяется...</div>
            </div>
          </div>
          <div class="notice" id="session-result">Состояние сессии будет показано после проверки.</div>
        </div>

        <div class="action-row">
          <button id="refresh-overview" class="btn-secondary" type="button">Обновить данные</button>
          <button id="logout-session" class="btn-danger" type="button">Завершить сессию</button>
        </div>
        <p class="footer-note">Завершение сессии остановит watcher и удалит текущий session-файл, после чего потребуется повторная авторизация.</p>
      </aside>

      <div class="stack">
        <section class="panel card">
          <div class="card-head">
            <div>
              <p class="eyebrow">Общая статистика</p>
              <h2>Ваш архив</h2>
            </div>
            <div class="badge-row">
              <span class="badge" id="top-chat-badge">Топ-чат: нет данных</span>
              <span class="badge" id="last-event-badge">Последнее событие: нет данных</span>
            </div>
          </div>
          <div class="stats-grid">
            <div class="stat">
              <div class="stat-label">Удалено всего</div>
              <div class="stat-value" id="stat-total-deleted">0</div>
              <div class="stat-note">Все удаленные сообщения, найденные watcher'ом.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Удалено сегодня</div>
              <div class="stat-value" id="stat-today-deleted">0</div>
              <div class="stat-note">Активность за текущие сутки.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Сообщений в архиве</div>
              <div class="stat-value" id="stat-total-messages">0</div>
              <div class="stat-note">Сохраненные оригиналы сообщений.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Лидер по удалениям</div>
              <div class="stat-value" id="stat-top-chat-count">0</div>
              <div class="stat-note" id="stat-top-chat-name">Нет данных</div>
            </div>
          </div>
          <div class="notice success" id="overview-summary">Загружаю статистику...</div>
        </section>

        <section class="panel card">
          <div class="card-head">
            <div>
              <p class="eyebrow">AI-помощник</p>
              <h2>Спросите обычным языком</h2>
            </div>
            <div class="badge-row">
              <span class="badge">Ответ без технического жаргона</span>
            </div>
          </div>

          <div class="quick-grid">
            <button class="chip" type="button" data-question="Кто удалил больше всего сообщений сегодня?">Кто удалял сегодня больше всего</button>
            <button class="chip" type="button" data-question="В каком чате у меня больше всего удаленных сообщений?">Какой чат самый активный</button>
            <button class="chip" type="button" data-question="Сколько у меня удалений было вчера?">Сколько было вчера</button>
            <button class="chip" type="button" data-question="Покажи последние 5 удаленных сообщений.">Последние 5 удалений</button>
          </div>

          <div class="assistant-area">
            <textarea id="question" placeholder="Например: кто чаще всего удаляет сообщения в моем архиве за неделю?" aria-label="Вопрос к AI-ассистенту"></textarea>
            <div class="action-row">
              <button id="ask-button" class="btn-primary" type="button">Получить ответ</button>
            </div>
            <div class="result" id="answer">Задайте вопрос, и я объясню результат простыми словами.</div>
            <div class="notice" id="status-text">Готов к работе.</div>
            <details>
              <summary>Технические детали для отладки</summary>
              <pre id="sql-output"></pre>
              <pre id="data-output">[]</pre>
            </details>
          </div>
        </section>
      </div>
    </section>
  </main>

  <script>
    const maxRows = {{MAX_ROWS}};
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) {
      tg.ready();
      tg.expand();
      if (typeof tg.enableClosingConfirmation === "function") {
        tg.enableClosingConfirmation();
      }
    }

    const identityPill = document.getElementById("identity-pill");
    const profileNameEl = document.getElementById("profile-name");
    const profileUsernameEl = document.getElementById("profile-username");
    const profileIdEl = document.getElementById("profile-id");
    const profileSourceEl = document.getElementById("profile-source");
    const profileSessionEl = document.getElementById("profile-session");
    const profileWatcherEl = document.getElementById("profile-watcher");
    const profileAvatarEl = document.getElementById("profile-avatar");
    const profileAvatarFallbackEl = document.getElementById("profile-avatar-fallback");
    const totalDeletedEl = document.getElementById("stat-total-deleted");
    const todayDeletedEl = document.getElementById("stat-today-deleted");
    const totalMessagesEl = document.getElementById("stat-total-messages");
    const topChatCountEl = document.getElementById("stat-top-chat-count");
    const topChatNameEl = document.getElementById("stat-top-chat-name");
    const topChatBadgeEl = document.getElementById("top-chat-badge");
    const lastEventBadgeEl = document.getElementById("last-event-badge");
    const overviewSummaryEl = document.getElementById("overview-summary");
    const questionEl = document.getElementById("question");
    const answerEl = document.getElementById("answer");
    const sqlEl = document.getElementById("sql-output");
    const dataEl = document.getElementById("data-output");
    const statusText = document.getElementById("status-text");
    const sessionResultEl = document.getElementById("session-result");
    const askBtn = document.getElementById("ask-button");
    const refreshOverviewBtn = document.getElementById("refresh-overview");
    const logoutSessionBtn = document.getElementById("logout-session");
    const quickQuestionButtons = Array.from(document.querySelectorAll("[data-question]"));

    const state = {
      identity: null,
      avatarBlobUrl: null,
    };

    const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

    const renderStatus = (text, kind = "") => {
      statusText.textContent = text || "";
      statusText.className = kind ? `notice ${kind}` : "notice";
    };

    const renderRows = (rows) => {
      dataEl.textContent = rows && rows.length ? JSON.stringify(rows, null, 2) : "[]";
    };

    const decodeMaybe = (value) => {
      if (!value) {
        return "";
      }
      try {
        return value.includes("%") ? decodeURIComponent(value) : value;
      } catch (error) {
        return value;
      }
    };

    const readLaunchParam = (name) => {
      const sources = [
        new URLSearchParams(window.location.search),
        new URLSearchParams(window.location.hash.startsWith("#") ? window.location.hash.slice(1) : ""),
      ];
      for (const source of sources) {
        const value = source.get(name);
        if (value) {
          return value;
        }
      }
      return "";
    };

    const parseInitData = (initData) => {
      const parsed = {};
      if (!initData) {
        return parsed;
      }
      const params = new URLSearchParams(initData);
      params.forEach((value, key) => {
        parsed[key] = value;
      });
      if (parsed.user) {
        try {
          parsed.user = JSON.parse(parsed.user);
        } catch (error) {
          parsed.user = null;
        }
      }
      return parsed;
    };

    const getTelegramContext = () => {
      const rawInitData = tg && typeof tg.initData === "string" && tg.initData.trim()
        ? tg.initData.trim()
        : readLaunchParam("tgWebAppData");
      const initData = rawInitData || "";
      const parsed = parseInitData(initData);
      const user = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || parsed.user || null;
      return { initData, parsed, user };
    };

    const buildIdentityPayload = () => {
      const context = getTelegramContext();
      const user = context.user || {};
      return {
        init_data: context.initData || "",
        user_id: user.id || null,
        first_name: user.first_name || "",
        last_name: user.last_name || "",
        username: user.username || "",
        photo_url: user.photo_url || "",
        language_code: user.language_code || "",
      };
    };

    const waitForIdentityPayload = async () => {
      for (let attempt = 0; attempt < 12; attempt += 1) {
        const payload = buildIdentityPayload();
        if (payload.init_data || payload.user_id) {
          state.identity = payload;
          return payload;
        }
        await sleep(250);
      }
      const fallbackPayload = buildIdentityPayload();
      state.identity = fallbackPayload;
      return fallbackPayload;
    };

    const displayNameFromProfile = (profile) => {
      if (!profile) {
        return "Пользователь";
      }
      return profile.display_name || [profile.first_name, profile.last_name].filter(Boolean).join(" ").trim() || (profile.username ? `@${profile.username}` : "Пользователь");
    };

    const renderAvatar = async (profile, identityPayload) => {
      const initials = (profile && profile.initials) || "U";
      profileAvatarFallbackEl.textContent = initials;
      profileAvatarFallbackEl.style.display = "grid";
      profileAvatarEl.style.display = "none";
      profileAvatarEl.removeAttribute("src");

      if (state.avatarBlobUrl) {
        URL.revokeObjectURL(state.avatarBlobUrl);
        state.avatarBlobUrl = null;
      }

      if (profile && profile.photo_url) {
        profileAvatarEl.src = profile.photo_url;
        profileAvatarEl.style.display = "block";
        profileAvatarFallbackEl.style.display = "none";
        return;
      }

      try {
        const response = await fetch("/ai/profile/avatar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(identityPayload),
        });
        if (!response.ok) {
          return;
        }
        const blob = await response.blob();
        if (!blob || !blob.size) {
          return;
        }
        state.avatarBlobUrl = URL.createObjectURL(blob);
        profileAvatarEl.src = state.avatarBlobUrl;
        profileAvatarEl.style.display = "block";
        profileAvatarFallbackEl.style.display = "none";
      } catch (error) {
        console.warn("Avatar load failed", error);
      }
    };

    const renderProfile = async (profile, meta = {}) => {
      const identityPayload = state.identity || buildIdentityPayload();
      const name = displayNameFromProfile(profile);
      const username = profile && profile.username ? `@${profile.username}` : "username не указан";
      profileNameEl.textContent = name;
      profileUsernameEl.textContent = username;
      profileIdEl.textContent = profile && profile.user_id ? String(profile.user_id) : "—";
      profileSourceEl.textContent = profile && profile.source === "local" ? "Локальный режим" : "Telegram Mini App";
      profileSessionEl.textContent = meta.session_active ? "Активна" : "Не активна";
      profileWatcherEl.textContent = meta.watcher_active ? "Подключен" : "Не подключен";
      identityPill.textContent = profile && profile.user_id ? `${name} · ID ${profile.user_id}` : "Пользователь не определен";
      await renderAvatar(profile, identityPayload);
    };

    const postJson = async (url, payload) => {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const parsed = await response.json().catch(() => ({ detail: `Ошибка ${response.status}` }));
      if (!response.ok) {
        throw new Error(parsed.detail || `Ошибка ${response.status}`);
      }
      return parsed;
    };

    const refreshOverview = async () => {
      refreshOverviewBtn.disabled = true;
      try {
        const identityPayload = await waitForIdentityPayload();
        const payload = await postJson("/ai/overview", identityPayload);
        totalDeletedEl.textContent = payload.total_deleted || 0;
        todayDeletedEl.textContent = payload.deleted_today || 0;
        totalMessagesEl.textContent = payload.total_messages || 0;
        topChatCountEl.textContent = payload.top_chat?.count || 0;
        topChatNameEl.textContent = payload.top_chat?.name || "Нет данных";
        topChatBadgeEl.textContent = `Топ-чат: ${payload.top_chat?.name || "нет данных"}`;
        lastEventBadgeEl.textContent = `Последнее событие: ${payload.last_event || "нет данных"}`;
        overviewSummaryEl.textContent = payload.summary || "Статистика обновлена.";
        overviewSummaryEl.className = "notice success";
        sessionResultEl.textContent = payload.session_active
          ? "Сессия активна. Watcher готов обрабатывать события."
          : "Сессия не активна. Для сбора сообщений потребуется авторизация.";
        sessionResultEl.className = payload.session_active ? "notice success" : "notice";
        await renderProfile(payload.profile || {}, payload);
      } catch (error) {
        overviewSummaryEl.textContent = error?.message || "Не удалось загрузить статистику.";
        overviewSummaryEl.className = "notice danger";
        renderStatus("Не удалось определить пользователя. Откройте центр через кнопку внутри Telegram.", "danger");
      } finally {
        refreshOverviewBtn.disabled = false;
      }
    };

    const askAssistant = async () => {
      const question = questionEl.value.trim();
      if (!question) {
        renderStatus("Введите вопрос для ассистента.", "danger");
        return;
      }
      askBtn.disabled = true;
      renderStatus("Готовлю ответ и анализирую архив...");
      sqlEl.textContent = "";
      dataEl.textContent = "[]";
      answerEl.textContent = "Обрабатываю запрос...";
      try {
        const identityPayload = await waitForIdentityPayload();
        const payload = await postJson("/ai", { question, ...identityPayload });
        answerEl.textContent = payload.answer || "Нет ответа.";
        sqlEl.textContent = payload.sql || "";
        renderRows(payload.result?.rows ?? []);
        renderStatus(
          payload.result?.truncated
            ? `Ответ готов. Для технических деталей показаны только первые ${maxRows} строк.`
            : "Ответ готов.",
          "success"
        );
      } catch (error) {
        answerEl.textContent = "Не удалось получить ответ.";
        renderStatus(error?.message || "Ошибка при выполнении запроса.", "danger");
      } finally {
        askBtn.disabled = false;
      }
    };

    const logoutSession = async () => {
      if (!window.confirm("Завершить текущую сессию? После этого потребуется авторизоваться заново.")) {
        return;
      }
      logoutSessionBtn.disabled = true;
      try {
        const identityPayload = await waitForIdentityPayload();
        const payload = await postJson("/ai/session/logout", identityPayload);
        sessionResultEl.textContent = payload.message || "Сессия завершена.";
        sessionResultEl.className = payload.session_closed ? "notice success" : "notice danger";
        await refreshOverview();
      } catch (error) {
        sessionResultEl.textContent = error?.message || "Не удалось завершить сессию.";
        sessionResultEl.className = "notice danger";
      } finally {
        logoutSessionBtn.disabled = false;
      }
    };

    askBtn.addEventListener("click", askAssistant);
    refreshOverviewBtn.addEventListener("click", refreshOverview);
    logoutSessionBtn.addEventListener("click", logoutSession);
    questionEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        askAssistant();
      }
    });
    quickQuestionButtons.forEach((button) => {
      button.addEventListener("click", () => {
        questionEl.value = button.dataset.question || "";
        questionEl.focus();
      });
    });

    refreshOverview();
  </script>
</body>
</html>
"""
MINIAPP_HTML = MINIAPP_TEMPLATE.replace("{{MAX_ROWS}}", str(AI_MAX_RESULT_ROWS))
ai_app = FastAPI(title="Saved Delete Messages — AI assistant", version="1.0")
ai_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

BOT_RUNTIME_APP: Optional[Any] = None
BOT_RUNTIME_LOOP: Optional[asyncio.AbstractEventLoop] = None
AI_AVATAR_CACHE: Dict[int, Dict[str, Any]] = {}


@dataclass(frozen=True)
class AIIdentityContext:
    user_id: int
    profile: Dict[str, Any]
    source: str


class AIIdentityPayload(BaseModel):
    init_data: str = ""
    user_id: Optional[int] = None
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    language_code: str = ""


class AIQuestionPayload(AIIdentityPayload):
    question: str = Field(..., min_length=1, max_length=AI_MAX_QUESTION_CHARS)


class AIQueryResult(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    limit: int


class AIQueryResponse(BaseModel):
    answer: str
    sql: str
    result: AIQueryResult


class AIUserProfile(BaseModel):
    user_id: int
    display_name: str
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    initials: str = "U"
    language_code: str = ""
    source: str = "telegram"
    is_premium: bool = False


class AIOverviewResponse(BaseModel):
    profile: AIUserProfile
    total_deleted: int
    deleted_today: int
    total_messages: int
    top_chat: Dict[str, Any]
    last_event: str
    session_active: bool
    watcher_active: bool
    summary: str


class AISessionLogoutResponse(BaseModel):
    message: str
    session_closed: bool
    file_removed: bool = False
    watcher_stopped: bool = False
    state_reset: bool = False


def _normalize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(value).decode("ascii")
    return value


def _short_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def _clean_profile_text(value: Any, max_len: int = 120) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    return _short_text(text, max_len) if text else ""


def _clean_username(value: Any) -> str:
    username = _clean_profile_text(value, 64).lstrip("@")
    return re.sub(r"[^0-9A-Za-z_]", "", username)


def _clean_photo_url(value: Any) -> str:
    url = _clean_profile_text(value, 600)
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return ""


def _build_profile_payload(user_id: int, source: str, user_data: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    user_data = user_data or {}
    payload = payload or {}
    first_name = _clean_profile_text(user_data.get("first_name") or payload.get("first_name"), 80)
    last_name = _clean_profile_text(user_data.get("last_name") or payload.get("last_name"), 80)
    username = _clean_username(user_data.get("username") or payload.get("username"))
    language_code = _clean_profile_text(user_data.get("language_code") or payload.get("language_code"), 16)
    photo_url = _clean_photo_url(user_data.get("photo_url") or payload.get("photo_url"))

    display_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not display_name and username:
        display_name = f"@{username}"
    if not display_name:
        display_name = f"Пользователь {user_id}"

    if first_name and last_name:
        initials = (first_name[:1] + last_name[:1]).upper()
    else:
        letters = re.findall(r"[A-Za-zА-Яа-яЁё0-9]", first_name or username or str(user_id))
        initials = "".join(letters[:2]).upper() if letters else "U"

    return {
        "user_id": int(user_id),
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "photo_url": photo_url,
        "initials": initials or "U",
        "language_code": language_code,
        "source": source,
        "is_premium": bool(user_data.get("is_premium")),
    }


def _format_event_time(value: Any) -> str:
    if not value:
        return "нет данных"
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        event_dt = datetime.fromisoformat(raw)
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        local_dt = event_dt.astimezone(CONFIG.tz)
        return local_dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return _short_text(str(value), 80)


def _verify_telegram_init_data(init_data: str) -> Tuple[Dict[str, str], Dict[str, Any]]:
    import hashlib
    import hmac
    from urllib.parse import parse_qsl

    if not CONFIG.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен.")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    init_hash = parsed.pop("hash", None)
    if not init_hash:
        raise HTTPException(status_code=401, detail="Некорректный Telegram initData.")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData",
        CONFIG.bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, init_hash):
        raise HTTPException(status_code=401, detail="Подпись Mini App не прошла проверку.")

    auth_date_raw = parsed.get("auth_date")
    if auth_date_raw:
        try:
            auth_date = int(auth_date_raw)
            if abs(int(time.time()) - auth_date) > AI_INITDATA_MAX_AGE_SEC:
                raise HTTPException(status_code=401, detail="Сессия Mini App устарела. Откройте приложение заново.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Некорректный auth_date в initData.")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Пользователь не найден в initData.")

    try:
        user_data = json.loads(user_raw)
        user_id = int(user_data.get("id"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Некорректный профиль пользователя.") from exc

    if user_id <= 0:
        raise HTTPException(status_code=401, detail="Некорректный идентификатор пользователя.")

    return parsed, user_data


def _resolve_identity(payload: Dict[str, Any]) -> AIIdentityContext:
    init_data = str(payload.get("init_data") or "").strip()
    if init_data:
        _, user_data = _verify_telegram_init_data(init_data)
        user_id = int(user_data["id"])
        return AIIdentityContext(
            user_id=user_id,
            profile=_build_profile_payload(user_id, source="telegram", user_data=user_data),
            source="telegram",
        )

    local_user_id = payload.get("user_id")
    if AI_ALLOW_LOCAL_USER_ID and local_user_id is not None:
        try:
            user_id = int(local_user_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный user_id.") from exc
        if user_id <= 0:
            raise HTTPException(status_code=400, detail="Некорректный user_id.")
        if CONFIG.admin_ids and user_id not in CONFIG.admin_ids:
            raise HTTPException(
                status_code=401,
                detail="Локальный режим разрешен только для admin user_id. Откройте Mini App через Telegram.",
            )
        return AIIdentityContext(
            user_id=user_id,
            profile=_build_profile_payload(user_id, source="local", payload=payload),
            source="local",
        )

    raise HTTPException(
        status_code=401,
        detail="Не удалось определить пользователя. Откройте Mini App через кнопку в Telegram.",
    )


def _resolve_user_id(payload: Dict[str, Any]) -> int:
    return _resolve_identity(payload).user_id


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=? LIMIT 1",
        (table_name,),
    ) as cur:
        return bool(await cur.fetchone())


async def _table_columns(conn: aiosqlite.Connection, table_name: str) -> List[str]:
    async with conn.execute(f"PRAGMA table_info({table_name})") as cur:
        rows = await cur.fetchall()
    return [str(row[1]) for row in rows] if rows else []


def _pick_column(columns: List[str], *candidates: str) -> Optional[str]:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


async def _create_messages_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.messages")
    if await _table_exists(conn, "messages"):
        cols = await _table_columns(conn, "messages")
        id_col = _pick_column(cols, "id")
        user_col = _pick_column(cols, "user_id")
        chat_col = _pick_column(cols, "chat_id")
        text_col = _pick_column(cols, "text")
        date_col = _pick_column(cols, "date", "message_date", "saved_at")
        if user_col:
            await conn.execute(
                f"""
                CREATE TEMP VIEW messages AS
                SELECT
                    {id_col if id_col else 'NULL'} AS id,
                    {user_col} AS user_id,
                    {chat_col if chat_col else 'NULL'} AS chat_id,
                    COALESCE({text_col if text_col else "''"}, '') AS text,
                    {date_col if date_col else 'NULL'} AS date
                FROM main.messages
                WHERE {user_col} = {int(user_id)}
                """
            )
            return

    if await _table_exists(conn, "pending"):
        await conn.execute(
            f"""
            CREATE TEMP VIEW messages AS
            SELECT
                id AS id,
                owner_id AS user_id,
                chat_id AS chat_id,
                COALESCE(text, '') AS text,
                COALESCE(message_date, added_at) AS date
            FROM main.pending
            WHERE owner_id = {int(user_id)}
            """
        )
        return

    await conn.execute(
        """
        CREATE TEMP VIEW messages AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            NULL AS chat_id,
            '' AS text,
            NULL AS date
        WHERE 0
        """
    )


async def _create_deleted_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.deleted_messages")
    if await _table_exists(conn, "deleted_messages"):
        cols = await _table_columns(conn, "deleted_messages")
        id_col = _pick_column(cols, "id")
        user_col = _pick_column(cols, "user_id", "owner_id")
        chat_col = _pick_column(cols, "chat_id")
        text_col = _pick_column(cols, "text", "text_preview", "original_text_preview")
        date_col = _pick_column(cols, "date", "saved_at", "original_timestamp")
        chat_title_col = _pick_column(cols, "chat_title")
        sender_col = _pick_column(cols, "sender_username")
        content_col = _pick_column(cols, "content_type")
        if user_col:
            await conn.execute(
                f"""
                CREATE TEMP VIEW deleted_messages AS
                SELECT
                    {id_col if id_col else 'NULL'} AS id,
                    {user_col} AS user_id,
                    {chat_col if chat_col else 'NULL'} AS chat_id,
                    COALESCE({text_col if text_col else "''"}, '') AS text,
                    {date_col if date_col else 'NULL'} AS date,
                    COALESCE({chat_title_col if chat_title_col else "''"}, '') AS chat_title,
                    COALESCE({sender_col if sender_col else "''"}, '') AS sender_username,
                    COALESCE({content_col if content_col else "''"}, '') AS content_type
                FROM main.deleted_messages
                WHERE {user_col} = {int(user_id)}
                """
            )
            return

    await conn.execute(
        """
        CREATE TEMP VIEW deleted_messages AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            NULL AS chat_id,
            '' AS text,
            NULL AS date,
            '' AS chat_title,
            '' AS sender_username,
            '' AS content_type
        WHERE 0
        """
    )


async def _prepare_user_views(conn: aiosqlite.Connection, user_id: int) -> None:
    await _create_messages_view(conn, user_id)
    await _create_deleted_view(conn, user_id)


def sanitize_sql(query: str) -> str:
    if not query:
        raise HTTPException(status_code=400, detail="Пустой SQL-запрос.")
    trimmed = query.strip()
    trimmed = trimmed.rstrip(";").strip()
    if ";" in trimmed:
        raise HTTPException(status_code=400, detail="Разрешён только один SELECT-запрос.")
    if not re.match(r"(?i)^select\b", trimmed):
        raise HTTPException(status_code=400, detail="Допустимы только SELECT-запросы.")
    if "--" in trimmed or "/*" in trimmed or "*/" in trimmed:
        raise HTTPException(status_code=400, detail="SQL-комментарии запрещены.")
    for keyword in AI_FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", trimmed, re.IGNORECASE):
            raise HTTPException(status_code=400, detail=f"Запрещённый оператор в SQL: {keyword}.")
    if re.search(r"(?i)\b(?:main|temp|sqlite_master|sqlite_temp_master)\s*\.", trimmed):
        raise HTTPException(status_code=400, detail="Прямой доступ к системным схемам запрещен.")
    found_tables = {
        tbl.lower().strip('`"')
        for tbl in re.findall(r"(?i)\b(?:from|join)\s+([a-zA-Z0-9_]+)", trimmed)
        if tbl
    }
    invalid_tables = found_tables - AI_ALLOWED_TABLES
    if invalid_tables:
        raise HTTPException(
            status_code=400,
            detail=f"Разрешены только таблицы: {', '.join(sorted(AI_ALLOWED_TABLES))}. "
            f"Найдено: {', '.join(sorted(invalid_tables))}.",
        )
    return trimmed


def _extract_sql(content: str) -> str:
    text = (content or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="AI не вернул SQL-запрос.")
    if text.lower().startswith("sql:"):
        text = text.split(":", 1)[1].strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
    match = re.search(r"(?is)(select\b.*)", text)
    sql_candidate = match.group(1) if match else text
    if ";" in sql_candidate:
        sql_candidate = sql_candidate[: sql_candidate.find(";") + 1]
    return sanitize_sql(sql_candidate)


async def _openrouter_chat(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0,
    max_tokens: int = 600,
) -> List[Dict[str, Any]]:
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY не настроен.")
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }
    last_error: Optional[str] = None
    for attempt in range(1, max(1, AI_OPENROUTER_RETRIES) + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        except httpx.RequestError as exc:
            last_error = str(exc)
            if attempt < AI_OPENROUTER_RETRIES:
                await asyncio.sleep(0.6 * attempt)
                continue
            raise HTTPException(status_code=502, detail=f"OpenRouter network error: {last_error}")

        if response.status_code in (429, 500, 502, 503, 504) and attempt < AI_OPENROUTER_RETRIES:
            await asyncio.sleep(0.6 * attempt)
            continue

        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase or "OpenRouter response error."
            logger.error("OpenRouter returned %s: %s", response.status_code, detail)
            raise HTTPException(status_code=502, detail=f"OpenRouter error ({response.status_code}): {detail}")

        try:
            data = response.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="OpenRouter вернул некорректный JSON.")
        choices = data.get("choices") or []
        if not isinstance(choices, list):
            raise HTTPException(status_code=502, detail="OpenRouter не вернул choices.")
        return choices

    raise HTTPException(status_code=502, detail=f"OpenRouter unavailable: {last_error or 'unknown error'}")


async def generate_sql(question: str) -> str:
    safe_question = _short_text(question.strip(), 700)
    prompt = [
        {"role": "system", "content": AI_SYSTEM_PROMPT},
        {"role": "user", "content": f"Вопрос: {safe_question}"},
    ]
    try:
        choices = await _openrouter_chat(prompt, temperature=0, max_tokens=600)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("OpenRouter SQL generation failed")
        raise HTTPException(status_code=502, detail="Не удалось сгенерировать SQL через OpenRouter.") from exc
    if not choices:
        raise HTTPException(status_code=502, detail="OpenRouter не вернул содержимое.")
    content = choices[0].get("message", {}).get("content", "")
    sql = _extract_sql(content)
    logger.info("AI SQL generated: %s", sql)
    return sql


async def run_sql(query: str, user_id: int) -> Dict[str, Any]:
    sanitized = sanitize_sql(query)
    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await _prepare_user_views(conn, user_id)
        await conn.execute("PRAGMA busy_timeout = 5000")
        try:
            async with conn.execute(sanitized) as cur:
                columns = [desc[0] for desc in (cur.description or [])]
                rows = []
                row_count = 0
                truncated = False
                while True:
                    row = await cur.fetchone()
                    if row is None:
                        break
                    row_count += 1
                    if len(rows) < AI_MAX_RESULT_ROWS:
                        row_dict = {col: _normalize_value(row[col]) for col in columns}
                        rows.append(row_dict)
                    else:
                        truncated = True
                        break
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": row_count,
                    "truncated": truncated,
                    "limit": AI_MAX_RESULT_ROWS,
                }
        except sqlite3.Error as exc:
            logger.exception("AI SQL execution failed")
            raise HTTPException(status_code=400, detail=f"Ошибка выполнения SQL: {exc}")


async def explain_result(question: str, sql: str, result: Dict[str, Any]) -> str:
    if not OPENROUTER_API_KEY:
        return "AI-ассистент недоступен без OPENROUTER_API_KEY."
    payload = {
        "question": question,
        "sql": sql,
        "result": result,
    }
    prompt = [
        {"role": "system", "content": AI_RESULT_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        },
    ]
    try:
        choices = await _openrouter_chat(prompt, temperature=0.3, max_tokens=400)
    except HTTPException as exc:
        logger.exception("OpenRouter explain_result failed: %s", exc.detail)
        return str(exc.detail or "Не удалось получить пояснение от AI.")
    except Exception:
        logger.exception("OpenRouter explain_result unexpected failure")
        return "Не удалось получить пояснение от AI."
    if not choices:
        return "AI не вернул объяснение."
    reply = choices[0].get("message", {}).get("content", "").strip()
    if not reply:
        return "AI не вернул объяснение."
    return _short_text(reply, AI_MAX_ANSWER_CHARS)


async def _telegram_bot_api_get(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not CONFIG.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен.")

    url = f"https://api.telegram.org/bot{CONFIG.bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params)
    except httpx.RequestError as exc:
        logger.warning("Telegram Bot API request failed for %s: %s", method, exc)
        raise HTTPException(status_code=502, detail="Не удалось получить данные профиля из Telegram.") from exc

    if response.status_code >= 400:
        logger.warning("Telegram Bot API returned %s for %s: %s", response.status_code, method, response.text[:300])
        raise HTTPException(status_code=502, detail="Telegram не вернул данные профиля.")

    payload = response.json()
    if not payload.get("ok"):
        logger.warning("Telegram Bot API error for %s: %s", method, payload)
        raise HTTPException(status_code=502, detail="Telegram не смог вернуть данные профиля.")
    return payload.get("result") or {}


async def _fetch_avatar_bytes(user_id: int) -> Optional[Tuple[bytes, str]]:
    cached = AI_AVATAR_CACHE.get(user_id)
    now_ts = time.time()
    if cached and now_ts - float(cached.get("ts", 0)) < AI_AVATAR_CACHE_TTL_SEC:
        return cached["content"], cached["media_type"]

    result = await _telegram_bot_api_get("getUserProfilePhotos", {"user_id": user_id, "limit": 1})
    photos = result.get("photos") or []
    if not photos:
        return None

    best_photo = photos[0][-1] if photos[0] else None
    if not best_photo or not best_photo.get("file_id"):
        return None

    file_result = await _telegram_bot_api_get("getFile", {"file_id": best_photo["file_id"]})
    file_path = str(file_result.get("file_path") or "").strip()
    if not file_path:
        return None

    file_url = f"https://api.telegram.org/file/bot{CONFIG.bot_token}/{file_path}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(file_url)
    except httpx.RequestError as exc:
        logger.warning("Telegram avatar download failed for user %s: %s", user_id, exc)
        return None

    if response.status_code >= 400 or not response.content:
        logger.warning("Telegram avatar download returned %s for user %s", response.status_code, user_id)
        return None

    content = response.content
    if len(content) > AI_AVATAR_MAX_BYTES:
        logger.warning("Avatar for user %s exceeds limit: %s bytes", user_id, len(content))
        return None

    media_type = response.headers.get("content-type") or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    AI_AVATAR_CACHE[user_id] = {"ts": now_ts, "content": content, "media_type": media_type}
    return content, media_type


async def _build_overview(identity: AIIdentityContext) -> Dict[str, Any]:
    user_id = identity.user_id
    profile = dict(identity.profile)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await _prepare_user_views(conn, user_id)

        async with conn.execute("SELECT COUNT(*) FROM deleted_messages") as cur:
            total_deleted = int((await cur.fetchone())[0] or 0)

        async with conn.execute("SELECT COUNT(*) FROM messages") as cur:
            total_messages = int((await cur.fetchone())[0] or 0)

        local_now = datetime.now(CONFIG.tz)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        async with conn.execute(
            "SELECT COUNT(*) FROM deleted_messages WHERE date >= ?",
            (day_start,),
        ) as cur:
            deleted_today = int((await cur.fetchone())[0] or 0)

        async with conn.execute(
            """
            SELECT COALESCE(chat_title, CAST(chat_id AS TEXT), 'Неизвестный чат') AS chat_name,
                   COUNT(*) AS cnt
            FROM deleted_messages
            GROUP BY chat_name
            ORDER BY cnt DESC
            LIMIT 1
            """
        ) as cur:
            top_chat = await cur.fetchone()

        async with conn.execute("SELECT date FROM deleted_messages ORDER BY date DESC LIMIT 1") as cur:
            last_row = await cur.fetchone()

    top_chat_name = str(top_chat[0]) if top_chat else "нет данных"
    top_chat_count = int(top_chat[1]) if top_chat else 0
    last_event_raw = str(last_row[0]) if last_row and last_row[0] else ""
    last_event = _format_event_time(last_event_raw)

    session_active = False
    watcher_active = False
    if BOT_RUNTIME_APP is not None:
        try:
            session_active = bool(BOT_RUNTIME_APP.storage.is_valid(user_id))
        except Exception:
            session_active = False
        try:
            watcher_active = user_id in BOT_RUNTIME_APP.watcher_service.watched_clients
        except Exception:
            watcher_active = False

    summary = (
        f"{profile['display_name']}, в вашем архиве {total_deleted} удаленных сообщений и {total_messages} сохраненных записей. "
        f"Сегодня удалено {deleted_today}. Самый активный чат: {top_chat_name} ({top_chat_count}). "
        f"Последнее удаление зафиксировано: {last_event}."
    )
    return {
        "profile": profile,
        "total_deleted": total_deleted,
        "deleted_today": deleted_today,
        "total_messages": total_messages,
        "top_chat": {"name": top_chat_name, "count": top_chat_count},
        "last_event": last_event,
        "session_active": session_active,
        "watcher_active": watcher_active,
        "summary": summary,
    }


def _close_user_session(user_id: int) -> Dict[str, Any]:
    if BOT_RUNTIME_APP is None:
        return {"message": "Сервис сессий пока не инициализирован.", "session_closed": False}

    file_removed = False
    watcher_stopped = False
    state_reset = False

    try:
        BOT_RUNTIME_APP.storage.delete(user_id)
        file_removed = True
    except Exception:
        logger.exception("Failed to delete session zip for user %s", user_id)

    if BOT_RUNTIME_LOOP is not None:
        try:
            stop_future = asyncio.run_coroutine_threadsafe(
                BOT_RUNTIME_APP.watcher_service.stop(user_id),
                BOT_RUNTIME_LOOP,
            )
            stop_future.result(timeout=20)
            watcher_stopped = True
        except Exception:
            logger.exception("Failed to stop watcher for user %s", user_id)

        try:
            state_future = asyncio.run_coroutine_threadsafe(
                set_state(
                    BOT_RUNTIME_APP.db,
                    user_id,
                    "IDLE",
                    phone=None,
                    tmp_prefix=None,
                    awaiting_2fa=0,
                    auth_fail_count=0,
                    banned_until=None,
                ),
                BOT_RUNTIME_LOOP,
            )
            state_future.result(timeout=20)
            state_reset = True
        except Exception:
            logger.exception("Failed to reset auth state for user %s", user_id)

    if file_removed or watcher_stopped:
        msg = "Сессия завершена: watcher остановлен и данные входа удалены."
    elif state_reset:
        msg = "Сессия частично завершена: состояние входа обновлено."
    else:
        msg = "Не удалось полностью завершить сессию. Попробуйте повторить позже."

    return {
        "message": msg,
        "session_closed": bool(file_removed or watcher_stopped),
        "file_removed": file_removed,
        "watcher_stopped": watcher_stopped,
        "state_reset": state_reset,
    }


def _run_ai_server() -> None:
    logger.info("Запускаю FastAPI на %s:%s", AI_APP_HOST, AI_APP_PORT)
    uvicorn.run(
        ai_app,
        host=AI_APP_HOST,
        port=AI_APP_PORT,
        log_level="warning",
        access_log=False,
    )


def start_ai_daemon() -> None:
    thread = Thread(target=_run_ai_server, daemon=True)
    thread.start()


@ai_app.get("/", response_class=HTMLResponse)
async def ai_root() -> HTMLResponse:
    return HTMLResponse(
        "<h1>AI-assistant Telegram Mini App</h1><p>Перейдите на <a href='/miniapp'>Mini App</a>.</p>",
        headers={"Cache-Control": "no-store"},
    )


@ai_app.get("/miniapp", response_class=HTMLResponse)
async def serve_mini_app() -> HTMLResponse:
    return HTMLResponse(MINIAPP_HTML, headers={"Cache-Control": "no-store"})


@ai_app.get("/ai/health")
async def ai_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "db_exists": os.path.exists(CONFIG.db_path),
        "openrouter_key_configured": bool(OPENROUTER_API_KEY),
        "local_user_fallback": AI_ALLOW_LOCAL_USER_ID,
    }


@ai_app.post("/ai", response_model=AIQueryResponse)
async def ai_endpoint(payload: AIQuestionPayload) -> AIQueryResponse:
    payload_data = payload.dict()
    identity = _resolve_identity(payload_data)
    user_id = identity.user_id
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Введите вопрос.")
    sql = await generate_sql(question)
    result = await run_sql(sql, user_id=user_id)
    answer = await explain_result(question, sql, result)
    logger.info(
        "AI query user=%s rows=%s truncated=%s",
        user_id,
        result["row_count"],
        result["truncated"],
    )
    return AIQueryResponse(answer=answer, sql=sql, result=AIQueryResult(**result))


@ai_app.post("/ai/overview", response_model=AIOverviewResponse)
async def ai_overview(payload: AIIdentityPayload) -> AIOverviewResponse:
    identity = _resolve_identity(payload.dict())
    overview = await _build_overview(identity)
    return AIOverviewResponse(**overview)


@ai_app.post("/ai/profile/avatar")
async def ai_profile_avatar(payload: AIIdentityPayload) -> Response:
    identity = _resolve_identity(payload.dict())
    avatar = await _fetch_avatar_bytes(identity.user_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Аватар недоступен.")
    content, media_type = avatar
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=900"})


@ai_app.post("/ai/session/logout", response_model=AISessionLogoutResponse)
async def ai_session_logout(payload: AIIdentityPayload) -> AISessionLogoutResponse:
    identity = _resolve_identity(payload.dict())
    user_id = identity.user_id
    result = _close_user_session(user_id)
    logger.info("Session close request user=%s closed=%s", user_id, result.get("session_closed"))
    return AISessionLogoutResponse(**result)
