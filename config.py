import os

# Токен бота (от @BotFather)
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8511376414:AAFS2OIjqSfGoxjRta3ehijKjMZFMX1O_jE")

# ID основного Канала (начинается с -100)
CHANNEL_ID: int = int(os.getenv("CHANNEL_ID", "-1003829715647"))

# ID закрытой Группы (начинается с -100)
GROUP_ID: int = int(os.getenv("GROUP_ID", "-1003827311251"))

# ID админской группы с темами (для уведомлений команды)
ADMIN_GROUP_ID: int = int(os.getenv("ADMIN_GROUP_ID", "-1003506963583"))

# ID темы (топика) в админской группе для уведомлений
ADMIN_TOPIC_ID: int = int(os.getenv("ADMIN_TOPIC_ID", "412"))

# Сколько нужно пригласить людей
REQUIRED_INVITES: int = int(os.getenv("REQUIRED_INVITES", "4"))

# Количество дней подписки
SUBSCRIPTION_DAYS: int = int(os.getenv("SUBSCRIPTION_DAYS", "21"))

# Путь к базе данных
DB_PATH: str = os.getenv("DB_PATH", "referral_bot.db")

# Порт для Render (автоматически берется из системы)
PORT: int = int(os.getenv("PORT", 8080))
