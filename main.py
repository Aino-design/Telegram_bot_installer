import asyncio
import os
import tempfile
import shutil
from datetime import datetime, timedelta

import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, FSInputFile

# ========= CONFIG =========

BOT_TOKEN = "8687253696:AAGxeaingqzbCIGPqWsziXr4VYN0Bpopmm8" or os.getenv("BOT_TOKEN")
ADMIN_ID = 6705555401

DB = "db.sqlite"

GOLD_PRICE = 120
DIAMOND_PRICE = 250

GOLD_DAYS = 30
DIAMOND_DAYS = 90

LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
queue = asyncio.Queue()

# ========= DATABASE =========

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            premium TEXT DEFAULT 'обычный',
            stars INTEGER DEFAULT 0,
            downloads INTEGER DEFAULT 0,
            reset TEXT,
            expires TEXT
        )""")
        await db.commit()

async def add_user(uid):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(id,reset) VALUES(?,?)",
            (uid, datetime.utcnow().isoformat())
        )
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c:
            return await c.fetchone()

async def set_premium(uid, level, days):
    exp = (datetime.utcnow()+timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET premium=?, expires=? WHERE id=?",
                         (level, exp, uid))
        await db.commit()

async def add_stars(uid, amount):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET stars=stars+? WHERE id=?",
                         (amount, uid))
        await db.commit()

# ========= DOWNLOAD =========

def dl(url, folder):
    y = {"format":"best","outtmpl":f"{folder}/video.%(ext)s",
         "merge_output_format":"mp4","quiet":True}
    with YoutubeDL(y) as ydl:
        ydl.download([url])

async def worker():
    while True:
        chat, uid, url = await queue.get()
        tmp = tempfile.mkdtemp()
        try:
            await bot.send_message(chat,"⏬ Скачиваю...")
            await asyncio.get_event_loop().run_in_executor(None,dl,url,tmp)
            await bot.send_video(chat,FSInputFile(f"{tmp}/video.mp4"))
        except Exception as e:
            await bot.send_message(chat,f"❌ {e}")
        shutil.rmtree(tmp)

# ========= START =========

@dp.message(CommandStart())
async def start(m:Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥 <b>VIDEO DOWNLOADER PRO</b>\n\n"
        "Просто отправь ссылку на TikTok или Instagram."
    )

# ========= PROFILE =========

@dp.message(Command("profile"))
async def profile(m:Message):
    u=await get_user(m.from_user.id)
    await m.answer(
        f"👤 Профиль\n\n"
        f"💎 {u[1]}\n"
        f"⭐ Баланс: {u[2]}\n"
        f"📥 Скачано сегодня: {u[3]}"
    )

# ========= PREMIUM SHOP =========

@dp.message(Command("premium"))
async def prem(m:Message):
    await m.answer(
        f"💎 Премиум:\n\n"
        f"🥇 Золотой — {GOLD_PRICE}⭐\n"
        f"💠 Алмазный — {DIAMOND_PRICE}⭐\n\n"
        "/buy_gold\n/buy_diamond"
    )

@dp.message(Command("buy_gold"))
async def gold(m:Message):
    await bot.send_invoice(
        m.chat.id,"Золотой","30 дней","gold",
        "", "XTR",[LabeledPrice("Gold",GOLD_PRICE)]
    )

@dp.message(Command("buy_diamond"))
async def dia(m:Message):
    await bot.send_invoice(
        m.chat.id,"Алмазный","90 дней","dia",
        "", "XTR",[LabeledPrice("Diamond",DIAMOND_PRICE)]
    )

@dp.pre_checkout_query()
async def pre(q:PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id,True)

# ========= LINK =========

@dp.message(F.text.startswith("http"))
async def link(m:Message):
    await queue.put((m.chat.id,m.from_user.id,m.text))
    await m.answer("📥 В очереди...")

# ========= ADMIN =========

@dp.message(Command("admin"))
async def admin(m:Message):
    if m.from_user.id!=ADMIN_ID:return
    await m.answer(
        "🛠 Админ:\n"
        "/stats\n"
        "/give_gold ID\n"
        "/give_diamond ID\n"
        "/add_stars ID amount"
    )

# ========= RUN =========

async def main():
    await init_db()
    asyncio.create_task(worker())
    await dp.start_polling(bot)

asyncio.run(main())