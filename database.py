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
                created_at TEXT,
                last_active TEXT,
                reminder_sent INTEGER DEFAULT 0
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
        # Таблица событий канала (вход/выход для аналитики)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                event_type TEXT,  -- 'join' or 'leave'
                referrer_id INTEGER,
                referrer_name TEXT,
                event_date TEXT
            )
        """)
        # Таблица заявок на вступление (анкеты)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS join_requests (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                source TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                resolved_at TEXT
            )
        """)
        await db.commit()

async def create_user(user_id, username, full_name, invite_link):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, invite_link, created_at, last_active) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, invite_link, now, now)
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

async def update_last_active(user_id):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_active = ?, reminder_sent = 0 WHERE user_id = ?", (now, user_id))
        await db.commit()

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
        except:
            return False

async def remove_referral(referrer_id, referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute(
            "DELETE FROM referrals WHERE referrer_id = ? AND referred_id = ?",
            (referrer_id, referred_id)
        )
        if res.rowcount > 0:
            await db.execute(
                "UPDATE users SET invited_count = MAX(0, invited_count - 1) WHERE user_id = ?",
                (referrer_id,)
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

async def set_expiry(user_id, days):
    expiry = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET expiry_date = ?, completed = 1, notified = 0 WHERE user_id = ?",
            (expiry, user_id)
        )
        await db.commit()

async def mark_notified(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET notified = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def reset_user_status(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET completed = 0, expiry_date = NULL, notified = 0 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def get_expired_users():
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id FROM users WHERE completed = 1 AND expiry_date <= ?", (now,)
        ) as c:
            return [row['user_id'] for row in await c.fetchall()]

async def get_users_to_notify(days_before):
    threshold = (datetime.now(timezone.utc) + timedelta(days=days_before)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, expiry_date FROM users WHERE completed = 1 AND notified = 0 AND expiry_date <= ?",
            (threshold,)
        ) as c:
            return [dict(row) for row in await c.fetchall()]

# ── Напоминания неактивным ──────────────────────────────────────────────────
async def get_inactive_users_to_remind(inactive_days=3):
    """Пользователи с invite_link, неактивные N дней, которым не отправлено напоминание."""
    threshold = (datetime.now(timezone.utc) - timedelta(days=inactive_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id, invite_link, invited_count FROM users
               WHERE invite_link IS NOT NULL
                 AND reminder_sent = 0
                 AND (completed = 0 OR completed IS NULL)
                 AND last_active <= ?""",
            (threshold,)
        ) as c:
            return [dict(row) for row in await c.fetchall()]

async def mark_reminder_sent(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reminder_sent = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

# ── Аналитика событий канала ────────────────────────────────────────────────
async def log_channel_event(user_id, username, full_name, event_type, referrer_id=None, referrer_name=None):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO channel_events
               (user_id, username, full_name, event_type, referrer_id, referrer_name, event_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, full_name, event_type, referrer_id, referrer_name, now)
        )
        await db.commit()

async def get_daily_stats(date_str: str):
    """Статистика за конкретный день (YYYY-MM-DD) по Киеву (UTC+3)."""
    # Переводим начало и конец дня из Киева (UTC+3) в UTC
    day_start_utc = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(hours=3)).isoformat()
    day_end_utc = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(hours=21)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM channel_events WHERE event_date >= ? AND event_date < ?",
            (day_start_utc, day_end_utc)
        ) as c:
            events = [dict(r) for r in await c.fetchall()]

        # Полное количество участников канала (сумма всех join - leave)
        async with db.execute(
            "SELECT COUNT(*) as total FROM channel_events WHERE event_type = 'join'"
        ) as c:
            total_joins = (await c.fetchone())['total']
        async with db.execute(
            "SELECT COUNT(*) as total FROM channel_events WHERE event_type = 'leave'"
        ) as c:
            total_leaves = (await c.fetchone())['total']

    joins = [e for e in events if e['event_type'] == 'join']
    leaves = [e for e in events if e['event_type'] == 'leave']
    net = len(joins) - len(leaves)
    total_now = total_joins - total_leaves
    churn = round(len(leaves) / total_joins * 100, 1) if total_joins > 0 else 0.0
    return {
        'joins': joins,
        'leaves': leaves,
        'net': net,
        'total_now': total_now,
        'churn_rate': churn,
    }

async def get_period_stats(date_from: str, date_to: str):
    """Статистика за период (YYYY-MM-DD включительно) по Киеву."""
    start_utc = (datetime.strptime(date_from, "%Y-%m-%d") - timedelta(hours=3)).isoformat()
    end_utc = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(hours=21)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM channel_events WHERE event_date >= ? AND event_date < ?",
            (start_utc, end_utc)
        ) as c:
            events = [dict(r) for r in await c.fetchall()]

        async with db.execute("SELECT COUNT(*) as t FROM channel_events WHERE event_type = 'join'") as c:
            total_joins = (await c.fetchone())['t']
        async with db.execute("SELECT COUNT(*) as t FROM channel_events WHERE event_type = 'leave'") as c:
            total_leaves = (await c.fetchone())['t']

    joins = [e for e in events if e['event_type'] == 'join']
    leaves = [e for e in events if e['event_type'] == 'leave']
    net = len(joins) - len(leaves)
    total_now = total_joins - total_leaves
    churn = round(len(leaves) / total_joins * 100, 1) if total_joins > 0 else 0.0
    return {
        'joins': joins,
        'leaves': leaves,
        'net': net,
        'total_now': total_now,
        'churn_rate': churn,
        'date_from': date_from,
        'date_to': date_to,
    }

# ── Заявки на вступление ────────────────────────────────────────────────────
async def save_join_request(user_id, username, full_name):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO join_requests
               (user_id, username, full_name, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (user_id, username, full_name, now)
        )
        await db.commit()

async def resolve_join_request(user_id, status, source=None):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE join_requests
               SET status = ?, source = ?, resolved_at = ?
               WHERE user_id = ?""",
            (status, source, now, user_id)
        )
        await db.commit()
