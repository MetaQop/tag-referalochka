import asyncio
import logging
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import BOT_TOKEN, CHANNEL_ID, GROUP_ID, REQUIRED_INVITES

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ──────────────────────────────────────────────
# КЛАВИАТУРА (ГЛАВНОЕ МЕНЮ)
# ──────────────────────────────────────────────
def main_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Получить ссылку", callback_data="get_link"))
    builder.row(InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"))
    return builder.as_markup()

# ══════════════════════════════════════════════
# ОБРАБОТЧИКИ СООБЩЕНИЙ
# ══════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Приветствие и объяснение правил."""
    safe_name = html.quote(message.from_user.full_name)
    
    welcome_text = (
        f"👋 Привет, <b>{safe_name}</b>!\n\n"
        f"Это бот доступа в <b>Закрытую Группу</b>. Правила просты:\n"
        f"1️⃣ Нажми кнопку <b>'Получить ссылку'</b>.\n"
        f"2️⃣ Пригласи по ней <b>{REQUIRED_INVITES} друзей</b> в наш Канал.\n"
        f"3️⃣ Бот автоматически пришлет тебе доступ в Группу!"
    )
    
    await message.answer(welcome_text, reply_markup=main_menu_kb(), parse_mode="HTML")

# ══════════════════════════════════════════════
# ОБРАБОТЧИКИ КНОПОК (CALLBACK)
# ══════════════════════════════════════════════

@dp.callback_query(F.data == "get_link")
async def handle_get_link(callback: CallbackQuery):
    """Выдача или генерация персональной ссылки."""
    user = callback.from_user
    user_data = await db.get_user(user.id)

    # Если пользователя еще нет в базе — создаем его
    if not user_data:
        try:
            invite_link_obj = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                name=f"ref_{user.id}",
                creates_join_request=False,
            )
            invite_url = invite_link_obj.invite_link
            
            await db.create_user(
                user_id=user.id,
                username=user.username or "",
                full_name=user.full_name,
                invite_link=invite_url,
            )
        except Exception as e:
            logger.error(f"Ошибка создания ссылки: {e}")
            await callback.answer("❌ Ошибка: Бот не админ в канале!", show_alert=True)
            return
    else:
        invite_url = user_data["invite_link"]

    await callback.message.answer(
        f"🔗 <b>Твоя персональная ссылка:</b>\n\n<code>{invite_url}</code>\n\n"
        f"Пересылай её друзьям. Когда {REQUIRED_INVITES} чел. вступят, ты получишь награду!",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "my_stats")
async def handle_stats(callback: CallbackQuery):
    """Показ статистики приглашений."""
    user_data = await db.get_user(callback.from_user.id)
    
    if not user_data:
        await callback.answer("Сначала нажми 'Получить ссылку'!", show_alert=True)
        return

    invited = user_data["invited_count"]
    remaining = max(0, REQUIRED_INVITES - invited)
    status = "✅ Выполнено!" if user_data["completed"] else f"⏳ Осталось: {remaining}"

    stats_text = (
        f"📊 <b>Твоя статистика:</b>\n\n"
        f"👥 Приглашено: <b>{invited}</b>\n"
        f"🎯 Цель: <b>{REQUIRED_INVITES}</b>\n"
        f"📝 Статус: <b>{status}</b>"
    )
    
    await callback.message.answer(stats_text, parse_mode="HTML")
    await callback.answer()

# ══════════════════════════════════════════════
# ТРЕКИНГ ВСТУПЛЕНИЙ
# ══════════════════════════════════════════════

@dp.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated) -> None:
    """Отслеживает вступление в канал и начисляет баллы."""
    if event.chat.id != CHANNEL_ID:
        return

    old_s = event.old_chat_member.status
    new_s = event.new_chat_member.status

    # ИСПРАВЛЕНО: ChatMemberStatus.KICKED вместо BANNED
    was_not_member = old_s in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    is_now_member = new_s == ChatMemberStatus.MEMBER

    if was_not_member and is_now_member and event.invite_link:
        used_url = event.invite_link.invite_link
        referrer = await db.get_user_by_invite_link(used_url)
        
        if not referrer:
            return

        referrer_id = referrer["user_id"]
        new_member = event.new_chat_member.user

        if new_member.id == referrer_id:
            return

        # Защита от дублей
        if await db.is_referral_counted(referrer_id, new_member.id):
            return

        await db.add_referral(referrer_id=referrer_id, referred_id=new_member.id)
        new_count = await db.increment_invited_count(referrer_id)

        # Уведомляем пригласившего
        if new_count < REQUIRED_INVITES:
            await bot.send_message(
                chat_id=referrer_id,
                text=f"🎉 По твоей ссылке вступил новый человек! Прогресс: <b>{new_count}/{REQUIRED_INVITES}</b>",
                parse_mode="HTML"
            )
        else:
            await _grant_group_access(referrer_id)

async def _grant_group_access(user_id: int) -> None:
    """Выдача доступа при достижении цели."""
    user_data = await db.get_user(user_id)
    if not user_data or user_data["completed"]:
        return

    if await db.mark_completed(user_id):
        try:
            group_invite = await bot.create_chat_invite_link(
                chat_id=GROUP_ID,
                name=f"reward_{user_id}",
                member_limit=1,
                creates_join_request=False,
            )
            
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🏆 <b>Поздравляем! Ты выполнил задание!</b>\n\n"
                    "Вот твоя ссылка в закрытую Группу:\n"
                    f"🔐 <b><a href='{group_invite.invite_link}'>ВСТУПИТЬ В ГРУППУ</a></b>\n\n"
                    "<i>Ссылка одноразовая, не передавай её никому.</i>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка выдачи награды: {e}")

async def main() -> None:
    await db.init_db()
    logger.info("Бот запущен и готов к работе.")
    await dp.start_polling(bot, allowed_updates=["message", "chat_member", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
