import aiosqlite
from datetime import datetime, timezone, timedelta
from config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                full_name     TEXT,
                invite_link   TEXT UNIQUE,
                invited_count INTEGER DEFAULT 0,
                completed     INTEGER DEFAULT 0,
                expiry_date   TEXT,
                notified      INTEGER DEFAULT 0,
                created_at    TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id  INTEGER,
                referred_id  INTEGER,
                joined_at    TEXT,
                UNIQUE(referrer_id, referred_id)
            )
        """)
        # Миграция: добавить колонки если их нет (для старых БД)
        for col, definition in [
            ("expiry_date", "TEXT"),
            ("notified",    "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except Exception:
                pass  # Уже существует
        await db.commit()

# ─────────────────────────────────────────────
async def create_user(user_id, username, full_name, invite_link):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, invite_link, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, invite_link, now),
        )
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def get_user_by_invite_link(link):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE invite_link = ?", (link,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def add_referral(referrer_id, referred_id):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_id, joined_at) VALUES (?, ?, ?)",
                (referrer_id, referred_id, now),
            )
            await db.execute(
                "UPDATE users SET invited_count = invited_count + 1 WHERE user_id = ?",
                (referrer_id,),
            )
            await db.commit()
            return True
        except Exception:
            return False

async def remove_referral(referrer_id, referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute(
            "DELETE FROM referrals WHERE referrer_id = ? AND referred_id = ?",
            (referrer_id, referred_id),
        )
        if res.rowcount > 0:
            await db.execute(
                "UPDATE users SET invited_count = MAX(0, invited_count - 1) WHERE user_id = ?",
                (referrer_id,),
            )
            await db.commit()
            return True
        return False

async def get_referrer_of(referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ?", (referred_id,)
        ) as c:
            row = await c.fetchone()
            return row[0] if row else None

async def get_referrer_info(referred_id):
    """Вернуть полную инфу о реферере (для логирования)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.user_id, u.username, u.full_name, u.invited_count
               FROM referrals r JOIN users u ON r.referrer_id = u.user_id
               WHERE r.referred_id = ?""",
            (referred_id,),
        ) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def set_expiry(user_id, days):
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET expiry_date = ?, completed = 1, notified = 0 WHERE user_id = ?",
            (expiry, user_id),
        )
        await db.commit()
    return expiry

async def mark_notified(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET notified = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def reset_user_status(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET completed = 0, expiry_date = NULL, notified = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()

async def get_expired_users():
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, full_name FROM users WHERE completed = 1 AND expiry_date <= ?", (now,)
        ) as c:
            return [dict(r) for r in await c.fetchall()]

async def get_users_to_notify(days_before):
    threshold = (datetime.now(timezone.utc) + timedelta(days=days_before)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, expiry_date FROM users WHERE completed = 1 AND notified = 0 AND expiry_date <= ?",
            (threshold,),
        ) as c:
            return [dict(r) for r in await c.fetchall()]

async def get_top_referrers(limit: int = 10):
    """Топ рефереров по количеству приглашённых."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.user_id, u.username, u.full_name, u.invited_count,
                      u.completed, u.expiry_date
               FROM users u
               ORDER BY u.invited_count DESC
               LIMIT ?""",
            (limit,),
        ) as c:
            return [dict(r) for r in await c.fetchall()]

async def get_total_stats():
    """Общая статистика для /adminstats."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM referrals") as c:
            total_referrals = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE completed = 1") as c:
            total_completed = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE completed = 1 AND expiry_date > ?",
            (datetime.now(timezone.utc).isoformat(),),
        ) as c:
            active_now = (await c.fetchone())[0]
    return {
        "total_users": total_users,
        "total_referrals": total_referrals,
        "total_completed": total_completed,
        "active_now": active_now,
    }
