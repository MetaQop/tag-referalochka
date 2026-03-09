import asyncio
import logging
from datetime import datetime, timezone, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import (
    BOT_TOKEN, CHANNEL_ID, GROUP_ID, REQUIRED_INVITES, PORT,
    SUBSCRIPTION_DAYS, ADMIN_GROUP_ID, ADMIN_TOPIC_ID
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ──────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────
def kyiv_now() -> datetime:
    """Текущее время по Киеву (UTC+3)."""
    return datetime.now(timezone.utc) + timedelta(hours=3)

async def notify_admin(text: str):
    """Отправить сообщение в тему (топик) админской группы."""
    try:
        await bot.send_message(
            ADMIN_GROUP_ID,
            text,
            message_thread_id=ADMIN_TOPIC_ID,
            parse_mode="HTML"
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
# СЕРВЕР И ПЛАНИРОВЩИК
# ──────────────────────────────────────────────
async def handle_health(request):
    return web.Response(text="OK")

async def start_server():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Health server started on port {PORT}")

async def sub_scheduler():
    """Каждый час: уведомления об истечении, кик просроченных, ремайндеры."""
    while True:
        try:
            # Предупреждение за 3 дня
            for user in await db.get_users_to_notify(3):
                exp = datetime.fromisoformat(user['expiry_date']).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(
                        user['user_id'],
                        f"⚠️ Подписка истекает <b>{exp}</b>. Через 3 дня вы будете исключены.",
                        parse_mode="HTML"
                    )
                    await db.mark_notified(user['user_id'])
                except Exception as e:
                    logger.warning(f"Notify error {user['user_id']}: {e}")

            # Кик просроченных
            for uid in await db.get_expired_users():
                try:
                    await bot.ban_chat_member(GROUP_ID, uid)
                    await bot.unban_chat_member(GROUP_ID, uid)
                    await db.reset_user_status(uid)
                    await bot.send_message(uid, "🔴 Срок подписки истек. Вы исключены из группы.")
                except Exception as e:
                    logger.error(f"Kick error {uid}: {e}")

            # Ремайндеры неактивным (3 дня бездействия)
            for user in await db.get_inactive_users_to_remind(3):
                remaining = max(0, REQUIRED_INVITES - user['invited_count'])
                try:
                    await bot.send_message(
                        user['user_id'],
                        f"🔔 Эй! Прошло 3 дня, а ты ещё не в VIP.\n"
                        f"Осталось пригласить <b>{remaining}</b> друзей — и {SUBSCRIPTION_DAYS} дней эксклюзива твои 🔥\n"
                        f"Твоя ссылка: <code>{user['invite_link']}</code>",
                        parse_mode="HTML"
                    )
                    await db.mark_reminder_sent(user['user_id'])
                except Exception as e:
                    logger.warning(f"Reminder error {user['user_id']}: {e}")

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(3600)

async def daily_stats_scheduler():
    """Каждый день в 00:00 по Киеву отправляет сводку в админ-группу."""
    while True:
        try:
            now_kyiv = kyiv_now()
            # Считаем секунды до следующей полуночи по Киеву
            next_midnight = (now_kyiv + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_seconds = (next_midnight - now_kyiv).total_seconds()
            await asyncio.sleep(wait_seconds)

            # Отправляем сводку за прошедший день
            yesterday = (kyiv_now() - timedelta(days=1)).strftime("%Y-%m-%d")
            stats = await db.get_daily_stats(yesterday)
            date_label = datetime.strptime(yesterday, "%Y-%m-%d").strftime("%d.%m.%Y")
            text = build_stats_text(stats, date_label)
            await notify_admin(text)
        except Exception as e:
            logger.error(f"Daily stats error: {e}")
            await asyncio.sleep(60)

# ──────────────────────────────────────────────
# КЛАВИАТУРА
# ──────────────────────────────────────────────
def main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="get_link"))
    builder.row(InlineKeyboardButton(text="📊 Мой прогресс", callback_data="stats"))
    return builder.as_markup()

# ──────────────────────────────────────────────
# ХЭНДЛЕРЫ БОТА
# ──────────────────────────────────────────────
@dp.message(CommandStart())
async def start(m: Message):
    await db.update_last_active(m.from_user.id)
    u = await db.get_user(m.from_user.id)

    # Получаем или создаём invite_link
    if not u:
        try:
            link_obj = await bot.create_chat_invite_link(CHANNEL_ID, name=f"ref_{m.from_user.id}")
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
                "⚠️ Бот не является администратором канала. Обратитесь к администратору.",
                reply_markup=main_kb(), parse_mode="HTML"
            )
            return
        invited = 0
    else:
        invite_link = u['invite_link']
        invited = u['invited_count']

    await m.answer(
        f"👋 Привет! Ты попал в один из лучших NSFW каналов.\n\n"
        f"🎁 Хочешь VIP? Пригласи {REQUIRED_INVITES} друга → получаешь VIP на {SUBSCRIPTION_DAYS} дней\n\n"
        f"Твоя ссылка: <code>{invite_link}</code>\n\n"
        f"📊 Твой прогресс: {invited}/{REQUIRED_INVITES}\n\n"
        f"❓ Вопросы — пиши сюда",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "get_link")
async def get_link(c: CallbackQuery):
    await db.update_last_active(c.from_user.id)
    u = await db.get_user(c.from_user.id)
    if not u:
        try:
            link_obj = await bot.create_chat_invite_link(CHANNEL_ID, name=f"ref_{c.from_user.id}")
            invite_link = link_obj.invite_link
            await db.create_user(
                c.from_user.id, c.from_user.username,
                c.from_user.full_name, invite_link
            )
        except:
            return await c.answer("❌ Бот не является администратором канала!", show_alert=True)
    else:
        invite_link = u['invite_link']

    await c.message.answer(
        f"🔗 Твоя реферальная ссылка:\n<code>{invite_link}</code>",
        parse_mode="HTML"
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

    await c.message.answer(txt, parse_mode="HTML")
    await c.answer()

# ──────────────────────────────────────────────
# КОМАНДЫ АНАЛИТИКИ (только в группе)
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
    await m.reply(text, parse_mode="HTML")

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
    await m.reply(text, parse_mode="HTML")

@dp.message(Command("today"))
async def cmd_today(m: Message):
    if m.chat.id != ADMIN_GROUP_ID:
        return
    today = kyiv_now().strftime("%Y-%m-%d")
    stats = await db.get_daily_stats(today)
    date_label = kyiv_now().strftime("%d.%m.%Y")
    text = build_stats_text(stats, date_label)
    await m.reply(text, parse_mode="HTML")

# ──────────────────────────────────────────────
# ТРЕКИНГ ВХОДА / ВЫХОДА В КАНАЛ
# ──────────────────────────────────────────────
@dp.chat_member()
async def tracking(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID:
        return
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
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"VIP link error: {e}")
                    else:
                        try:
                            await bot.send_message(
                                ref['user_id'],
                                f"🎉 Новый участник по твоей ссылке! ({u['invited_count']}/{REQUIRED_INVITES})",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.warning(f"Ref notify error: {e}")

        # Логируем событие
        await db.log_channel_event(uid, uname, fname, 'join', referrer_id, referrer_name)

        # Уведомление в админ-группу
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

    # ── Выход (Анти-фейк) ───────────────────────
    elif old == ChatMemberStatus.MEMBER and new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        refr_id = await db.get_referrer_of(uid)

        # Логируем событие
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

        # Уведомление в админ-группу
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
    asyncio.create_task(start_server())
    asyncio.create_task(sub_scheduler())
    asyncio.create_task(daily_stats_scheduler())
    await dp.start_polling(
        bot,
        allowed_updates=["message", "chat_member", "callback_query"]
    )

if __name__ == "__main__":
    asyncio.run(main())
