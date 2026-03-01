# main.py — готовый рабочий файл с улучшенной поддержкой Pinterest и TikTok photo
import asyncio
import os
import tempfile
import shutil
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)

# ---------- Настройки / лог ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "ТОКЕН_БОТА"   # <- вставь реальный токен
ADMIN_ID = 6705555401
DB_PATH = "bot_db.sqlite"

# премиум настройки
GOLD_PRICE = 120
GOLD_DAYS = 30
DIAMOND_PRICE = 250
DIAMOND_DAYS = 90
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

# TTL для временных файлов — 30 минут
AUDIO_TTL_SECONDS = 30 * 60  # 30 минут

# бот и очередь
bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()

# кеш: token -> {"audio": path|null, "tmpdir": tmpdir, "video": filename, "url": original_url, "owner": user_id}
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}

# ---------- БД ----------
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

# ---------- Сброс лимитов и проверки ----------
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

# ---------- yt-dlp скачивание (универсальный) ----------
def download_video(url: str, folder: str):
    """
    Универсальные опции yt-dlp: не требовать mp4, разрешать неплейабельные форматы,
    сохранять файлы под id (безопасно), поддерживать загрузку изображений и HLS.
    """
    ydl_opts = {
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "format": "best",  # важное: не требовать ext=mp4
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "noplaylist": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "allow_unplayable_formats": True,
        # некоторые extractors могут отдавать несколько файлов — пусть yt-dlp сам их положит в папку
    }

    # Специально для TikTok photo ссылки: yt-dlp в новых версиях поддерживает их,
    # но полезно разрешить незамедлительную загрузку всех форматов.
    if "tiktok.com" in url and "/photo/" in url:
        ydl_opts["format"] = "best"
        ydl_opts["noplaylist"] = True
        ydl_opts["allow_unplayable_formats"] = True

    # Pinterest иногда даёт нестандартные форматы (HLS/JSON/images) — позволим yt-dlp взять лучший доступный.
    if "pinterest." in url:
        ydl_opts["format"] = "best"
        ydl_opts["allow_unplayable_formats"] = True

    # Запускаем загрузку
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# ---------- ffmpeg извлечение аудио ----------
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

# ---------- Download worker (скачивает, отправляет видео/изображения + кнопку) ----------
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")

async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token: Optional[str] = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            # скачиваем (в блоке executor)
            await asyncio.get_event_loop().run_in_executor(None, download_video, url, tmp)

            # Найдём видео/изображения в папке
            video_path: Optional[str] = None
            image_paths: List[str] = []
            other_files: List[str] = []

            for f in os.listdir(tmp):
                f_lower = f.lower()
                full = os.path.join(tmp, f)
                if f_lower.endswith(VIDEO_EXTS):
                    video_path = full
                    # не break — на всякий случай оставляем первыми найденными видео
                    break

            # если видео не найдено — соберём изображения
            if not video_path:
                for f in os.listdir(tmp):
                    f_lower = f.lower()
                    full = os.path.join(tmp, f)
                    if f_lower.endswith(IMAGE_EXTS):
                        image_paths.append(full)
                    else:
                        other_files.append(full)

            if not video_path and not image_paths:
                # иногда yt-dlp сохраняет JSON/metadata и файл формата, который не имеет привычного расширения.
                # попробуем найти любой файл с размером > 0 и с расширением (как fallback).
                for f in os.listdir(tmp):
                    full = os.path.join(tmp, f)
                    if os.path.isfile(full) and os.path.getsize(full) > 0:
                        # если файл не маленький и не текстовый metadata — считаем как потенциальное медиа
                        # допустим, это может быть webm без расширения, но редко.
                        other_files.append(full)

            # Если нашлись изображения — отправим их как альбом
            if image_paths and not video_path:
                try:
                    # Telegram ограничивает media_group до 10 элементов
                    images_to_send = sorted(image_paths)[:10]
                    media = []
                    for img in images_to_send:
                        media.append(types.InputMediaPhoto(media=types.InputFile(img)))
                    await bot.send_media_group(chat_id, media=media)
                    await bot.send_message(chat_id, "✅ Готово! (изображения)")
                    # увеличиваем счётчик скачиваний
                    try:
                        await increment_download(user_id)
                    except Exception:
                        logger.exception("increment_download failed for %s", user_id)
                except Exception as e:
                    logger.exception("send images failed: %s", e)
                    await bot.send_message(chat_id, f"❌ Ошибка отправки изображений: {e}")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                continue

            # Если найдено видео — отправляем его и кладём в кеш для получения аудио
            if video_path:
                token = uuid.uuid4().hex
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
                ])
                caption_text = "✅ Готово!\nХотите конвертировать только песню?"

                sent_ok = False
                try:
                    # Отправляем как video (если большой — aiogram сам разобрается)
                    await bot.send_video(chat_id, FSInputFile(video_path), caption=caption_text, reply_markup=kb)
                    sent_ok = True
                except Exception:
                    logger.exception("send_video failed; trying send_document")
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

                # увеличиваем счётчик скачиваний
                try:
                    await increment_download(user_id)
                except Exception:
                    logger.exception("increment_download failed for %s", user_id)

                # кладём в кеш метаданные (audio=None пока)
                audio_cache[token] = {
                    "audio": None,
                    "tmpdir": tmp,
                    "video": video_path,
                    "url": url,
                    "owner": user_id
                }

                # запланируем удаление tmp через TTL
                asyncio.create_task(cleanup_audio_after_delay(token, AUDIO_TTL_SECONDS))
                continue

            # Если попали сюда — не нашли медиа
            await bot.send_message(chat_id, "❌ Не удалось скачать видео/изображения с этой ссылки.")
            shutil.rmtree(tmp, ignore_errors=True)

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
        # не удаляем tmp здесь, т.к. он нужен для callback-аудио (в случае видео)

# ---------- Callback: обработка нажатия "Получить песню" ----------
@dp.callback_query(lambda c: c.data and c.data.startswith("get_audio:"))
async def cb_get_audio(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    info = audio_cache.get(token)

    # если нет записи — сообщаем
    if not info:
        await cq.answer("⚠️ Аудио устарело или недоступно — пришлите ссылку ещё раз.", show_alert=True)
        return

    # опционально: только владелец может получить аудио (если нужно)
    owner = info.get("owner")
    if owner and cq.from_user.id != owner and cq.from_user.id != ADMIN_ID:
        await cq.answer("Только тот, кто запросил видео, может получить аудио.", show_alert=True)
        return

    # подготовка путей/данных
    audio_path = info.get("audio")      # может быть None
    tmpdir = info.get("tmpdir")        # папка с файлами
    video_path = info.get("video")     # путь к видео (если сохранили)
    url = info.get("url")              # исходный url (на случай повторного скачивания)

    # закроем крутилку на кнопке
    await cq.answer()

    # 1) если mp3 уже есть в кеше — отправляем сразу как аудио (не voice)
    if audio_path and os.path.exists(audio_path):
        try:
            await bot.send_chat_action(cq.from_user.id, "upload_audio")
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path), title="Аудио из видео")
        except Exception:
            await cq.answer("Ошибка при отправке аудио.", show_alert=True)
        finally:
            # чистим файлы и запись
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
        return

    # 2) если mp3 нет, но есть видео-файл — извлекаем на месте и отправляем
    if video_path and os.path.exists(video_path):
        audio_path_new = os.path.join(tmpdir, "audio.mp3")
        await bot.send_chat_action(cq.from_user.id, "record_audio")
        success = await extract_audio_ffmpeg(video_path, audio_path_new)
        if not success:
            await cq.answer("Не удалось извлечь аудио из видео.", show_alert=True)
            # удаляем кеш/временную папку, чтобы не оставлять мусор
            try:
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
            return
        # отправляем как аудио (песня)
        try:
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path_new), title="Аудио из видео")
        except Exception:
            await cq.answer("Ошибка при отправке аудио.", show_alert=True)
        finally:
            try:
                if os.path.exists(audio_path_new):
                    os.remove(audio_path_new)
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
        return

    # 3) Если ни mp3 ни видео нет — пробуем повторно скачать (fallback), иначе — сообщаем
    if url:
        new_tmp = tempfile.mkdtemp()
        try:
            await cq.answer("Аудио отсутствует — пробую повторно скачать видео...", show_alert=True)
            await asyncio.get_event_loop().run_in_executor(None, download_video, url, new_tmp)
            new_video = None
            for f in os.listdir(new_tmp):
                if f.lower().endswith(VIDEO_EXTS):
                    new_video = os.path.join(new_tmp, f)
                    break
            if not new_video:
                await cq.answer("Не удалось повторно скачать видео.", show_alert=True)
                shutil.rmtree(new_tmp, ignore_errors=True)
                audio_cache.pop(token, None)
                return
            audio_path_new = os.path.join(new_tmp, "audio.mp3")
            success = await extract_audio_ffmpeg(new_video, audio_path_new)
            if not success:
                await cq.answer("Не удалось извлечь аудио после повторного скачивания.", show_alert=True)
                shutil.rmtree(new_tmp, ignore_errors=True)
                audio_cache.pop(token, None)
                return
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path_new), title="Аудио из видео")
        except Exception:
            await cq.answer("Ошибка при повторном скачивании/конвертации.", show_alert=True)
        finally:
            try:
                shutil.rmtree(new_tmp, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
        return

    # В остальных случаях
    await cq.answer("Аудио устарело или недоступно — пришлите ссылку ещё раз.", show_alert=True)
    audio_cache.pop(token, None)

# ---------- Команды ----------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥TikGram_installer_bot\n\n"
        "Отправь ссылку на TikTok,Instagram,YouTube,Pinterest и бот скачает видео/изображения."
    )

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    await add_user(m.from_user.id)
    await m.answer(
        "🔥TikGram_installer_bot\n\n"
        "Отправь ссылку на TikTok,Instagram,YouTube,Pinterest и бот скачает видео/изображения."
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
        f"Обычный(по умолчанию)\n"
        f"4 видео в день обычное\n\n"
        f"🥇 Золотой — {GOLD_PRICE}⭐ ({GOLD_DAYS} дней)\n" f"10 видео в день - хорошее разрешение\n\n"
        f"💠 Алмазный — {DIAMOND_PRICE}⭐ ({DIAMOND_DAYS} дней)\n" f"неограниченные видео в день - высокое разрешение - приоритет\n\n"
        "Команды:\n/buy_gold\n/buy_diamond"
    )

@dp.message(Command("about"))
async def about_handler(m: Message):
    await m.answer("🤖 Бот конвертирует ссылки в видео и может вырезать аудио из видео. Поддерживает Pinterest и TikTok photo.")

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

@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔗 Отправьте ссылку на видео/пин и я обработаю его и пришлю вам!")

@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    await add_user(user_id)
    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return
    await download_queue.put((m.chat.id, user_id, m.text))
    await m.answer("📥 Добавлено в очередь...")

# ---------- Команда /limit ----------
@dp.message(Command("limit"))
async def limit_handler(m: Message):
    await add_user(m.from_user.id)
    try:
        remaining, limit, premium = await get_remaining_downloads(m.from_user.id)
    except Exception as e:
        await m.answer(f"Ошибка при получении лимита: {e}")
        return

    if limit is None:
        text = f"♾ У вас безлимитный тариф\n💎 Статус: {premium}"
    else:
        text = (
            f"📊 Ваш лимит на сегодня:\n\n"
            f"💎 Статус: {premium}\n"
            f"⬇️ Осталось скачиваний: {remaining}/{limit}"
        )
    await m.answer(text)

# ---------- Админ команды ----------
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

# ---------- Запуск ----------
async def main():
    await init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook (ok to ignore)")

    asyncio.create_task(download_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())