import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    BotCommand,
    LabeledPrice,
    PreCheckoutQuery,
    Message
)
from aiogram.filters import Command

TOKEN = "8687253696:AAGxeaingqzbCIGPqWsziXr4VYN0Bpopmm8"
ADMIN_ID = 6705555401

# ⭐ Stars token = пустая строка
PROVIDER_TOKEN = ""

logging.basicConfig(level=logging.INFO)

bot = Bot(TOKEN)
dp = Dispatcher()

db = sqlite3.connect("bot.db")
cur = db.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    premium TEXT DEFAULT 'normal',
    expires TEXT,
    downloads INTEGER DEFAULT 0,
    last_reset TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS stats(
    total_stars INTEGER DEFAULT 0,
    gold INTEGER DEFAULT 0,
    diamond INTEGER DEFAULT 0
)
""")

if cur.execute("SELECT COUNT(*) FROM stats").fetchone()[0] == 0:
    cur.execute("INSERT INTO stats VALUES(0,0,0)")
db.commit()


# ================= COMMANDS =================

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск"),
        BotCommand(command="download", description="Скачать видео"),
        BotCommand(command="premium", description="Премиум"),
        BotCommand(command="profile", description="Профиль"),
        BotCommand(command="admin", description="Админ панель")
    ])


# ================= HELPERS =================

def get_user(user_id):
    row = cur.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users(id,last_reset) VALUES(?,?)",
            (user_id, datetime.now().date())
        )
        db.commit()
        return get_user(user_id)
    return row


def reset_if_needed(user):
    today = str(datetime.now().date())
    if user[4] != today:
        cur.execute("UPDATE users SET downloads=0,last_reset=? WHERE id=?",
                    (today, user[0]))
        db.commit()


def limit_for(level):
    return {
        "normal": 4,
        "gold": 10,
        "diamond": 999999
    }[level]


# ================= START =================

@dp.message(Command("start"))
async def start(msg: Message):
    get_user(msg.from_user.id)

    await msg.answer(
        "👋 Привет!\n\n"
        "Этот бот скачивает видео из:\n"
        "• TikTok\n"
        "• Instagram\n\n"
        "❌ YouTube Shorts НЕ поддерживаются.\n\n"
        "Команды:\n"
        "/download — скачать видео\n"
        "/premium — купить премиум\n"
        "/profile — профиль"
    )


# ================= PROFILE =================

@dp.message(Command("profile"))
async def profile(msg: Message):
    u = get_user(msg.from_user.id)
    reset_if_needed(u)

    await msg.answer(
        f"👤 Профиль\n\n"
        f"Премиум: {u[1]}\n"
        f"Скачиваний сегодня: {u[3]}"
    )


# ================= DOWNLOAD =================

@dp.message(Command("download"))
async def download_cmd(msg: Message):
    await msg.answer("📩 Отправь ссылку на TikTok или Instagram")


@dp.message(F.text.startswith("http"))
async def handle_link(msg: Message):
    user = get_user(msg.from_user.id)
    reset_if_needed(user)

    limit = limit_for(user[1])

    if user[3] >= limit:
        await msg.answer("❌ Дневной лимит достигнут")
        return

    cur.execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (user[0],))
    db.commit()

    if "youtube" in msg.text or "youtu.be" in msg.text:
        await msg.answer("❌ YouTube Shorts не поддерживаются")
        return

    await msg.answer("⏳ Видео добавлено в очередь...")


# ================= PREMIUM INFO =================

@dp.message(Command("premium"))
async def premium(msg: Message):
    await msg.answer(
        "💎 Премиум уровни:\n\n"
        "Обычный — 4 видео/день\n\n"
        "🥇 GOLD — 10 видео/день\n"
        "120 ⭐ / 30 дней\n\n"
        "💎 DIAMOND — без лимита\n"
        "250 ⭐ / 90 дней\n\n"
        "Для покупки:\n"
        "/buy_gold\n"
        "/buy_diamond"
    )


# ================= BUY =================

@dp.message(Command("buy_gold"))
async def buy_gold(msg: Message):
    prices = [LabeledPrice(label="GOLD", amount=120)]
    await bot.send_invoice(
        msg.chat.id,
        title="GOLD Premium",
        description="30 дней",
        payload="gold",
        provider_token=PROVIDER_TOKEN,
        currency="XTR",
        prices=prices
    )


@dp.message(Command("buy_diamond"))
async def buy_diamond(msg: Message):
    prices = [LabeledPrice(label="DIAMOND", amount=250)]
    await bot.send_invoice(
        msg.chat.id,
        title="DIAMOND Premium",
        description="90 дней",
        payload="diamond",
        provider_token=PROVIDER_TOKEN,
        currency="XTR",
        prices=prices
    )


# ================= PAYMENT =================

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def payment(msg: Message):
    payload = msg.successful_payment.invoice_payload
    user = get_user(msg.from_user.id)

    if payload == "gold":
        expires = datetime.now() + timedelta(days=30)
        cur.execute("UPDATE users SET premium='gold', expires=? WHERE id=?",
                    (expires, user[0]))
        cur.execute("UPDATE stats SET total_stars=total_stars+120,gold=gold+1")

    if payload == "diamond":
        expires = datetime.now() + timedelta(days=90)
        cur.execute("UPDATE users SET premium='diamond', expires=? WHERE id=?",
                    (expires, user[0]))
        cur.execute("UPDATE stats SET total_stars=total_stars+250,diamond=diamond+1")

    db.commit()
    await msg.answer("✅ Премиум успешно активирован!")


# ================= ADMIN =================

@dp.message(Command("admin"))
async def admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return

    users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    stats = cur.execute("SELECT * FROM stats").fetchone()

    await msg.answer(
        f"👑 Админ панель\n\n"
        f"Пользователей: {users}\n"
        f"⭐ Заработано: {stats[0]}\n"
        f"GOLD: {stats[1]}\n"
        f"DIAMOND: {stats[2]}"
    )


# ================= MAIN =================

async def main():
    await set_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())