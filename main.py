import asyncio
import os
import tempfile
import shutil
import logging
from datetime import datetime, timedelta

import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile

# ================= CONFIG =================

TOKEN = "8736949755:AAG8So7fVUlyNpJxmGQptWQNk5bx7kjPoLs"
ADMIN_IDS = [6705555401]

DB = "users.db"

LIMITS = {
    "обычный": 4,
    "золотой": 10,
    "алмазный": None
}

GOLD_DAYS = 30
DIAMOND_DAYS = 90

logging.basicConfig(level=logging.INFO)

bot = Bot(TOKEN)
dp = Dispatcher()

queue = asyncio.Queue()

# ================= DATABASE =================

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            premium TEXT DEFAULT 'обычный',
            downloads INTEGER DEFAULT 0,
            reset TEXT,
            expires TEXT
        )
        """)
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
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
            return await cur.fetchone()

async def inc_download(uid):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (uid,))
        await db.commit()

async def reset_limits(uid):
    row = await get_user(uid)
    last = datetime.fromisoformat(row[3])
    if datetime.utcnow() - last > timedelta(days=1):
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE users SET downloads=0, reset=? WHERE id=?",
                             (datetime.utcnow().isoformat(), uid))
            await db.commit()

# ================= DOWNLOAD =================

def download_video(url, folder):
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": f"{folder}/video.%(ext)s",
        "quiet": True,
        "merge_output_format": "mp4"
    }

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

async def worker():
    while True:
        chat_id, user_id, url = await queue.get()

        temp = tempfile.mkdtemp()

        try:
            await bot.send_message(chat_id, "⏬ Скачиваю видео...")

            await asyncio.get_event_loop().run_in_executor(
                None, download_video, url, temp
            )

            file = os.path.join(temp, "video.mp4")

            await bot.send_video(chat_id, FSInputFile(file))

            await inc_download(user_id)

            await bot.send_message(chat_id, "✅ Готово!")

        except Exception as e:
            await bot.send_message(chat_id, f"❌ Ошибка:\n{e}")

        finally:
            shutil.rmtree(temp, ignore_errors=True)

# ================= HANDLERS =================

@dp.message(CommandStart())
async def start(m: Message):
    await add_user(m.from_user.id)

    await m.answer(
        "🔥 <b>ULTRA VIDEO BOT</b>\n\n"
        "📥 Пришли ссылку на:\n"
        "• TikTok\n"
        "• Instagram\n\n"
        "Я отправлю тебе видео без водяных знаков."
    )

@dp.message(Command("profile"))
async def profile(m: Message):
    row = await get_user(m.from_user.id)

    await m.answer(
        f"👤 Профиль\n\n"
        f"💎 Премиум: {row[1]}\n"
        f"📥 Сегодня скачано: {row[2]}"
    )

# ================= LINK HANDLER =================

@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    uid = m.from_user.id
    await add_user(uid)
    await reset_limits(uid)

    row = await get_user(uid)

    limit = LIMITS[row[1]]

    if limit and row[2] >= limit:
        await m.answer("❌ Достигнут дневной лимит.")
        return

    await queue.put((m.chat.id, uid, m.text))

    await m.answer("📥 Добавлено в очередь...")

# ================= ADMIN PANEL =================

@dp.message(Command("admin"))
async def admin_panel(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return

    await m.answer(
        "🛠 Админ панель\n\n"
        "/give_gold ID\n"
        "/give_diamond ID\n"
        "/stats"
    )

@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return

    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            count = (await cur.fetchone())[0]

    await m.answer(f"👥 Пользователей: {count}")

# ================= RUN =================

async def main():
    await init_db()
    asyncio.create_task(worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())