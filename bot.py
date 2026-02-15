import asyncio
import logging
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import BOT_TOKEN, CHANNEL_ID, GROUP_ID, REQUIRED_INVITES, PORT, SUBSCRIPTION_DAYS

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ──────────────────────────────────────────────
# СЕРВЕР И ПЛАНИРОВЩИК
# ──────────────────────────────────────────────
async def handle_health(request): return web.Response(text="OK")

async def start_server():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

async def sub_scheduler():
    while True:
        try:
            # Предупреждение за 3 дня
            for user in await db.get_users_to_notify(3):
                exp = datetime.fromisoformat(user['expiry_date']).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(user['user_id'], f"⚠️ Подписка истекает <b>{exp}</b>. Через 3 дня вы будете исключены.")
                    await db.mark_notified(user['user_id'])
                except: pass

            # Кик просроченных
            for uid in await db.get_expired_users():
                try:
                    await bot.ban_chat_member(GROUP_ID, uid)
                    await bot.unban_chat_member(GROUP_ID, uid)
                    await db.reset_user_status(uid)
                    await bot.send_message(uid, "🔴 Срок подписки истек. Вы исключены из группы.")
                except Exception as e: logger.error(f"Kick error {uid}: {e}")
        except Exception as e: logger.error(f"Sched error: {e}")
        await asyncio.sleep(3600)

# ──────────────────────────────────────────────
# ЛОГИКА БОТА
# ──────────────────────────────────────────────
def main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Получить ссылку", callback_data="get_link"))
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="stats"))
    return builder.as_markup()

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(f"👋 Привет, {html.quote(m.from_user.full_name)}!\nПригласи {REQUIRED_INVITES} друзей в канал и получи доступ в закрытую группу на {SUBSCRIPTION_DAYS} дней.", reply_markup=main_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "get_link")
async def get_link(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if not u:
        try:
            l = await bot.create_chat_invite_link(CHANNEL_ID, name=f"ref_{c.from_user.id}")
            await db.create_user(c.from_user.id, c.from_user.username, c.from_user.full_name, l.invite_link)
            url = l.invite_link
        except: return await c.answer("Бот не админ!", show_alert=True)
    else: url = u['invite_link']
    await c.message.answer(f"🔗 Твоя ссылка: <code>{url}</code>", parse_mode="HTML"); await c.answer()

@dp.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    if not u: return await c.answer("Получите ссылку!")
    txt = f"📊 Друзей: {u['invited_count']}/{REQUIRED_INVITES}\n"
    if u['completed'] and u['expiry_date']:
        exp = datetime.fromisoformat(u['expiry_date']).strftime("%d.%m.%Y %H:%M")
        txt += f"🔐 Доступ до: <b>{exp}</b>"
    await c.message.answer(txt, parse_mode="HTML"); await c.answer()

@dp.chat_member()
async def tracking(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID: return
    old, new = event.old_chat_member.status, event.new_chat_member.status
    uid = event.new_chat_member.user.id

    # Вступление
    if old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER:
        if not event.invite_link: return
        ref = await db.get_user_by_invite_link(event.invite_link.invite_link)
        if ref and uid != ref['user_id']:
            if await db.add_referral(ref['user_id'], uid):
                u = await db.get_user(ref['user_id'])
                if u['invited_count'] >= REQUIRED_INVITES and not u['completed']:
                    await db.set_expiry(ref['user_id'], SUBSCRIPTION_DAYS)
                    l = await bot.create_chat_invite_link(GROUP_ID, member_limit=1)
                    await bot.send_message(ref['user_id'], f"🏆 Готово! Доступ на {SUBSCRIPTION_DAYS} дней:\n{l.invite_link}")
                else:
                    await bot.send_message(ref['user_id'], f"🎉 Новый участник! ({u['invited_count']}/{REQUIRED_INVITES})")

    # Выход (Анти-фейк)
    elif old == ChatMemberStatus.MEMBER and new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        refr_id = await db.get_referrer_of(uid)
        if refr_id:
            if await db.remove_referral(refr_id, uid):
                u = await db.get_user(refr_id)
                await bot.send_message(refr_id, f"📉 Участник покинул канал. Балл аннулирован ({u['invited_count']}/{REQUIRED_INVITES})")

async def main():
    await db.init_db()
    asyncio.create_task(start_server())
    asyncio.create_task(sub_scheduler())
    await dp.start_polling(bot, allowed_updates=["message", "chat_member", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
