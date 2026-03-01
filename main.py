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
TOKEN = "8687253696:AAGxeaingqzbCIGPqWsziXr4VYN0Bpopmm8" or os.getenv("TOKEN")
ADMIN_ID = 6705555401  # <- твой Telegram ID
DB_PATH = "bot_db.sqlite"

# Premium settings
GOLD_PRICE = 120
GOLD_DAYS = 30
DIAMOND_PRICE = 250
DIAMOND_DAYS = 90
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue = asyncio.Queue()


# ========= DATABASE =========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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


async def add_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, reset) VALUES(?, ?)",
                         (uid, datetime.utcnow().isoformat()))
        await db.commit()


async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c:
            return await c.fetchone()


async def set_premium(uid: int, level: str, days: int):
    exp = (datetime.utcnow() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium=?, expires=? WHERE id=?", (level, exp, uid))
        await db.commit()


async def add_stars(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET stars=stars+? WHERE id=?", (amount, uid))
        await db.commit()


async def increment_download(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET downloads = downloads + 1 WHERE id=?", (uid,))
        await db.commit()


async def can_download(uid: int) -> bool:
    user = await get_user(uid)
    if not user:
        return True
    premium, downloads = user[1], user[3]
    limit = LIMITS.get(premium, 4)
    if limit is None:
        return True
    return downloads < limit


# ========= DOWNLOAD FUNCTION =========
@dp.message(Command("convert"))
async def start_handler(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔗Отправьте ссылку на видео и я обрабтую его и пришлю вам!"
    )
def download_video(url: str, folder: str):
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": f"{folder}/video.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю видео...")
            await asyncio.get_event_loop().run_in_executor(None, download_video, url, tmp)
            await bot.send_video(chat_id, FSInputFile(f"{tmp}/video.mp4"))
            await increment_download(user_id)
            await bot.send_message(chat_id, "✅ Готово!")
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Ошибка: {e}")
        finally:
            shutil.rmtree(tmp)


# ========= COMMANDS =========
@dp.message(CommandStart())
async def start_handler(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥TikGram_installer_bot\n\n"
        "Отправь ссылку на TikTok,Instagram,YouTube и бот скачает видео."
    )

@dp.message(Command("menu"))
async def start_handler(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥TikGram_installer_bot\n\n"
        "Отправь ссылку на TikTok,Instagram,YouTube и бот скачает видео."
    )



@dp.message(Command("profile"))
async def profile_handler(m: Message):
    user = await get_user(m.from_user.id)
    await m.answer(
        f"👤 Профиль\n"
        f"💎 {user[1]}\n"
    )


@dp.message(Command("premium"))
async def premium_handler(m: Message):
    await m.answer(
        f"💎 Премиум:\n"
        f"Обычный(по умолчанию)\n" f"4 видео в день обычное\n\n"
        f"🥇 Золотой — {GOLD_PRICE}⭐ ({GOLD_DAYS} дней)\n" f"10 видео в день - хорошее разрешение\n\n"
        f"💠 Алмазный — {DIAMOND_PRICE}⭐ ({DIAMOND_DAYS} дней)\n" f"неограниченные видео в день - высокое разрешение - приоритет\n\n"
        "Команды:\n/buy_gold\n/buy_diamond"
    )

@dp.message(Command("about"))
async def about_handler(m: Message):
    user = await get_user(m.from_user.id)
    await m.answer(
        f"🤖 бот может конвертировать ссылки в видео и отправлять вам "
    )

@dp.message(Command("buy_gold"))
async def buy_gold(m: Message):
    prices = [LabeledPrice(label=f"Золотой ({GOLD_DAYS} дней)", amount=GOLD_PRICE)]
    await bot.send_invoice(m.chat.id, title="Золотой премиум", description="Покупка премиума", payload=f"gold:{m.from_user.id}", provider_token="", currency="XTR", prices=prices, start_parameter="premium")


@dp.message(Command("buy_diamond"))
async def buy_diamond(m: Message):
    prices = [LabeledPrice(label=f"Алмазный ({DIAMOND_DAYS} дней)", amount=DIAMOND_PRICE)]
    await bot.send_invoice(m.chat.id, title="Алмазный премиум", description="Покупка премиума", payload=f"diamond:{m.from_user.id}", provider_token="", currency="XTR", prices=prices, start_parameter="premium")


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return
    await download_queue.put((m.chat.id, user_id, m.text))
    await m.answer("📥 Добавлено в очередь...")

async def get_remaining_downloads(user_id: int):
    await reset_if_needed(user_id)
    row = await get_user_row(user_id)

    if not row:
        return 0, 4

    premium = row[2] or "обычный"
    downloads_today = row[3] or 0

    limit = LIMITS.get(premium, 4)

    # безлимит
    if limit is None:
        return None, None

    remaining = max(limit - downloads_today, 0)
    return remaining, limit

@dp.message(Command("limit"))
async def show_limit(msg: Message):
    remaining, limit = await get_remaining_downloads(msg.from_user.id)

    if remaining is None:
        await msg.answer("♾ У вас безлимитные скачивания сегодня.")
        return

    await msg.answer(
        f"📊 Лимиты на сегодня:\n"
        f"Всего: {limit}\n"
        f"Осталось: {remaining}"
    )
    await bot.send_message(chat_id, f"📥 Осталось скачиваний сегодня: {remaining}")

# ========= ADMIN COMMANDS =========
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "🛠 Админ панель:\n"
        "/stats — Статистика\n"
        "/give_gold ID — Выдать Золотой\n"
        "/give_diamond ID — Выдать Алмазный\n"
        "/add_stars ID сумма — Начислить звёзды"
    )


@dp.message(F.text.startswith("/give_gold"))
async def give_gold(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(m.text.split()[1])
        await set_premium(uid, "золотой", GOLD_DAYS)
        await m.answer(f"✅ Золотой выдан пользователю {uid}")
    except:
        await m.answer("❌ Неверный формат. /give_gold ID")


@dp.message(F.text.startswith("/give_diamond"))
async def give_diamond(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(m.text.split()[1])
        await set_premium(uid, "алмазный", DIAMOND_DAYS)
        await m.answer(f"✅ Алмазный выдан пользователю {uid}")
    except:
        await m.answer("❌ Неверный формат. /give_diamond ID")


@dp.message(F.text.startswith("/add_stars"))
async def add_stars_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        parts = m.text.split()
        uid = int(parts[1])
        amount = int(parts[2])
        await add_stars(uid, amount)
        await m.answer(f"✅ Начислено {amount}⭐ пользователю {uid}")
    except:
        await m.answer("❌ Неверный формат. /add_stars ID сумма")


# ========= RUN =========
async def main():
    await init_db()
    asyncio.create_task(download_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())