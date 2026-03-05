"""
Реферальный бот TAG — с поддержкой Render.com (webhook) и Google Sheets.
Основной функционал не изменён. Добавлено:
  - Webhook режим для Render (автоматически, если задан RENDER_EXTERNAL_URL)
  - Логирование всех реферальных событий в Google Sheets
  - /adminstats — статистика для Олега
"""

import asyncio
import logging
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import database as db
from config import (
    BOT_TOKEN, CHANNEL_ID, GROUP_ID,
    REQUIRED_INVITES, SUBSCRIPTION_DAYS,
    WEBHOOK_URL, WEBHOOK_PATH, PORT,
    SHEETS_ENABLED,
)

# Sheets импортируем только если включено
if SHEETS_ENABLED:
    import sheets

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ID Олега — может использовать /adminstats (добавь свой Telegram ID)
ADMIN_IDS = {int(x) for x in "".join([]).split(",") if x}  # задай через env ADMIN_IDS

import os
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# ──────────────────────────────────────────────
# HEALTH CHECK (для Render)
# ──────────────────────────────────────────────
async def handle_health(request):
    return web.Response(text="OK")

# ──────────────────────────────────────────────
# ПЛАНИРОВЩИК (уведомления и кики)
# ──────────────────────────────────────────────
async def sub_scheduler():
    while True:
        try:
            # Предупреждение за 3 дня
            for user in await db.get_users_to_notify(3):
                exp = datetime.fromisoformat(user["expiry_date"]).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(
                        user["user_id"],
                        f"⚠️ Подписка истекает <b>{exp}</b>. Через 3 дня вы будете исключены.\n"
                        f"Пригласи ещё друзей, чтобы продлить доступ!",
                        parse_mode="HTML",
                    )
                    await db.mark_notified(user["user_id"])
                except Exception:
                    pass

            # Кик просроченных
            for row in await db.get_expired_users():
                uid  = row["user_id"]
                name = row.get("full_name", str(uid))
                try:
                    await bot.ban_chat_member(GROUP_ID, uid)
                    await bot.unban_chat_member(GROUP_ID, uid)
                    await db.reset_user_status(uid)
                    await bot.send_message(uid, "🔴 Срок подписки истёк. Вы исключены из группы.")
                    if SHEETS_ENABLED:
                        sheets.log_access_expired(uid, name)
                except Exception as e:
                    logger.error(f"Kick error {uid}: {e}")

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(3600)

# ──────────────────────────────────────────────
# КЛАВИАТУРА
# ──────────────────────────────────────────────
def main_kb():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔗 Получить ссылку",  callback_data="get_link"))
    b.row(InlineKeyboardButton(text="📊 Статистика",       callback_data="stats"))
    return b.as_markup()

# ──────────────────────────────────────────────
# КОМАНДЫ
# ──────────────────────────────────────────────
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        f"👋 Привет, {html.quote(m.from_user.full_name)}!\n\n"
        f"Пригласи <b>{REQUIRED_INVITES}</b> друзей в канал и получи доступ "
        f"в закрытую группу на <b>{SUBSCRIPTION_DAYS}</b> дней.",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )

@dp.message(Command("adminstats"))
async def admin_stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    stats = await db.get_total_stats()
    top   = await db.get_top_referrers(5)

    top_text = ""
    for i, u in enumerate(top, 1):
        uname = f"@{u['username']}" if u["username"] else u["full_name"]
        top_text += f"  {i}. {uname} — {u['invited_count']} чел.\n"

    await m.answer(
        f"📊 <b>Статистика реферального бота</b>\n\n"
        f"👤 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"🔗 Всего рефералов: <b>{stats['total_referrals']}</b>\n"
        f"✅ Получили доступ: <b>{stats['total_completed']}</b>\n"
        f"🟢 Активных сейчас: <b>{stats['active_now']}</b>\n\n"
        f"🏆 <b>Топ 5 рефереров:</b>\n{top_text}",
        parse_mode="HTML",
    )

# ──────────────────────────────────────────────
# CALLBACK КНОПКИ
# ──────────────────────────────────────────────
@dp.callback_query(F.data == "get_link")
async def get_link(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if not u:
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, name=f"ref_{c.from_user.id}")
            await db.create_user(c.from_user.id, c.from_user.username, c.from_user.full_name, link.invite_link)
            url = link.invite_link
        except Exception:
            return await c.answer("Бот не является администратором канала!", show_alert=True)
    else:
        url = u["invite_link"]

    await c.message.answer(
        f"🔗 Твоя реферальная ссылка:\n<code>{url}</code>\n\n"
        f"Поделись ей — каждый вступивший засчитывается тебе!",
        parse_mode="HTML",
    )
    await c.answer()

@dp.callback_query(F.data == "stats")
async def stats_cb(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if not u:
        return await c.answer("Сначала получи ссылку!", show_alert=True)

    txt = f"📊 <b>Твоя статистика</b>\n\nПриглашено: <b>{u['invited_count']}/{REQUIRED_INVITES}</b>\n"
    if u["completed"] and u["expiry_date"]:
        exp = datetime.fromisoformat(u["expiry_date"]).strftime("%d.%m.%Y %H:%M")
        txt += f"🔐 Доступ активен до: <b>{exp}</b>"
    else:
        remaining = REQUIRED_INVITES - u["invited_count"]
        txt += f"⏳ До доступа осталось: <b>{remaining}</b> чел."

    await c.message.answer(txt, parse_mode="HTML")
    await c.answer()

# ──────────────────────────────────────────────
# ОТСЛЕЖИВАНИЕ ВСТУПЛЕНИЙ/ВЫХОДОВ
# ──────────────────────────────────────────────
@dp.chat_member()
async def tracking(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID:
        return

    old = event.old_chat_member.status
    new = event.new_chat_member.status
    uid = event.new_chat_member.user
    user_id = uid.id

    # ── ВСТУПЛЕНИЕ ──────────────────────────
    if old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER:
        if not event.invite_link:
            return

        ref = await db.get_user_by_invite_link(event.invite_link.invite_link)
        if not ref or user_id == ref["user_id"]:
            return

        added = await db.add_referral(ref["user_id"], user_id)
        if not added:
            return

        u = await db.get_user(ref["user_id"])
        count = u["invited_count"]

        # Логируем в Sheets
        if SHEETS_ENABLED:
            sheets.log_referral_join(
                referred_id=user_id,
                referred_name=uid.full_name,
                referred_username=uid.username or "",
                referrer_id=ref["user_id"],
                referrer_name=ref["full_name"],
                referrer_username=ref["username"] or "",
                referrer_total=count,
            )

        if count >= REQUIRED_INVITES and not u["completed"]:
            # Выдаём доступ
            expiry = await db.set_expiry(ref["user_id"], SUBSCRIPTION_DAYS)
            group_link = await bot.create_chat_invite_link(GROUP_ID, member_limit=1)
            await bot.send_message(
                ref["user_id"],
                f"🏆 Поздравляем! Ты пригласил {REQUIRED_INVITES} друзей.\n\n"
                f"Вот твоя ссылка в закрытую группу на {SUBSCRIPTION_DAYS} дней:\n"
                f"{group_link.invite_link}",
            )
            if SHEETS_ENABLED:
                sheets.log_access_granted(
                    user_id=ref["user_id"],
                    user_name=ref["full_name"],
                    username=ref["username"] or "",
                    invited_count=count,
                    expiry_date=datetime.fromisoformat(expiry).strftime("%d.%m.%Y"),
                )
        else:
            remaining = REQUIRED_INVITES - count
            await bot.send_message(
                ref["user_id"],
                f"🎉 Новый участник по твоей ссылке! ({count}/{REQUIRED_INVITES})\n"
                f"⏳ Ещё {remaining} чел. до доступа.",
            )

    # ── ВЫХОД (анти-фейк) ────────────────────
    elif old == ChatMemberStatus.MEMBER and new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        ref_info = await db.get_referrer_info(user_id)
        if not ref_info:
            return

        removed = await db.remove_referral(ref_info["user_id"], user_id)
        if not removed:
            return

        u = await db.get_user(ref_info["user_id"])
        await bot.send_message(
            ref_info["user_id"],
            f"📉 Участник покинул канал — балл аннулирован. ({u['invited_count']}/{REQUIRED_INVITES})",
        )

        if SHEETS_ENABLED:
            sheets.log_referral_leave(
                referred_id=user_id,
                referred_name=uid.full_name,
                referrer_id=ref_info["user_id"],
                referrer_name=ref_info["full_name"],
                referrer_total=u["invited_count"],
            )

# ──────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────
async def main():
    await db.init_db()
    asyncio.create_task(sub_scheduler())

    if WEBHOOK_URL:
        # ── WEBHOOK РЕЖИМ (Render.com) ──────
        logger.info(f"Webhook mode: {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL)

        app = web.Application()
        app.router.add_get("/health", handle_health)

        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Server started on port {PORT}")

        # Держим сервер живым
        await asyncio.Event().wait()
    else:
        # ── POLLING РЕЖИМ (локально) ────────
        logger.info("Polling mode (local)")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "chat_member", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
