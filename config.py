import os

# ── Telegram ──────────────────────────────────
BOT_TOKEN: str   = os.getenv("BOT_TOKEN", "8511376414:AAFS2OIjqSfGoxjRta3ehijKjMZFMX1O_jE")
CHANNEL_ID: int  = int(os.getenv("CHANNEL_ID", "-1003829715647"))
GROUP_ID: int    = int(os.getenv("GROUP_ID", "-1003827311251"))

# ── Реферальная логика ────────────────────────
REQUIRED_INVITES: int    = int(os.getenv("REQUIRED_INVITES", "4"))
SUBSCRIPTION_DAYS: int   = int(os.getenv("SUBSCRIPTION_DAYS", "30"))

# ── База данных ───────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "referral_bot.db")

# ── Render / Webhook ──────────────────────────
# На Render автоматически задаётся переменная RENDER_EXTERNAL_URL
# Пример: https://your-app.onrender.com
WEBHOOK_HOST: str = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH: str = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL: str  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else ""
PORT: int         = int(os.getenv("PORT", 8080))

# ── Google Sheets (опционально) ───────────────
# Если не задано — Sheets-логирование просто пропускается без ошибок
SHEETS_CREDS: str      = os.getenv("SHEETS_CREDS_PATH", "credentials.json")
SPREADSHEET_NAME: str  = os.getenv("SPREADSHEET_NAME", "TAG Growth Tracker")
SHEETS_ENABLED: bool   = os.path.exists(SHEETS_CREDS)
