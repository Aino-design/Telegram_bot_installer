# main.py
# Рабочая версия: скачивает видео (YouTube / Shorts, TikTok, Pinterest и др.), отправляет видео,
# сразу пытается извлечь аудио и хранит его в кеше, кнопка "Получить песню 🎵" отправляет аудио.
# Убраны "звёзды" — остались только очки (points). Добавлена команда /farm (cooldown 20 часов).
import os
import re
import json
import uuid
import shutil
import random
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from functools import partial

import requests
import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

# -------------------- Настройки / лог --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "REPLACE_WITH_YOUR_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

# премиум / цены (в очках)
GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

# временные настройки
AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")
MIN_VIDEO_BYTES = 50_000  # минимальный приемлемый размер видео в байтах

# -------------------- БД: init + помощники --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            premium TEXT DEFAULT 'обычный',
            points INTEGER DEFAULT 0,
            downloads INTEGER DEFAULT 0,
            reset TEXT,
            expires TEXT,
            last_farm TEXT
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
        async with db.execute("SELECT id, premium, points, downloads, reset, expires, last_farm FROM users WHERE id=?", (uid,)) as cur:
            return await cur.fetchone()

async def set_premium(uid: int, level: str, days: int):
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, reset) VALUES(?, ?)", (uid, datetime.now(timezone.utc).isoformat()))
        await db.execute("UPDATE users SET premium=?, expires=? WHERE id=?", (level, exp, uid))
        await db.commit()

async def add_points(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, reset) VALUES(?, ?)", (uid, datetime.now(timezone.utc).isoformat()))
        await db.execute("UPDATE users SET points = COALESCE(points,0) + ? WHERE id=?", (amount, uid))
        await db.commit()

async def use_points(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT points FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if (row[0] or 0) < amount:
            return False
        await db.execute("UPDATE users SET points = points - ? WHERE id=?", (amount, uid))
        await db.commit()
    return True

async def increment_download(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, reset) VALUES(?, ?)", (uid, datetime.now(timezone.utc).isoformat()))
        await db.execute("UPDATE users SET downloads = COALESCE(downloads,0) + 1 WHERE id=?", (uid,))
        await db.commit()

async def set_last_farm(uid: int, iso_ts: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_farm=? WHERE id=?", (iso_ts, uid))
        await db.commit()

# лимиты
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

# -------------------- Веб-парсинг и скачивание --------------------
def resolve_redirect(url: str, timeout: int = 10) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, allow_redirects=True)
        if r.status_code in (200, 301, 302):
            return r.url
    except Exception:
        pass
    return url

def find_media_urls_from_html(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'https?://[^\s"\'<>]+?\.(?:mp4|webm|m3u8)(?:\?[^"\s<>]*)?', html, flags=re.IGNORECASE):
        out.append(m.group(0))
    # JSON-like keys
    for key in ('playAddr','downloadAddr','videoUrl','contentUrl','src'):
        for m in re.finditer(rf'"{key}"\s*:\s*"(https?://[^"]+)"', html, flags=re.IGNORECASE):
            candidate = m.group(1)
            if re.search(r'\.(?:mp4|webm|m3u8)(?:\?|$)', candidate, flags=re.IGNORECASE):
                out.append(candidate)
    seen = set(); res = []
    for u in out:
        uu = u.replace('\\u002F','/').replace('\\/','/').replace('\\','')
        if uu not in seen:
            seen.add(uu); res.append(uu)
    return res

def find_image_urls_from_html(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp|gif)(?:\?[^"\s<>]*)?', html, flags=re.IGNORECASE):
        out.append(m.group(0))
    seen = set(); res = []
    for u in out:
        uu = u.replace('\\u002F','/').replace('\\/','/').replace('\\','')
        if uu not in seen:
            seen.add(uu); res.append(uu)
    return res

def download_file_sync(url: str, dest_path: str, timeout: int = 30) -> bool:
    try:
        with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.debug("download_file_sync failed %s -> %s", url, e)
        return False

# -------------------- yt-dlp wrapper --------------------
def download_with_ytdlp(url: str, folder: str, cookiefile: Optional[str] = None) -> str:
    outtmpl = os.path.join(folder, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "noplaylist": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "allow_unplayable_formats": True,
        "merge_output_format": "mp4",
    }
    if cookiefile and os.path.exists(cookiefile):
        ydl_opts["cookiefile"] = cookiefile
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        try:
            filename = ydl.prepare_filename(info)
            if os.path.exists(filename):
                return filename
            alt = os.path.splitext(filename)[0] + ".mp4"
            if os.path.exists(alt):
                return alt
        except Exception:
            pass
        files = [os.path.join(folder, f) for f in os.listdir(folder)]
        files = [f for f in files if os.path.isfile(f)]
        if not files:
            raise Exception("yt-dlp didn't save any file")
        return sorted(files, key=os.path.getmtime, reverse=True)[0]

def safe_download_video(url: str, folder: str) -> None:
    logger.info("safe_download_video start url=%s", url)
    url = resolve_redirect(url)

    # 1) try yt-dlp first
    try:
        filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
        logger.info("download_with_ytdlp saved %s", filename)
        return
    except Exception as e:
        logger.info("yt-dlp failed: %s", e)

    # 2) try html scraping for direct video urls
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        html = r.text
        media_urls = find_media_urls_from_html(html)
        if media_urls:
            for m_url in media_urls:
                ext_m = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', m_url)
                ext = ext_m.group(1) if ext_m else "mp4"
                dst = os.path.join(folder, "scraped_video." + ext)
                if download_file_sync(m_url, dst):
                    logger.info("downloaded scraped media %s", dst)
                    return
    except Exception as e:
        logger.debug("html media scrape failed: %s", e)

    # 3) try to save images (caller decides to reject)
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
        html = r.text
        image_urls = find_image_urls_from_html(html)
        for i, img in enumerate(image_urls[:10], start=1):
            ext = "jpg"
            m = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', img)
            if m:
                ext = m.group(1)
            dest = os.path.join(folder, f"image_{i}.{ext}")
            download_file_sync(img, dest)
        return
    except Exception:
        pass

# -------------------- ffmpeg audio extraction --------------------
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
        tmpdir = info.get("tmpdir")
        audio = info.get("audio")
        if audio and os.path.exists(audio):
            os.remove(audio)
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        logger.exception("cleanup_audio_after_delay failed")
    audio_cache.pop(token, None)

# -------------------- Worker: скачивает, отправляет видео, пред-извлекает аудио --------------------
async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            # ищем видео
            video_path = None
            image_paths = []
            for f in os.listdir(tmp):
                if f.lower().endswith(VIDEO_EXTS):
                    video_path = os.path.join(tmp, f)
                    break
            if not video_path:
                for f in os.listdir(tmp):
                    if re.search(r'\.(mp4|webm|m3u8|mov|mkv)$', f, flags=re.IGNORECASE):
                        video_path = os.path.join(tmp, f)
                        break
            if not video_path:
                for f in os.listdir(tmp):
                    if f.lower().endswith(IMAGE_EXTS):
                        image_paths.append(os.path.join(tmp, f))

            # если только изображения — отказываемся
            if image_paths and not video_path:
                await bot.send_message(chat_id, "❌ Я не работаю с изображениями. Пожалуйста, пришлите ссылку на видео.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            if not video_path:
                await bot.send_message(chat_id, "❌ Не удалось скачать видео с этой ссылки.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # проверяем размер видео (иногда yt-dlp сохраняет маленький файл)
            try:
                size = os.path.getsize(video_path)
            except Exception:
                size = 0

            if size < MIN_VIDEO_BYTES:
                # попытка повторного скачивания через yt-dlp (жёсткая попытка)
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                    tmp = tempfile.mkdtemp()
                    filename = download_with_ytdlp(url, tmp, cookiefile=COOKIES_FILE if USE_COOKIES else None)
                    video_path = filename
                    size = os.path.getsize(video_path) if os.path.exists(video_path) else 0
                except Exception:
                    size = 0

            if size < MIN_VIDEO_BYTES:
                await bot.send_message(chat_id, "❌ Скачанное видео слишком маленькое или не содержит контента — попробуйте другую ссылку.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # отправляем видео и создаём токен для аудио
            token = uuid.uuid4().hex
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
            ])
            caption = "✅ Готово!\nНажмите кнопку ниже, чтобы получить аудио из видео."
            sent_ok = False
            try:
                await bot.send_video(chat_id, FSInputFile(video_path), caption=caption, reply_markup=kb)
                sent_ok = True
            except Exception:
                logger.exception("send_video failed; trying send_document")
                try:
                    await bot.send_document(chat_id, FSInputFile(video_path), caption=caption, reply_markup=kb)
                    sent_ok = True
                except Exception as e:
                    logger.exception("send_document failed: %s", e)
                    await bot.send_message(chat_id, f"❌ Ошибка отправки видео: {e}")
                    sent_ok = False

            if not sent_ok:
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # отмечаем скачивание в БД
            try:
                await increment_download(user_id)
            except Exception:
                logger.exception("increment_download failed for %s", user_id)

            # готовим запись в кеше: пытаемся сразу извлечь аудио (фоновая задача, но хотим максимальную вероятность)
            audio_path = os.path.join(tmp, "audio.mp3")
            audio_ok = False
            try:
                # пытаемся извлечь (await — запускает ffmpeg subprocess)
                audio_ok = await extract_audio_ffmpeg(video_path, audio_path)
            except Exception:
                audio_ok = False

            audio_cache[token] = {
                "audio": audio_path if audio_ok else None,
                "tmpdir": tmp,
                "video": video_path,
                "url": url,
                "owner": user_id
            }
            # планируем удаление кеша через AUDIO_TTL_SECONDS
            asyncio.create_task(cleanup_audio_after_delay(token, AUDIO_TTL_SECONDS))
            continue

        except Exception as exc:
            logger.exception("download_worker exception: %s", exc)
            try:
                await bot.send_message(chat_id, f"❌ Ошибка: {exc}")
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)
            continue

# -------------------- Callback: получение аудио (из кеша или извлечение на месте) --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("get_audio:"))
async def cb_get_audio(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    info = audio_cache.get(token)
    if not info:
        await cq.answer("⚠️ Аудио устарело или недоступно — пришлите ссылку ещё раз.", show_alert=True)
        return

    owner = info.get("owner")
    if owner and cq.from_user.id != owner and cq.from_user.id != ADMIN_ID:
        await cq.answer("Только тот, кто запросил видео, может получить аудио.", show_alert=True)
        return

    await cq.answer()  # скрытая ответка

    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")
    video_path = info.get("video")
    url = info.get("url")

    # если аудио уже пред-извлечено — отправляем
    if audio_path and os.path.exists(audio_path):
        try:
            await bot.send_chat_action(cq.from_user.id, "upload_audio")
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path), title="Аудио из видео")
        except Exception:
            await cq.answer("Ошибка при отправке аудио.", show_alert=True)
        finally:
            # удаляем ресурсы
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
        return

    # иначе — пробуем извлечь сейчас из сохранённого видео
    if video_path and os.path.exists(video_path):
        audio_path_new = os.path.join(tmpdir, "audio_on_demand.mp3")
        await bot.send_chat_action(cq.from_user.id, "record_audio")
        ok = await extract_audio_ffmpeg(video_path, audio_path_new)
        if not ok:
            # пробуем повторно скачать и извлечь
            new_tmp = tempfile.mkdtemp()
            try:
                await cq.answer("Попытка повторного скачивания для извлечения аудио...", show_alert=True)
                await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, new_tmp))
                new_video = None
                for f in os.listdir(new_tmp):
                    if f.lower().endswith(VIDEO_EXTS):
                        new_video = os.path.join(new_tmp, f)
                        break
                if new_video:
                    audio_path_new2 = os.path.join(new_tmp, "audio_retry.mp3")
                    ok2 = await extract_audio_ffmpeg(new_video, audio_path_new2)
                    if ok2:
                        try:
                            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path_new2), title="Аудио из видео")
                        except Exception:
                            await cq.answer("Ошибка при отправке аудио.", show_alert=True)
                        finally:
                            try:
                                if os.path.exists(audio_path_new2):
                                    os.remove(audio_path_new2)
                                shutil.rmtree(new_tmp, ignore_errors=True)
                            except Exception:
                                pass
                        audio_cache.pop(token, None)
                        return
                await cq.answer("Не удалось извлечь аудио.", show_alert=True)
            except Exception:
                await cq.answer("Ошибка при повторном скачивании/конвертации.", show_alert=True)
            finally:
                try:
                    shutil.rmtree(new_tmp, ignore_errors=True)
                except Exception:
                    pass
            audio_cache.pop(token, None)
            return

        # если успешно извлекли сейчас
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

    await cq.answer("Аудио устарело или недоступно — пришлите ссылку ещё раз.", show_alert=True)
    audio_cache.pop(token, None)

# -------------------- Команды: /start, /about, /premium, /profile, /farm --------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔥 TikGram_bot\n\nОтправь ссылку на видео (YouTube/Shorts, TikTok, Pinterest и т.д.) — бот скачает и пришлёт видео. После отправки видео можно получить аудио через кнопку.")

@dp.message(Command("about"))
async def about_handler(m: Message):
    await m.answer("🤖 Бот скачивает видео по ссылкам и позволяет получить аудио из видео (кнопка «Получить песню 🎵»).")

@dp.message(Command("premium"))
async def premium_handler(m: Message):
    await add_user(m.from_user.id)
    user = await get_user(m.from_user.id)
    premium = user[1] if user else "обычный"
    expires = user[5] or "—"
    points = (user[2] or 0) if user else 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Купить GOLD ({GOLD_PRICE} очков)", callback_data="buy_gold_points")],
        [InlineKeyboardButton(text=f"Купить DIAMOND ({DIAMOND_PRICE} очков)", callback_data="buy_diamond_points")]
    ])
    text = f"💎 Уровень премиума: {premium}\n⏳ Действует до: {expires}\n🔹 Очки: {points}\n\nНажмите кнопку, чтобы купить премиум за очки."
    await m.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data in ("buy_gold_points", "buy_diamond_points"))
async def buy_points_cb(cq: CallbackQuery):
    uid = cq.from_user.id
    await add_user(uid)
    if cq.data == "buy_gold_points":
        price, days, level = GOLD_PRICE, GOLD_DAYS, "золотой"
    else:
        price, days, level = DIAMOND_PRICE, DIAMOND_DAYS, "алмазный"
    ok = await use_points(uid, price)
    if ok:
        await set_premium(uid, level, days)
        await cq.answer(f"✅ Вы купили {level} на {days} дней за {price} очков.", show_alert=True)
    else:
        await cq.answer(f"❌ У вас недостаточно очков. Цена: {price} очков.", show_alert=True)

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Профиль: не найден")
        return
    await m.answer(f"👤 Профиль\n💎 {user[1]}\n🔹 Очки: {user[2]}\n📥 Скачиваний: {user[3]}")

# -------------------- /farm — фарм очков каждые 20 часов (10-35) --------------------
@dp.message(Command("farm"))
async def farm_points(m: Message):
    await add_user(m.from_user.id)
    uid = m.from_user.id
    user = await get_user(uid)
    last_farm_iso = user[6] if user else None
    now = datetime.now(timezone.utc)
    if last_farm_iso:
        try:
            last = datetime.fromisoformat(last_farm_iso)
        except Exception:
            last = datetime.fromtimestamp(0, timezone.utc)
        delta = now - last
        if delta < timedelta(hours=20):
            remain = timedelta(hours=20) - delta
            hours = int(remain.total_seconds() // 3600)
            minutes = int((remain.total_seconds() % 3600) // 60)
            await m.answer(f"⏳ Вы уже фармили. Можно снова через {hours}ч {minutes}м.")
            return
    amount = random.randint(10, 35)
    await add_points(uid, amount)
    await set_last_farm(uid, now.isoformat())
    await m.answer(f"🎉 Вы получили {amount} очков! (Можно фармить снова через 20 часов)")

# -------------------- Обработка входящих ссылок (http...) --------------------
def looks_like_image_url(url: str) -> bool:
    lower = url.lower()
    if any(lower.endswith(ext) for ext in IMAGE_EXTS):
        return True
    if "tiktok.com" in lower and "/photo/" in lower:
        return True
    return False

@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    url = m.text.strip()
    await add_user(user_id)

    if looks_like_image_url(url):
        await m.answer("❌ Я не работаю с изображениями. Пожалуйста, пришлите ссылку на видео.")
        return

    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return

    await download_queue.put((m.chat.id, user_id, url))
    await m.answer("📥 Добавлено в очередь на скачивание...")

# -------------------- Админ (скрыт) --------------------
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "🛠 Админ панель (только для владельца):\n"
        "/stats — Статистика\n"
        "/give_gold ID — Выдать Золотой\n"
        "/give_diamond ID — Выдать Алмазный\n"
        "/give_points ID сумма — Выдать очки"
    )

@dp.message(F.text.startswith("/stats"))
async def stats_handler_admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium, COUNT(*) FROM users GROUP BY premium") as cur:
            rows = await cur.fetchall()
        premium_counts = {r[0]: r[1] for r in rows}
        total_premium = sum(v for k,v in premium_counts.items() if k in ("золотой","алмазный"))
        async with db.execute("SELECT id, points FROM users ORDER BY points DESC LIMIT 10") as cur:
            top = await cur.fetchall()
    text = f"📊 Статистика\nВсего премиум-пользователей (золотой/алмазный): {total_premium}\n\n🏆 Топ-10 по очкам:\n"
    if not top:
        text += "Пока нет игроков."
    else:
        for i,row in enumerate(top, start=1):
            uid, pts = row
            text += f"{i}. {uid} — {pts} очков\n"
    await m.answer(text)

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

@dp.message(F.text.startswith("/give_points"))
async def give_points_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        parts = m.text.split()
        uid = int(parts[1]); amount = int(parts[2])
        await add_points(uid, amount)
        await m.answer(f"✅ Начислено {amount} очков пользователю {uid}")
    except Exception:
        await m.answer("❌ Неверный формат. /give_points ID сумма")

# -------------------- Запуск --------------------
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