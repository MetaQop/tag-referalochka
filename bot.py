import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import BOT_TOKEN, CHANNEL_ID, GROUP_ID, REQUIRED_INVITES, PORT

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ──────────────────────────────────────────────
# ВЕБ-СЕРВЕР ДЛЯ RENDER (Health Check)
# ──────────────────────────────────────────────
async def handle_health_check(request):
    return web.Response(text="Bot is alive!", status=200)

async def start_webhook_server():
    app = web.Application()
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health-check сервер запущен на порту {PORT}")

# ──────────────────────────────────────────────
# КЛАВИАТУРА
# ──────────────────────────────────────────────
def main_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Получить ссылку", callback_data="get_link"))
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="my_stats"))
    return builder.as_markup()

# ──────────────────────────────────────────────
# ОБРАБОТЧИКИ
# ──────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    safe_name = html.quote(message.from_user.full_name)
    text = (
        f"👋 Привет, <b>{safe_name}</b>!\n\n"
        f"Чтобы попасть в закрытую группу, пригласи <b>{REQUIRED_INVITES}</b> друзей в канал.\n"
        "Используй кнопки ниже для работы."
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "get_link")
async def handle_get_link(callback: CallbackQuery):
    user = callback.from_user
    user_data = await db.get_user(user.id)

    if not user_data:
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, name=f"ref_{user.id}")
            await db.create_user(user.id, user.username or "", user.full_name, link.invite_link)
            invite_url = link.invite_link
        except Exception as e:
            logger.error(f"Ошибка ссылки: {e}")
            await callback.answer("Ошибка! Бот не админ в канале.", show_alert=True)
            return
    else:
        invite_url = user_data["invite_link"]

    await callback.message.answer(f"🔗 Твоя ссылка:\n<code>{invite_url}</code>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "my_stats")
async def handle_stats(callback: CallbackQuery):
    user_data = await db.get_user(callback.from_user.id)
    if not user_data:
        await callback.answer("Нажми сначала 'Получить ссылку'", show_alert=True)
        return

    invited = user_data["invited_count"]
    remaining = max(0, REQUIRED_INVITES - invited)
    text = (
        f"📊 <b>Твоя статистика:</b>\n\n"
        f"Приглашено: {invited}\n"
        f"Осталось: {remaining}\n"
        f"Статус: {'✅ Готово' if user_data['completed'] else '⏳ В процессе'}"
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@dp.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated):
    if event.chat.id != CHANNEL_ID:
        return

    old, new = event.old_chat_member.status, event.new_chat_member.status
    # Замена BANNED на KICKED для aiogram 3.x
    if old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new == ChatMemberStatus.MEMBER:
        if not event.invite_link:
            return

        referrer = await db.get_user_by_invite_link(event.invite_link.invite_link)
        if not referrer or event.new_chat_member.user.id == referrer["user_id"]:
            return

        if await db.is_referral_counted(referrer["user_id"], event.new_chat_member.user.id):
            return

        await db.add_referral(referrer["user_id"], event.new_chat_member.user.id)
        count = await db.increment_invited_count(referrer["user_id"])

        if count >= REQUIRED_INVITES:
            await _grant_group_access(referrer["user_id"])
        else:
            await bot.send_message(referrer["user_id"], f"🎉 Новый участник! Прогресс: {count}/{REQUIRED_INVITES}")

async def _grant_group_access(user_id: int):
    user_data = await db.get_user(user_id)
    if user_data and not user_data["completed"]:
        if await db.mark_completed(user_id):
            link = await bot.create_chat_invite_link(GROUP_ID, member_limit=1)
            await bot.send_message(user_id, f"🏆 <b>Готово!</b> Ссылка в группу:\n{link.invite_link}", parse_mode="HTML")

async def main():
    await db.init_db()
    asyncio.create_task(start_webhook_server())
    await dp.start_polling(bot, allowed_updates=["message", "chat_member", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
