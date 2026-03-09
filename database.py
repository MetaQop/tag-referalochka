import aiosqlite
from datetime import datetime, timezone, timedelta
from config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                invite_link TEXT UNIQUE,
                invited_count INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0,
                expiry_date TEXT,
                notified INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        # Таблица связей (кто кого пригласил)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                joined_at TEXT,
                UNIQUE(referrer_id, referred_id)
            )
        """)
        await db.commit()

async def create_user(user_id, username, full_name, invite_link):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, invite_link, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, invite_link, now)
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
                (referrer_id, referred_id, now)
            )
            await db.execute(
                "UPDATE users SET invited_count = invited_count + 1 WHERE user_id = ?",
                (referrer_id,)
            )
            await db.commit()
            return True
        except: return False

async def remove_referral(referrer_id, referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("DELETE FROM referrals WHERE referrer_id = ? AND referred_id = ?", (referrer_id, referred_id))
        if res.rowcount > 0:
            await db.execute("UPDATE users SET invited_count = MAX(0, invited_count - 1) WHERE user_id = ?", (referrer_id,))
            await db.commit()
            return True
        return False

async def get_referrer_of(referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT referrer_id FROM referrals WHERE referred_id = ?", (referred_id,)) as c:
            row = await c.fetchone()
            return row[0] if row else None

async def set_expiry(user_id, days):
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET expiry_date = ?, completed = 1, notified = 0 WHERE user_id = ?", (expiry, user_id))
        await db.commit()

async def mark_notified(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET notified = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def reset_user_status(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET completed = 0, expiry_date = NULL, notified = 0 WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_expired_users():
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM users WHERE completed = 1 AND expiry_date <= ?", (now,)) as c:
            return [row['user_id'] for row in await c.fetchall()]

async def get_users_to_notify(days_before):
    threshold = (datetime.now(timezone.utc) + timedelta(days=days_before)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, expiry_date FROM users WHERE completed = 1 AND notified = 0 AND expiry_date <= ?", (threshold,)
        ) as c:
            return [dict(row) for row in await c.fetchall()]
