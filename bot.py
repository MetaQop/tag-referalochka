import asyncio
import logging
from datetime import datetime, timezone, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatMemberUpdated, InlineKeyboardButton, CallbackQuery, ChatJoinRequest
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from config import (
    BOT_TOKEN, CHANNEL_ID, GROUP_ID, REQUIRED_INVITES, PORT,
    SUBSCRIPTION_DAYS, ADMIN_GROUP_ID, ADMIN_TOPIC_ID
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ID темы для вопросов от пользователей
SUPPORT_TOPIC_ID = 434

# ──────────────────────────────────────────────
# FSM — состояние ожидания вопроса
# ──────────────────────────────────────────────
class AskQuestion(StatesGroup):
    waiting_for_question = State()

# FSM — сценарий для новых заявок на вступление
class JoinRequest(StatesGroup):
    q1_age = State()       # Вопрос 1: возраст
    q2_source = State()    # Вопрос 2: откуда узнал
    q3_agree = State()     # Вопрос 3: согласие с правилами

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
    builder.row(InlineKeyboardButton(text="❓ Задать вопрос", callback_data="ask_question"))
    return builder.as_markup()

def age_confirm_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, мне 18+", callback_data="age_yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="age_no"),
    )
    return builder.as_markup()

def agree_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Согласен с правилами", callback_data="rules_agree"))
    builder.row(InlineKeyboardButton(text="❌ Отказываюсь", callback_data="rules_decline"))
    return builder.as_markup()

def skip_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_source"))
    return builder.as_markup()

def cancel_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_question"))
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

            # ── Многоступенчатые напоминания неактивным ──────────────────────
            # Стадии: (inactive_days, reminder_stage, текст)
            REMINDER_STAGES = [
                (3, 1,
                 "🔔 Эй! Прошло 3 дня, а ты ещё не в VIP.\n"
                 "Осталось пригласить <b>{remaining}</b> друзей — и {days} дней эксклюзива твои 🔥\n\n"
                 "Твоя ссылка:\n<code>{link}</code>"),
                (6, 2,
                 "👀 Ты ещё не воспользовался своим шансом на бесплатный VIP!\n\n"
                 "Осталось пригласить <b>{remaining}</b> человек — это буквально пара сообщений друзьям 💬\n\n"
                 "Ссылка:\n<code>{link}</code>"),
                (9, 3,
                 "⚡ Последнее напоминание — место в VIP ещё твоё!\n\n"
                 "Всего <b>{remaining}</b> приглашения и <b>{days} дней</b> эксклюзивного контента бесплатно.\n\n"
                 "Ссылка:\n<code>{link}</code>"),
            ]

            for inactive_days, stage, template in REMINDER_STAGES:
                for user in await db.get_inactive_users_to_remind_stage(inactive_days, stage):
                    remaining = max(0, REQUIRED_INVITES - user['invited_count'])
                    text = template.format(
                        remaining=remaining,
                        days=SUBSCRIPTION_DAYS,
                        link=user['invite_link'],
                    )
                    try:
                        await bot.send_message(user['user_id'], text)
                        await db.mark_reminder_stage(user['user_id'], stage)
                    except Exception as e:
                        logger.warning(f"Reminder stage {stage} error {user['user_id']}: {e}")

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
# ХЭНДЛЕРЫ — /start
# ──────────────────────────────────────────────
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
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

# ──────────────────────────────────────────────
# ХЭНДЛЕРЫ — кнопки
# ──────────────────────────────────────────────
@dp.callback_query(F.data == "get_link")
async def get_link(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
    await c.message.answer(f"🔗 Твоя реферальная ссылка:\n<code>{invite_link}</code>")
    await c.answer()

@dp.callback_query(F.data == "stats")
async def stats_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
# ХЭНДЛЕРЫ — вопрос администраторам
# ──────────────────────────────────────────────
@dp.callback_query(F.data == "ask_question")
async def ask_question_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AskQuestion.waiting_for_question)
    await c.message.answer(
        "✍️ Напиши свой вопрос — и мы передадим его администраторам.\n\n"
        "Можно написать текст, отправить фото или видео.",
        reply_markup=cancel_kb()
    )
    await c.answer()

@dp.callback_query(F.data == "cancel_question")
async def cancel_question(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer("Отменено. Возвращайся если будут вопросы 👍", reply_markup=main_kb())
    await c.answer()

@dp.message(AskQuestion.waiting_for_question)
async def receive_question(m: Message, state: FSMContext):
    await state.clear()

    user = m.from_user
    name = html.quote(user.full_name or "")
    username_str = f" (@{user.username})" if user.username else ""
    header = (
        f"❓ <b>Вопрос от пользователя</b>\n"
        f"👤 <a href='tg://user?id={user.id}'>{name}</a>{username_str}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"─────────────────"
    )

    try:
        # Отправляем заголовок с инфо о пользователе
        await bot.send_message(
            ADMIN_GROUP_ID,
            header,
            message_thread_id=SUPPORT_TOPIC_ID,
        )
        # Пересылаем само сообщение (текст, фото, видео, голос — всё что угодно)
        await m.forward(chat_id=ADMIN_GROUP_ID, message_thread_id=SUPPORT_TOPIC_ID)

        await m.answer(
            "✅ Вопрос отправлен! Администраторы ответят вам в ближайшее время.",
            reply_markup=main_kb()
        )
    except Exception as e:
        logger.error(f"Forward question error from {user.id}: {e}")
        await m.answer(
            "❌ Не удалось отправить вопрос. Попробуйте позже.",
            reply_markup=main_kb()
        )

# ──────────────────────────────────────────────
# ЗАЯВКИ НА ВСТУПЛЕНИЕ — chat_join_request
# ──────────────────────────────────────────────
@dp.chat_join_request(F.chat.id == CHANNEL_ID)
async def handle_join_request(request: ChatJoinRequest, state: FSMContext):
    uid = request.from_user.id
    fname = request.from_user.full_name
    uname = request.from_user.username

    logger.info(f"Join request from {uid} ({fname})")

    # Сохраняем заявку в базу
    await db.save_join_request(uid, uname, fname)

    # Сохраняем user_id заявителя в FSM чтобы потом одобрить/отклонить
    user_state = dp.fsm.resolve_context(bot, uid, uid)
    await user_state.update_data(join_request_chat_id=CHANNEL_ID)
    await user_state.set_state(JoinRequest.q1_age)

    try:
        await bot.send_message(
            uid,
            f"👋 Привет, <b>{html.quote(fname or 'друг')}</b>!\n\n"
            "Ты подал заявку на вступление в канал. Нам нужно задать тебе пару коротких вопросов.\n\n"
            "━━━━━━━━━━━━━━\n"
            "❓ <b>Вопрос 1 из 3</b>\n\n"
            "Подтверди, что тебе есть <b>18 лет</b>.\n"
            "Канал содержит контент для взрослых.",
            reply_markup=age_confirm_kb(),
        )
    except Exception as e:
        logger.warning(f"Cannot send DM to {uid}: {e}")
        # Если бот не может написать — сразу отклоняем
        try:
            await request.decline()
        except Exception:
            pass

# ── Q1: возраст ──────────────────────────────────────────────────────────────
@dp.callback_query(JoinRequest.q1_age, F.data == "age_no")
async def join_age_no(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(
        "❌ К сожалению, доступ разрешён только совершеннолетним.\n"
        "Твоя заявка отклонена."
    )
    try:
        await bot.decline_chat_join_request(CHANNEL_ID, c.from_user.id)
        await db.resolve_join_request(c.from_user.id, 'declined_age')
    except Exception as e:
        logger.error(f"Decline join request error {c.from_user.id}: {e}")

    # Уведомление в админку
    user_link = fmt_user(c.from_user.username, c.from_user.full_name, c.from_user.id)
    await notify_admin(
        f"🚫 <b>Заявка отклонена (несовершеннолетний)</b>\n👤 {user_link}"
    )
    await c.answer()

@dp.callback_query(JoinRequest.q1_age, F.data == "age_yes")
async def join_age_yes(c: CallbackQuery, state: FSMContext):
    await state.set_state(JoinRequest.q2_source)
    await c.message.edit_text(
        "✅ Отлично!\n\n"
        "━━━━━━━━━━━━━━\n"
        "❓ <b>Вопрос 2 из 3</b>\n\n"
        "Откуда ты узнал о нашем канале?\n\n"
        "<i>Напиши ответ текстом (например: от друга, реклама, поиск Telegram...)\n"
        "Или нажми «Пропустить»</i>",
        reply_markup=skip_kb(),
    )
    await c.answer()

# ── Q2: пропуск ──────────────────────────────────────────────────────────────
async def _go_to_q3(target, state: FSMContext, source="—"):
    await state.update_data(source=source)
    await state.set_state(JoinRequest.q3_agree)
    text = (
        "━━━━━━━━━━━━━━\n"
        "❓ <b>Вопрос 3 из 3</b>\n\n"
        "Ты попадёшь на самый лучший NSFW канал, где есть:\n\n"
        "😈 Альтушки\n"
        "📖 Комиксы\n"
        "🔞 Порно\n"
        "🖥 3Д\n"
        "🎮 Игры\n"
        "🎌 Аниме\n\n"
        "Так же ты можешь получить доступ к приватке бесплатно, воспользовавшись реферальной системой 🎁\n\n"
        "Принимаешь условия и вступаешь?"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=agree_kb())
        await target.answer()
    else:
        await target.answer(text, reply_markup=agree_kb())

# ── Q2: пропуск кнопкой ──────────────────────────────────────────────────────
@dp.callback_query(JoinRequest.q2_source, F.data == "skip_source")
async def join_source_skip(c: CallbackQuery, state: FSMContext):
    await _go_to_q3(c, state, source="не указал")

# ── Q2: источник (текстовый ответ) ──────────────────────────────────────────
@dp.message(JoinRequest.q2_source)
async def join_source_answer(m: Message, state: FSMContext):
    await _go_to_q3(m, state, source=m.text or "—")

# ── Q3: согласие — отказ ─────────────────────────────────────────────────────
@dp.callback_query(JoinRequest.q3_agree, F.data == "rules_decline")
async def join_rules_decline(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(
        "❌ Без согласия с правилами вступление невозможно.\n"
        "Твоя заявка отклонена. Возвращайся когда будешь готов!"
    )
    try:
        await bot.decline_chat_join_request(CHANNEL_ID, c.from_user.id)
        await db.resolve_join_request(c.from_user.id, 'declined_rules')
    except Exception as e:
        logger.error(f"Decline join request error {c.from_user.id}: {e}")

    user_link = fmt_user(c.from_user.username, c.from_user.full_name, c.from_user.id)
    await notify_admin(
        f"🚫 <b>Заявка отклонена (не согласен с правилами)</b>\n👤 {user_link}"
    )
    await c.answer()

# ── Q3: согласие — принятие ──────────────────────────────────────────────────
@dp.callback_query(JoinRequest.q3_agree, F.data == "rules_agree")
async def join_rules_agree(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    source = data.get("source", "—")
    await state.clear()

    uid = c.from_user.id
    fname = c.from_user.full_name
    uname = c.from_user.username

    try:
        await bot.approve_chat_join_request(CHANNEL_ID, uid)
        await db.resolve_join_request(uid, 'approved', source)
    except Exception as e:
        logger.error(f"Approve join request error {uid}: {e}")
        await c.message.edit_text("❌ Ошибка при одобрении заявки. Напиши администратору.")
        await c.answer()
        return

    await c.message.edit_text(
        "🎉 <b>Заявка одобрена!</b>\n\n"
        "Добро пожаловать в канал 🔥\n\n"
        "Кстати, у нас есть реферальная программа:\n"
        f"🎁 Пригласи <b>{REQUIRED_INVITES}</b> друзей → получи VIP на <b>{SUBSCRIPTION_DAYS} дней</b>\n\n"
        "Нажми /start чтобы получить свою реферальную ссылку!"
    )

    # Уведомление в админку с деталями анкеты
    user_link = fmt_user(uname, fname, uid)
    await notify_admin(
        f"✅ <b>Заявка одобрена</b>\n"
        f"👤 {user_link}\n"
        f"📍 Источник: <i>{html.quote(source)}</i>"
    )
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

            await bot.send_message(uid, welcome_text, reply_markup=main_kb())
        except Exception as e:
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
        allowed_updates=["message", "chat_member", "callback_query", "chat_join_request"]
    )

if __name__ == "__main__":
    asyncio.run(main())
