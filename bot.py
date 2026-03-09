import asyncio
import logging
from datetime import datetime, timezone, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

import database as db
from config import (
    BOT_TOKEN, CHANNEL_ID, GROUP_ID, REQUIRED_INVITES, PORT,
    SUBSCRIPTION_DAYS, ADMIN_GROUP_ID, ADMIN_TOPIC_ID
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Ссылка на тему с вопросами в админ-группе
SUPPORT_TOPIC_LINK = "https://t.me/c/3506963583/434"

# ──────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────
def kyiv_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)

async def notify_admin(text: str):
    try:
        await bot.send_message(
            ADMIN_GROUP_ID,
            text,
            message_thread_id=ADMIN_TOPIC_ID,
        )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")

def fmt_user(username, full_name, user_id):
    name = html.quote(full_name or "")
    if username:
        return f'<a href="tg://user?id={user_id}">{name}</a> (@{username})'
    return f'<a href="tg://user?id={user_id}">{name}</a>'

def build_stats_text(stats: dict, period_label: str) -> str:
    joins = stats['joins']
    leaves = stats['leaves']
    net = stats['net']
    total = stats['total_now']
    churn = stats['churn_rate']

    lines = [f"📊 <b>Статистика за {period_label}</b>\n"]
    lines.append(f"✅ Вошли: <b>{len(joins)}</b>")
    lines.append(f"❌ Вышли: <b>{len(leaves)}</b>")
    lines.append(f"📈 Чистый прирост: <b>{'+' if net >= 0 else ''}{net}</b>")
    lines.append(f"👥 Всего участников: <b>{total}</b>")
    lines.append(f"📉 Churn rate: <b>{churn}%</b>")
    return "\n".join(lines)

# ──────────────────────────────────────────────
# КЛАВИАТУРЫ
# ──────────────────────────────────────────────
def main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="get_link"))
    builder.row(InlineKeyboardButton(text="📊 Мой прогресс", callback_data="stats"))
    builder.row(InlineKeyboardButton(text="❓ Задать вопрос", url=SUPPORT_TOPIC_LINK))
    return builder.as_markup()

def welcome_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎁 Получить VIP", callback_data="get_link"))
    builder.row(InlineKeyboardButton(text="📊 Мой прогресс", callback_data="stats"))
    builder.row(InlineKeyboardButton(text="❓ Задать вопрос", url=SUPPORT_TOPIC_LINK))
    return builder.as_markup()

# ──────────────────────────────────────────────
# СЕРВЕР И ПЛАНИРОВЩИК
# ──────────────────────────────────────────────
async def handle_health(request):
    return web.Response(text="OK")

async def start_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Health server started on port {PORT}")

async def sub_scheduler():
    while True:
        try:
            for user in await db.get_users_to_notify(3):
                exp = datetime.fromisoformat(user['expiry_date']).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(
                        user['user_id'],
                        f"⚠️ Подписка истекает <b>{exp}</b>. Через 3 дня вы будете исключены.",
                    )
                    await db.mark_notified(user['user_id'])
                except Exception as e:
                    logger.warning(f"Notify error {user['user_id']}: {e}")

            for uid in await db.get_expired_users():
                try:
                    await bot.ban_chat_member(GROUP_ID, uid)
                    await bot.unban_chat_member(GROUP_ID, uid)
                    await db.reset_user_status(uid)
                    await bot.send_message(uid, "🔴 Срок подписки истек. Вы исключены из группы.")
                except Exception as e:
                    logger.error(f"Kick error {uid}: {e}")

            for user in await db.get_inactive_users_to_remind(3):
                remaining = max(0, REQUIRED_INVITES - user['invited_count'])
                try:
                    await bot.send_message(
                        user['user_id'],
                        f"🔔 Эй! Прошло 3 дня, а ты ещё не в VIP.\n"
                        f"Осталось пригласить <b>{remaining}</b> друзей — и {SUBSCRIPTION_DAYS} дней эксклюзива твои 🔥\n"
                        f"Твоя ссылка: <code>{user['invite_link']}</code>",
                    )
                    await db.mark_reminder_sent(user['user_id'])
                except Exception as e:
                    logger.warning(f"Reminder error {user['user_id']}: {e}")

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(3600)

async def daily_stats_scheduler():
    while True:
        try:
            now_kyiv = kyiv_now()
            next_midnight = (now_kyiv + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_seconds = (next_midnight - now_kyiv).total_seconds()
            await asyncio.sleep(wait_seconds)

            yesterday = (kyiv_now() - timedelta(days=1)).strftime("%Y-%m-%d")
            stats = await db.get_daily_stats(yesterday)
            date_label = datetime.strptime(yesterday, "%Y-%m-%d").strftime("%d.%m.%Y")
            text = build_stats_text(stats, date_label)
            await notify_admin(text)
        except Exception as e:
            logger.error(f"Daily stats error: {e}")
            await asyncio.sleep(60)

# ──────────────────────────────────────────────
# ХЭНДЛЕРЫ БОТА
# ──────────────────────────────────────────────
@dp.message(CommandStart())
async def start(m: Message):
    u = await db.get_user(m.from_user.id)

    if not u:
        try:
            link_obj = await bot.create_chat_invite_link(
                CHANNEL_ID,
                name=f"ref_{m.from_user.id}",
                creates_join_request=False
            )
            invite_link = link_obj.invite_link
            await db.create_user(
                m.from_user.id, m.from_user.username,
                m.from_user.full_name, invite_link
            )
        except Exception as e:
            logger.error(f"Create invite link error: {e}")
            await m.answer(
                f"👋 Привет, {html.quote(m.from_user.full_name)}!\n"
                f"Пригласи {REQUIRED_INVITES} друзей в канал — получи VIP на {SUBSCRIPTION_DAYS} дней.\n\n"
                "⚠️ Не удалось создать ссылку. Убедитесь что бот — администратор канала.",
                reply_markup=main_kb()
            )
            return
        invited = 0
    else:
        invite_link = u['invite_link']
        invited = u['invited_count']

    await db.update_last_active(m.from_user.id)

    await m.answer(
        f"👋 Привет! Ты попал в один из лучших NSFW каналов.\n\n"
        f"🎁 Хочешь VIP? Пригласи {REQUIRED_INVITES} друга → получаешь VIP на {SUBSCRIPTION_DAYS} дней\n\n"
        f"Твоя ссылка: <code>{invite_link}</code>\n\n"
        f"📊 Твой прогресс: {invited}/{REQUIRED_INVITES}\n\n"
        f"❓ Вопросы — нажми кнопку ниже",
        reply_markup=main_kb(),
    )

@dp.callback_query(F.data == "get_link")
async def get_link(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if not u:
        try:
            link_obj = await bot.create_chat_invite_link(
                CHANNEL_ID,
                name=f"ref_{c.from_user.id}",
                creates_join_request=False
            )
            invite_link = link_obj.invite_link
            await db.create_user(
                c.from_user.id, c.from_user.username,
                c.from_user.full_name, invite_link
            )
        except Exception as e:
            logger.error(f"get_link callback error: {e}")
            return await c.answer("❌ Бот не является администратором канала!", show_alert=True)
    else:
        invite_link = u['invite_link']

    await db.update_last_active(c.from_user.id)
    await c.message.answer(
        f"🔗 Твоя реферальная ссылка:\n<code>{invite_link}</code>",
    )
    await c.answer()

@dp.callback_query(F.data == "stats")
async def stats_cb(c: CallbackQuery):
    await db.update_last_active(c.from_user.id)
    u = await db.get_user(c.from_user.id)
    if not u:
        return await c.answer("Сначала получите ссылку!", show_alert=True)

    txt = f"📊 Друзей приглашено: {u['invited_count']}/{REQUIRED_INVITES}\n"
    if u['completed'] and u['expiry_date']:
        exp = datetime.fromisoformat(u['expiry_date']).strftime("%d.%m.%Y %H:%M")
        txt += f"🔐 VIP доступ до: <b>{exp}</b>"
    else:
        remaining = max(0, REQUIRED_INVITES - u['invited_count'])
        txt += f"⏳ Ещё нужно пригласить: <b>{remaining}</b>"

    await c.message.answer(txt)
    await c.answer()

# ──────────────────────────────────────────────
# КОМАНДЫ АНАЛИТИКИ (только в админ-группе)
# ──────────────────────────────────────────────
@dp.message(Command("week"))
async def cmd_week(m: Message):
    if m.chat.id != ADMIN_GROUP_ID:
        return
    today = kyiv_now()
    date_to = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    stats = await db.get_period_stats(date_from, date_to)
    d_from = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d.%m")
    d_to = datetime.strptime(date_to, "%Y-%m-%d").strftime("%d.%m.%Y")
    text = build_stats_text(stats, f"неделю ({d_from}–{d_to})")
    await m.reply(text)

@dp.message(Command("month"))
async def cmd_month(m: Message):
    if m.chat.id != ADMIN_GROUP_ID:
        return
    today = kyiv_now()
    date_to = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    stats = await db.get_period_stats(date_from, date_to)
    d_from = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d.%m")
    d_to = datetime.strptime(date_to, "%Y-%m-%d").strftime("%d.%m.%Y")
    text = build_stats_text(stats, f"месяц ({d_from}–{d_to})")
    await m.reply(text)

@dp.message(Command("today"))
async def cmd_today(m: Message):
    if m.chat.id != ADMIN_GROUP_ID:
        return
    today = kyiv_now().strftime("%Y-%m-%d")
    stats = await db.get_daily_stats(today)
    date_label = kyiv_now().strftime("%d.%m.%Y")
    text = build_stats_text(stats, date_label)
    await m.reply(text)

# ──────────────────────────────────────────────
# ТРЕКИНГ ВХОДА / ВЫХОДА В КАНАЛ
# ──────────────────────────────────────────────
@dp.chat_member(F.chat.id == CHANNEL_ID)
async def tracking(event: ChatMemberUpdated):
    old, new = event.old_chat_member.status, event.new_chat_member.status
    uid = event.new_chat_member.user.id
    uname = event.new_chat_member.user.username
    fname = event.new_chat_member.user.full_name

    # ── Вступление ──────────────────────────────
    if old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER:
        referrer_id = None
        referrer_name = None

        if event.invite_link:
            ref = await db.get_user_by_invite_link(event.invite_link.invite_link)
            if ref and uid != ref['user_id']:
                referrer_id = ref['user_id']
                referrer_name = ref.get('full_name') or ref.get('username') or str(ref['user_id'])

                if await db.add_referral(ref['user_id'], uid):
                    u = await db.get_user(ref['user_id'])
                    if u['invited_count'] >= REQUIRED_INVITES and not u['completed']:
                        await db.set_expiry(ref['user_id'], SUBSCRIPTION_DAYS)
                        try:
                            grp_link = await bot.create_chat_invite_link(GROUP_ID, member_limit=1)
                            await bot.send_message(
                                ref['user_id'],
                                f"🏆 Готово! Ты выполнил задание.\n"
                                f"Доступ в VIP на {SUBSCRIPTION_DAYS} дней:\n{grp_link.invite_link}",
                            )
                        except Exception as e:
                            logger.error(f"VIP link error: {e}")
                    else:
                        try:
                            await bot.send_message(
                                ref['user_id'],
                                f"🎉 Новый участник по твоей ссылке! ({u['invited_count']}/{REQUIRED_INVITES})",
                            )
                        except Exception as e:
                            logger.warning(f"Ref notify error: {e}")

        await db.log_channel_event(uid, uname, fname, 'join', referrer_id, referrer_name)

        # ── Приветственное сообщение новому участнику ──
        try:
            user_db = await db.get_user(uid)
            if not user_db:
                try:
                    link_obj = await bot.create_chat_invite_link(
                        CHANNEL_ID,
                        name=f"ref_{uid}",
                        creates_join_request=False
                    )
                    invite_link = link_obj.invite_link
                    await db.create_user(uid, uname, fname, invite_link)
                except Exception as e:
                    logger.error(f"Welcome: create invite link error for {uid}: {e}")
                    invite_link = None
            else:
                invite_link = user_db.get('invite_link')

            if invite_link:
                welcome_text = (
                    f"👋 Привет, {html.quote(fname or 'друг')}! Ты попал в один из лучших NSFW каналов.\n\n"
                    f"🎁 Хочешь VIP? Пригласи <b>{REQUIRED_INVITES}</b> друга → получаешь VIP на <b>{SUBSCRIPTION_DAYS} дней</b>\n\n"
                    f"Твоя реферальная ссылка:\n<code>{invite_link}</code>\n\n"
                    f"📊 Твой прогресс: 0/{REQUIRED_INVITES}\n\n"
                    f"❓ Есть вопросы? Нажми кнопку ниже — ответим!"
                )
            else:
                welcome_text = (
                    f"👋 Привет, {html.quote(fname or 'друг')}! Ты попал в один из лучших NSFW каналов.\n\n"
                    f"🎁 Хочешь VIP? Пригласи <b>{REQUIRED_INVITES}</b> друга → получаешь VIP на <b>{SUBSCRIPTION_DAYS} дней</b>\n\n"
                    f"Напиши /start боту чтобы получить реферальную ссылку.\n\n"
                    f"❓ Есть вопросы? Нажми кнопку ниже — ответим!"
                )

            await bot.send_message(uid, welcome_text, reply_markup=welcome_kb())
        except Exception as e:
            # Пользователь мог заблокировать бота — это нормально
            logger.warning(f"Welcome message failed for {uid}: {e}")

        # ── Уведомление в админ-группу ──
        user_link = fmt_user(uname, fname, uid)
        if referrer_id and referrer_name:
            admin_text = (
                f"✅ <b>Новый участник в канале</b>\n"
                f"👤 {user_link}\n"
                f"🔗 Пришёл по реферальной ссылке от: <b>{html.quote(referrer_name)}</b>"
            )
        else:
            admin_text = (
                f"✅ <b>Новый участник в канале</b>\n"
                f"👤 {user_link}\n"
                f"🔗 Источник: прямая ссылка / неизвестен"
            )
        await notify_admin(admin_text)

    # ── Выход ───────────────────────────────────
    elif old == ChatMemberStatus.MEMBER and new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        refr_id = await db.get_referrer_of(uid)

        await db.log_channel_event(uid, uname, fname, 'leave', refr_id, None)

        if refr_id:
            if await db.remove_referral(refr_id, uid):
                try:
                    u = await db.get_user(refr_id)
                    await bot.send_message(
                        refr_id,
                        f"📉 Участник покинул канал. Балл аннулирован ({u['invited_count']}/{REQUIRED_INVITES})"
                    )
                except Exception as e:
                    logger.warning(f"Remove ref notify error: {e}")

        user_link = fmt_user(uname, fname, uid)
        action = "исключён" if new == ChatMemberStatus.KICKED else "покинул канал"
        admin_text = (
            f"❌ <b>Участник {action}</b>\n"
            f"👤 {user_link}"
        )
        if refr_id:
            admin_text += f"\n🔗 Был приглашён реферером (ID: <code>{refr_id}</code>)"
        await notify_admin(admin_text)

# ──────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────
async def main():
    await db.init_db()

    loop = asyncio.get_event_loop()
    loop.create_task(start_server())
    loop.create_task(sub_scheduler())
    loop.create_task(daily_stats_scheduler())

    logger.info("Bot started polling...")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "chat_member", "callback_query"]
    )

if __name__ == "__main__":
    asyncio.run(main())
