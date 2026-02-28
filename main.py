# main.py (исправленный — совместим с aiogram 3.x)
import asyncio
import aiosqlite
import yt_dlp
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
import itertools
from typing import Optional, List

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, InputMediaPhoto
)

# ====== CONFIG ======
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8687253696:AAGxeaingqzbCIGPqWsziXr4VYN0Bpopmm8"
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "")  # пустая строка для Stars
CURRENCY = "XTR"

ADMIN_ID = 6705555401  # твой админ ID

DB_PATH = "users.db"

WORKER_COUNT = 3  # число параллельных обработчиков очереди

# Premium settings
GOLD_STARS = 120
GOLD_DAYS = 30
DIAMOND_STARS = 250
DIAMOND_DAYS = 90

LIMITS = {
    "none": 4,
    "gold": 10,
    "diamond": None
}

# ====== BOT init ======
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ====== Queue with priority ======
priority_counter = itertools.count()

@dataclass(order=True)
class JobItem:
    priority: int
    count: int
    user_id: int = field(compare=False)
    chat_id: int = field(compare=False)
    url: str = field(compare=False)
    requested_at: datetime = field(compare=False)

queue: "asyncio.PriorityQueue" = asyncio.PriorityQueue()

# ====== Database helpers ======
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            premium_level TEXT DEFAULT 'none',
            premium_expires TEXT DEFAULT '',
            downloads_today INTEGER DEFAULT 0,
            last_reset TEXT DEFAULT ''
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_stars INTEGER DEFAULT 0,
            gold_count INTEGER DEFAULT 0,
            diamond_count INTEGER DEFAULT 0,
            total_purchases INTEGER DEFAULT 0
        );
        """)
        cur = await db.execute("SELECT COUNT(*) FROM stats")
        row = await cur.fetchone()
        if row[0] == 0:
            await db.execute("INSERT INTO stats (id) VALUES (1)")
        await db.commit()

async def ensure_user(user_id: int, username: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, last_reset) VALUES (?, ?, ?)",
            (user_id, username or "", datetime.utcnow().date().isoformat())
        )
        await db.commit()

async def set_premium(user_id: int, level: str, days: int):
    expires = (datetime.utcnow() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium_level=?, premium_expires=? WHERE user_id=?",
                         (level, expires, user_id))
        if level == "gold":
            await db.execute("UPDATE stats SET total_stars = total_stars + ?, gold_count = gold_count + 1, total_purchases = total_purchases + 1 WHERE id=1", (GOLD_STARS,))
        elif level == "diamond":
            await db.execute("UPDATE stats SET total_stars = total_stars + ?, diamond_count = diamond_count + 1, total_purchases = total_purchases + 1 WHERE id=1", (DIAMOND_STARS,))
        await db.commit()

async def revoke_premium(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium_level='none', premium_expires='' WHERE user_id=?", (user_id,))
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, premium_level, premium_expires, downloads_today, last_reset FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row

async def reset_daily_if_needed(user_id: int):
    row = await get_user(user_id)
    if not row:
        return
    last_reset = row[5] or ""
    today = datetime.utcnow().date().isoformat()
    if last_reset != today:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET downloads_today=0, last_reset=? WHERE user_id=?", (today, user_id))
            await db.commit()

async def increment_download(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET downloads_today = downloads_today + 1 WHERE user_id=?", (user_id,))
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT total_stars, gold_count, diamond_count, total_purchases FROM stats WHERE id=1") as cur:
            return await cur.fetchone()

# ====== Utils ======
def is_youtube_url(url: str) -> bool:
    if not url: return False
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u

def priority_for_level(level: str) -> int:
    if level == "diamond": return 0
    if level == "gold": return 1
    return 2

def choose_video_url(info: dict) -> Optional[str]:
    if info is None:
        return None
    if info.get("url") and info.get("ext") in ("mp4","mov","m4a","m4v","webm"):
        return info.get("url")
    formats = info.get("formats") or []
    mp4s = [f for f in formats if f.get("ext") in ("mp4","m4v","mov","webm")]
    if mp4s:
        def score(f):
            s = 0
            if f.get("height"): s += f.get("height")
            if f.get("tbr"): s += int(f.get("tbr") or 0)
            return s
        best = max(mp4s, key=score)
        return best.get("url")
    if formats:
        return formats[-1].get("url")
    return info.get("url")

def extract_photos_from_info(info: dict) -> List[str]:
    urls = []
    if not info:
        return urls
    if isinstance(info.get("entries"), list) and info["entries"]:
        for e in info["entries"]:
            if e.get("url") and e.get("ext") and e["ext"] in ("jpg","png","webp"):
                urls.append(e["url"])
            for t in e.get("thumbnails", [])[:5]:
                if t.get("url"):
                    urls.append(t["url"])
    for t in info.get("thumbnails", [])[:5]:
        if t.get("url"):
            urls.append(t["url"])
    if info.get("image"):
        urls.append(info.get("image"))
    seen = []
    out = []
    for u in urls:
        if u and u not in seen:
            seen.append(u); out.append(u)
    return out

# ====== yt-dlp extract info (async-friendly) ======
async def ytdl_extract(url: str):
    loop = asyncio.get_event_loop()
    def run():
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "force_generic_extractor": False,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await loop.run_in_executor(None, run)

# ====== Worker ======
async def worker_task(worker_id: int):
    session = aiohttp.ClientSession()
    try:
        while True:
            priority, count, job = await queue.get()
            user_id = job.user_id
            chat_id = job.chat_id
            url = job.url
            try:
                await reset_daily_if_needed(user_id)
                row = await get_user(user_id)
                premium_level = row[2] if row else "none"
                lim = LIMITS.get(premium_level)
                if lim is not None and row and row[4] >= lim:
                    await bot.send_message(chat_id, "❌ Дневной лимит скачиваний исчерпан.")
                    queue.task_done(); continue

                info = await ytdl_extract(url)
                if not info:
                    await bot.send_message(chat_id, "❌ Не удалось получить информацию о медиа.")
                    queue.task_done(); continue

                if isinstance(info.get("entries"), list) and info.get("entries"):
                    info = info["entries"][0]

                photos = extract_photos_from_info(info)
                if photos:
                    medias = []
                    for p in photos[:10]:
                        medias.append(InputMediaPhoto(media=p))
                    try:
                        for i in range(0, len(medias), 10):
                            await bot.send_media_group(chat_id, medias[i:i+10])
                    except Exception:
                        for p in photos[:10]:
                            try:
                                await bot.send_photo(chat_id, p)
                            except Exception:
                                pass
                    music = None
                    music_meta = info.get("music") or info.get("audio") or info.get("track")
                    if isinstance(music_meta, dict):
                        music = music_meta.get("url") or music_meta.get("play_url")
                    if music:
                        try:
                            await bot.send_message(chat_id, "🎵 Музыка из поста:")
                            await bot.send_audio(chat_id, music)
                        except Exception:
                            pass
                    await increment_download(user_id)
                    queue.task_done()
                    continue

                video_url = choose_video_url(info)
                if video_url:
                    try:
                        await bot.send_chat_action(chat_id, "upload_video")
                        await bot.send_video(chat_id, video_url, supports_streaming=True, caption=info.get("title") or "")
                        await increment_download(user_id)
                    except Exception:
                        await bot.send_message(chat_id, f"✅ Вот ссылка на медиа:\n{video_url}")
                        await increment_download(user_id)
                else:
                    await bot.send_message(chat_id, "❌ Не найдено видео/фото в ссылке.")
            except Exception as e:
                await bot.send_message(chat_id, f"❌ Ошибка при обработке ссылки: {e}")
            finally:
                queue.task_done()
    finally:
        await session.close()

# ====== Command handlers ======
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username)
    await msg.reply("🤖 Ultra-PRO Bot запущен!\n\nНаберите /menu чтобы увидеть команды.")

@dp.message(Command("menu"))
async def cmd_menu(msg: Message):
    await msg.reply(
        "📋 Меню:\n"
        "/info — информация о боте\n"
        "/premium — тарифы\n"
        "/buy — купить премиум\n"
        "/status — проверить статус премиума\n"
        "/gift <user_id> <gold|diamond> [days] — (админ) подарить премиум\n"
        "/revoke <user_id> — (админ) снять премиум\n"
        "/stats — (админ) статистика\n\n"
        "Просто отправь ссылку на TikTok или Instagram, бот добавит её в очередь и пришлёт готовое медиа."
    )

@dp.message(Command("info"))
async def cmd_info(msg: Message):
    await msg.reply(
        "Я отправляю готовое видео или фото-пост (фото + музыка) по ссылке.\n"
        "Не сохраняю файлы на сервере — работаю через прямые URL.\n"
        "YouTube Shorts не поддерживаются."
    )

@dp.message(Command("premium"))
async def cmd_premium(msg: Message):
    await msg.reply(
        "💎 Тарифы:\n"
        f"• GOLD — {GOLD_STARS} ⭐ (30 дней) — быстрый доступ\n"
        f"• DIAMOND — {DIAMOND_STARS} ⭐ (90 дней) — приоритетная обработка\n"
        "Нажми /buy чтобы оплатить."
    )

@dp.message(Command("buy"))
async def cmd_buy(msg: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Купить GOLD — {GOLD_STARS}⭐ (30d)", callback_data="buy_gold")],
        [InlineKeyboardButton(text=f"Купить DIAMOND — {DIAMOND_STARS}⭐ (90d)", callback_data="buy_diamond")]
    ])
    await msg.reply("Выберите тариф для покупки:", reply_markup=kb)

@dp.callback_query(F.data == "buy_gold")
async def on_buy_gold(call: CallbackQuery):
    prices = [LabeledPrice(label=f"GOLD ({GOLD_DAYS} дней)", amount=GOLD_STARS)]
    await bot.send_invoice(call.from_user.id, title="GOLD Premium", description=f"{GOLD_DAYS} дней", payload=f"gold:{call.from_user.id}", provider_token=PROVIDER_TOKEN or "", currency=CURRENCY, prices=prices)
    await call.answer()

@dp.callback_query(F.data == "buy_diamond")
async def on_buy_diamond(call: CallbackQuery):
    prices = [LabeledPrice(label=f"DIAMOND ({DIAMOND_DAYS} дней)", amount=DIAMOND_STARS)]
    await bot.send_invoice(call.from_user.id, title="DIAMOND Premium", description=f"{DIAMOND_DAYS} дней", payload=f"diamond:{call.from_user.id}", provider_token=PROVIDER_TOKEN or "", currency=CURRENCY, prices=prices)
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def on_successful_payment(msg: Message):
    sp = msg.successful_payment
    payload = sp.invoice_payload or ""
    try:
        level, uid_s = payload.split(":")
        uid = int(uid_s)
    except Exception:
        uid = msg.from_user.id
        level = payload or "gold"
    if level == "gold":
        await set_premium(uid, "gold", GOLD_DAYS)
        await msg.reply("✅ Оплата принята — GOLD активирован!")
    elif level == "diamond":
        await set_premium(uid, "diamond", DIAMOND_DAYS)
        await msg.reply("🔥 Оплата принята — DIAMOND активирован!")
    else:
        await msg.reply("✅ Оплата принята!")
    try:
        await bot.send_message(ADMIN_ID, f"Пользователь @{msg.from_user.username or msg.from_user.id} купил {level.upper()}")
    except Exception:
        pass

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    row = await get_user(msg.from_user.id)
    if not row:
        await msg.reply("Пользователь не найден.")
        return
    level = row[2]
    expiry = row[3] or "—"
    await msg.reply(f"Текущий премиум: {level}\nИстекает: {expiry}")

@dp.message(Command("gift"))
async def cmd_gift(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.reply("Использование: /gift <user_id> <gold|diamond> [days]")
        return
    try:
        target = int(parts[1])
        level = parts[2]
        days = int(parts[3]) if len(parts) >= 4 else (GOLD_DAYS if level=="gold" else DIAMOND_DAYS)
        await set_premium(target, level, days)
        await msg.reply(f"✅ Выдан {level} пользователю {target} на {days} дней")
        try:
            await bot.send_message(target, f"🎁 Тебе подарили премиум: {level} на {days} дней (админ)")
        except:
            pass
    except Exception as e:
        await msg.reply(f"Ошибка: {e}")

@dp.message(Command("revoke"))
async def cmd_revoke(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.reply("Использование: /revoke <user_id>")
        return
    try:
        target = int(parts[1])
        await revoke_premium(target)
        await msg.reply(f"✅ Премиум снят у {target}")
    except Exception as e:
        await msg.reply(f"Ошибка: {e}")

@dp.message(Command("check"))
async def cmd_check(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.reply("Использование: /check <user_id>")
        return
    try:
        target = int(parts[1])
        row = await get_user(target)
        if not row:
            await msg.reply("Пользователь не найден.")
            return
        await msg.reply(f"Пользователь: {target}\nПремиум: {row[2]}\nИстекает: {row[3]}")
    except Exception as e:
        await msg.reply(f"Ошибка: {e}")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    s = await get_stats()
    await msg.reply(f"⭐ Всего звёзд: {s[0]}\nGOLD покупок: {s[1]}\nDIAMOND покупок: {s[2]}\nВсего покупок: {s[3]}")

# ====== Incoming link handler (enqueue) ======
@dp.message()
async def handle_incoming(msg: Message):
    text = (msg.text or "").strip()
    if not text.startswith("http"):
        return
    if is_youtube_url(text):
        await msg.reply("❌ YouTube Shorts не поддерживаются.")
        return

    await ensure_user(msg.from_user.id, msg.from_user.username)
    await reset_daily_if_needed(msg.from_user.id)
    user = await get_user(msg.from_user.id)
    level = user[2] if user else "none"
    lim = LIMITS.get(level)
    if lim is not None and user and user[4] >= lim:
        await msg.reply("❌ Дневной лимит скачиваний исчерпан.")
        return

    p = priority_for_level(level)
    cnt = next(priority_counter)
    job = JobItem(priority=p, count=cnt, user_id=msg.from_user.id, chat_id=msg.chat.id, url=text, requested_at=datetime.utcnow())
    await queue.put((job.priority, job.count, job))
    await msg.reply(f"⏳ Ссылка добавлена в очередь (приоритет: {level}).")
    if level == "diamond":
        await msg.reply("⚡ Ваш запрос будет обработан с приоритетом.")

# ====== Start workers and bot ======
async def main():
    await init_db()
    await bot.set_my_commands([
        BotCommand(command="menu", description="Меню"),
        BotCommand(command="info", description="О боте"),
        BotCommand(command="premium", description="Тарифы"),
        BotCommand(command="buy", description="Купить премиум"),
        BotCommand(command="status", description="Статус премиума"),
    ])
    for i in range(WORKER_COUNT):
        asyncio.create_task(worker_task(i+1))
    print("Ultra-PRO bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        try:
            asyncio.run(bot.session.close())
        except Exception:
            pass