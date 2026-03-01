# main.py — полный рабочий файл
import asyncio
import os
import tempfile
import shutil
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple

import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)

# --------------- Настройки и логирование ---------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "8687253696:AAGxeaingqzbCIGPqWsziXr4VYN0Bpopmm8"   # <- Поставь реальный токен
ADMIN_ID = 6705555401
DB_PATH = "bot_db.sqlite"

# Premium settings
GOLD_PRICE = 120
GOLD_DAYS = 30
DIAMOND_PRICE = 250
DIAMOND_DAYS = 90
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

# TTL для кеша аудио (в секундах)
AUDIO_TTL_SECONDS = 10 * 60  # 10 минут

# Инициализация бота и очереди
bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()

# Кеш для аудио: token -> {"audio": path, "tmpdir": tmpdir}
audio_cache: Dict[str, Dict[str, Optional[str]]] = {}

# --------------- База данных ---------------
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
    logger.info("DB initialized")

async def add_user(uid: int):
    # добавит пользователя, если нет
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, reset) VALUES(?, ?)", (uid, now_iso))
        await db.commit()

async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, premium, stars, downloads, reset, expires FROM users WHERE id=?", (uid,)) as cur:
            return await cur.fetchone()

async def set_premium(uid: int, level: str, days: int):
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
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

# --------------- Логика сброса лимитов ---------------
async def reset_if_needed(user_id: int):
    if not isinstance(user_id, int):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT reset FROM users WHERE id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        now = datetime.now(timezone.utc)
        if not row or row[0] is None:
            await db.execute("UPDATE users SET reset=? WHERE id=?", (now.isoformat(), user_id))
            await db.commit()
            return
        try:
            last_reset = datetime.fromisoformat(row[0])
        except Exception:
            await db.execute("UPDATE users SET reset=? WHERE id=?", (now.isoformat(), user_id))
            await db.commit()
            return
        if now.date() > last_reset.date():
            await db.execute("UPDATE users SET downloads=0, reset=? WHERE id=?", (now.isoformat(), user_id))
            await db.commit()

async def can_download(uid: int) -> bool:
    await reset_if_needed(uid)
    user = await get_user(uid)
    if not user:
        return True
    premium, downloads = user[1], user[3]
    limit = LIMITS.get(premium, 4)
    if limit is None:
        return True
    return downloads < limit

async def get_remaining_downloads(user_id: int) -> Tuple[Optional[int], Optional[int], str]:
    # возвращает (remaining, limit, premium)
    if not isinstance(user_id, int):
        raise TypeError("user_id must be int")
    await reset_if_needed(user_id)
    user = await get_user(user_id)
    if not user:
        return 4, 4, "обычный"
    premium = user[1] or "обычный"
    downloads_today = user[3] or 0
    limit = LIMITS.get(premium, 4)
    if limit is None:
        return None, None, premium
    remaining = max(limit - downloads_today, 0)
    return remaining, limit, premium

# --------------- yt-dlp скачивание ---------------
def download_video(url: str, folder: str):
    # сохраняет в folder/video.<ext>
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(folder, "video.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# --------------- ffmpeg извлечение аудио ---------------
async def extract_audio_ffmpeg(video_path: str, output_audio_path: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "mp3", "-ar", "44100", "-ac", "2",
            output_audio_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return os.path.exists(output_audio_path)
    except Exception as e:
        logger.exception("extract_audio_ffmpeg error: %s", e)
        return False

async def cleanup_audio_after_delay(token: str, delay: int = AUDIO_TTL_SECONDS):
    await asyncio.sleep(delay)
    info = audio_cache.get(token)
    if not info:
        return
    try:
        audio = info.get("audio")
        tmpdir = info.get("tmpdir")
        if audio and os.path.exists(audio):
            os.remove(audio)
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        logger.exception("cleanup_audio_after_delay failed for token %s", token)
    audio_cache.pop(token, None)

# --------------- Очередной воркер (основная вставка) ---------------
async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token: Optional[str] = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю видео...")
            # скачиваем в tmp
            await asyncio.get_event_loop().run_in_executor(None, download_video, url, tmp)

            # ищем видео-файл в tmp
            video_path: Optional[str] = None
            for f in os.listdir(tmp):
                if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts")):
                    video_path = os.path.join(tmp, f)
                    break

            if not video_path or not os.path.exists(video_path):
                await bot.send_message(chat_id, "❌ Не удалось найти скачанный файл.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # аудио путь
            audio_path = os.path.join(tmp, "audio.mp3")

            # извлекаем аудио (ассинхронно)
            audio_ok = False
            try:
                audio_ok = await extract_audio_ffmpeg(video_path, audio_path)
            except Exception:
                logger.exception("Ошибка при extract_audio_ffmpeg")

            # токен для callback
            token = uuid.uuid4().hex

            # клавиатура
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
            ])

            caption_text = "✅ Готово!\nХотите конвертировать только песню?"

            # отправка видео
            sent_ok = False
            try:
                await bot.send_video(chat_id, FSInputFile(video_path), caption=caption_text, reply_markup=kb)
                sent_ok = True
            except Exception:
                logger.exception("send_video failed; try send_document")
                try:
                    await bot.send_document(chat_id, FSInputFile(video_path), caption=caption_text, reply_markup=kb)
                    sent_ok = True
                except Exception as e_send:
                    logger.exception("send_document failed: %s", e_send)
                    await bot.send_message(chat_id, f"❌ Ошибка отправки видео: {e_send}")
                    sent_ok = False

            if not sent_ok:
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # увеличиваем счётчик
            try:
                await increment_download(user_id)
            except Exception:
                logger.exception("increment_download failed for %s", user_id)

            # кладём в кеш (даже если audio_ok False — кладём запись с None)
            audio_cache[token] = {"audio": audio_path if audio_ok and os.path.exists(audio_path) else None,
                                  "tmpdir": tmp}

            # запланируем очистку
            asyncio.create_task(cleanup_audio_after_delay(token, AUDIO_TTL_SECONDS))

        except Exception as exc:
            logger.exception("download_worker exception: %s", exc)
            try:
                await bot.send_message(chat_id, f"❌ Ошибка: {exc}")
            except Exception:
                pass
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
        # НЕ удаляем tmp тут если в audio_cache есть запись — она будет удалена позже

# --------------- Callback (обработчик кнопки) ---------------
@dp.callback_query(lambda c: c.data and c.data.startswith("get_audio:"))
async def cb_get_audio(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    info = audio_cache.get(token)

    if not info:
        await cq.answer("Аудио устарело или недоступно.", show_alert=True)
        return

    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")

    if not audio_path or not os.path.exists(audio_path):
        await cq.answer("Аудио не было извлечено или уже удалено.", show_alert=True)
        # удаляем tmpdir если есть
        try:
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            logger.exception("Failed to remove tmpdir after missing audio")
        audio_cache.pop(token, None)
        return

    try:
        await cq.answer()  # убирает "крутилку"
        await bot.send_chat_action(cq.from_user.id, "upload_audio")
        await bot.send_audio(cq.from_user.id, FSInputFile(audio_path))
    except Exception:
        await cq.answer("Ошибка при отправке аудио.", show_alert=True)
        return

    # после успешной отправки — удалим tmp и запись
    try:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        logger.exception("Error cleaning tmp after sending audio")
    audio_cache.pop(token, None)

# --------------- Команды бота ---------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥TikGram_installer_bot\n\n"
        "Отправь ссылку на TikTok,Instagram,YouTube и бот скачает видео."
    )

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥TikGram_installer_bot\n\n"
        "Отправь ссылку на TikTok,Instagram,YouTube и бот скачает видео."
    )

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Профиль: не найден")
        return
    await m.answer(
        f"👤 Профиль\n"
        f"💎 {user[1]}\n"
        f"⭐ Звёзды: {user[2]}\n"
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
    await m.answer("🤖 Бот конвертирует ссылки в видео и может вырезать аудио из видео.")

@dp.message(Command("buy_gold"))
async def buy_gold(m: Message):
    prices = [LabeledPrice(label=f"Золотой ({GOLD_DAYS} дней)", amount=GOLD_PRICE)]
    await bot.send_invoice(m.chat.id, title="Золотой премиум", description="Покупка премиума",
                           payload=f"gold:{m.from_user.id}", provider_token="", currency="XTR", prices=prices,
                           start_parameter="premium")

@dp.message(Command("buy_diamond"))
async def buy_diamond(m: Message):
    prices = [LabeledPrice(label=f"Алмазный ({DIAMOND_DAYS} дней)", amount=DIAMOND_PRICE)]
    await bot.send_invoice(m.chat.id, title="Алмазный премиум", description="Покупка премиума",
                           payload=f"diamond:{m.from_user.id}", provider_token="", currency="XTR", prices=prices,
                           start_parameter="premium")

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

# Команда конверта — просит прислать ссылку (твоя ранее)
@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔗 Отправьте ссылку на видео и я обработаю его и пришлю вам!")

# Обработка ссылок (простая очередь)
@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return
    await download_queue.put((m.chat.id, user_id, m.text))
    await m.answer("📥 Добавлено в очередь...")

# --------------- Админ команды ---------------
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
    except Exception:
        await m.answer("❌ Неверный формат. /give_gold ID")

@dp.message(F.text.startswith("/give_diamond"))
async def give_diamond(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(m.text.split()[1])
        await set_premium(uid, "алмазный", DIAMOND_DAYS)
        await m.answer(f"✅ Алмазный выдан пользователю {uid}")
    except Exception:
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
    except Exception:
        await m.answer("❌ Неверный формат. /add_stars ID сумма")

# --------------- Запуск бота ---------------
async def main():
    await init_db()

    # Убираем возможный webhook и падение по конфликту
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Failed to delete webhook (ok to ignore)")

    # стартуем воркер и polling
    asyncio.create_task(download_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())