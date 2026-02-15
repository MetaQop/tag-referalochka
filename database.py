"""
database.py — Асинхронная работа с SQLite через aiosqlite.

Структура БД:
  ┌──────────────────────────────────────────────────────────────┐
  │ Таблица users                                                │
  │   user_id       INTEGER  PRIMARY KEY  — Telegram user ID    │
  │   username      TEXT                  — @username (м.б. '')  │
  │   full_name     TEXT                  — Имя и фамилия        │
  │   invite_link   TEXT     UNIQUE       — Персональная ссылка  │
  │   invited_count INTEGER  DEFAULT 0   — Счётчик приглашённых │
  │   completed     INTEGER  DEFAULT 0   — Флаг: 1 = выполнено  │
  │   created_at    TEXT                  — Дата регистрации     │
  ├──────────────────────────────────────────────────────────────┤
  │ Таблица referrals                                            │
  │   id            INTEGER  PRIMARY KEY AUTOINCREMENT          │
  │   referrer_id   INTEGER  — user_id пригласившего            │
  │   referred_id   INTEGER  — user_id вступившего              │
  │   joined_at     TEXT     — Дата вступления                   │
  │   UNIQUE(referrer_id, referred_id)  — нет дублей            │
  └──────────────────────────────────────────────────────────────┘
"""

import aiosqlite
from datetime import datetime, timezone

from config import DB_PATH


# ══════════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ
# ══════════════════════════════════════════════

async def init_db() -> None:
    """Создаёт таблицы, если они не существуют."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT    NOT NULL DEFAULT '',
                full_name     TEXT    NOT NULL DEFAULT '',
                invite_link   TEXT    UNIQUE,
                invited_count INTEGER NOT NULL DEFAULT 0,
                completed     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL
            )
        """)

        # Таблица рефералов (для защиты от повторного зачёта)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                joined_at   TEXT    NOT NULL,
                UNIQUE(referrer_id, referred_id),
                FOREIGN KEY (referrer_id) REFERENCES users(user_id)
            )
        """)

        # Индексы для ускорения выборок
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer
            ON referrals(referrer_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_invite_link
            ON users(invite_link)
        """)

        await db.commit()


# ══════════════════════════════════════════════
# CRUD — ПОЛЬЗОВАТЕЛИ
# ══════════════════════════════════════════════

async def create_user(
    user_id: int,
    username: str,
    full_name: str,
    invite_link: str,
) -> None:
    """
    Добавляет нового пользователя.
    Если пользователь уже существует — ничего не делает (INSERT OR IGNORE).
    """
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO users
                (user_id, username, full_name, invite_link, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, username, full_name, invite_link, now),
        )
        await db.commit()


async def get_user(user_id: int) -> dict | None:
    """
    Возвращает словарь с данными пользователя или None, если не найден.
    Ключи: user_id, username, full_name, invite_link,
           invited_count, completed, created_at
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_user_by_invite_link(invite_link: str) -> dict | None:
    """
    Возвращает пользователя, которому принадлежит данная реферальная ссылка.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE invite_link = ?", (invite_link,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def increment_invited_count(user_id: int) -> int:
    """
    Увеличивает счётчик приглашённых на 1.
    Возвращает новое значение счётчика.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET invited_count = invited_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT invited_count FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def mark_completed(user_id: int) -> bool:
    """
    Атомарно помечает пользователя как «завершившего задание».
    Возвращает True, если запись была обновлена (не была completed раньше).
    Возвращает False, если уже был помечен (защита от гонки условий).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE users
            SET completed = 1
            WHERE user_id = ? AND completed = 0
            """,
            (user_id,),
        )
        await db.commit()
        return cursor.rowcount > 0  # rowcount=0 → уже был completed


# ══════════════════════════════════════════════
# CRUD — РЕФЕРАЛЫ
# ══════════════════════════════════════════════

async def add_referral(referrer_id: int, referred_id: int) -> bool:
    """
    Записывает нового реферала.
    Возвращает True при успехе, False если уже существует.
    """
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO referrals (referrer_id, referred_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (referrer_id, referred_id, now),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # UNIQUE constraint: этот реферал уже зачтён
            return False


async def is_referral_counted(referrer_id: int, referred_id: int) -> bool:
    """Проверяет, был ли данный реферал уже засчитан."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT 1 FROM referrals
            WHERE referrer_id = ? AND referred_id = ?
            """,
            (referrer_id, referred_id),
        ) as cursor:
            return await cursor.fetchone() is not None


# ══════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════

def _now_iso() -> str:
    """Возвращает текущее время UTC в ISO-формате."""
    return datetime.now(timezone.utc).isoformat()
