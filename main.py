# main.py — полная версия с поддержкой нескольких администраторов
import os
import re
import uuid
import shutil
import random
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple
from functools import partial

import requests
import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# -------------------- Лог и конфиг --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Railway предоставляет порт через переменную окружения PORT
PORT = int(os.getenv("PORT", 8080))
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден! Установите переменную окружения TOKEN")

# Администраторы: можно указать несколько ID через запятую в переменной ADMINS
ADMINS_STR = os.getenv("ADMINS", os.getenv("ADMIN_ID", "6705555401"))
ADMINS = [int(x.strip()) for x in ADMINS_STR.split(",") if x.strip().isdigit()]
if not ADMINS:
    ADMINS = [6705555401, 7476993474]  # значение по умолчанию
logger.info(f"Admins: {ADMINS}")

DB_PATH = os.getenv("DB_PATH", "data/bot_db.sqlite")

# Создаём директорию для базы данных, если её нет
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# премиум / очки / лимиты
GOLD_PRICE = int(os.getenv("GOLD_PRICE", "120"))
GOLD_DAYS = int(os.getenv("GOLD_DAYS", "30"))
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE", "250"))
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS", "90"))
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}  # None = безлимит

AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS", "1800"))  # 30 минут
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".aac", ".opus")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")
MIN_VIDEO_BYTES = 20_000  # минимальный размер принимаемого видео (байты)

# Инициализация бота и диспетчера
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}
BOT_USERNAME: Optional[str] = None

# Очередь загрузок будет создана в on_startup
download_queue: asyncio.Queue = None

# ---------------- ffmpeg / ffprobe ----------------
HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None
logger.info("ffmpeg available: %s, ffprobe available: %s", HAS_FFMPEG, HAS_FFPROBE)

# -------------------- DB --------------------
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
    logger.info("DB initialized at %s", DB_PATH)

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
        if not row or (row[0] or 0) < amount:
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

# -------------------- Веб-помощники --------------------
def resolve_redirect(url: str, timeout: int = 10) -> str:
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=timeout, allow_redirects=True)
        if r.status_code in (200,301,302):
            return r.url
    except Exception:
        pass
    return url

def extract_first_link_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'(https?://[^\s\)\]\}\,]+)', text)
    if m:
        return m.group(1).rstrip('.,)')
    return None

def find_media_urls_from_html(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'https?://[^\s"\'<>]+?\.(?:mp4|webm|m3u8)(?:\?[^"\s<>]*)?', html, flags=re.IGNORECASE):
        out.append(m.group(0))
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
        with requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.debug("download_file_sync failed %s -> %s", url, e)
        return False

# -------------------- yt-dlp wrapper с упрощённой логикой для YouTube --------------------
def download_with_ytdlp(url: str, folder: str, cookiefile: Optional[str] = None) -> str:
    """
    Упрощённая стратегия для YouTube: перебираем несколько клиентов и форматов.
    Для других сайтов используем стандартный подход.
    """
    # Определяем, YouTube ли это
    is_youtube = "youtube.com" in url or "youtu.be" in url
    
    # Базовые опции
    base_opts = {
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "noplaylist": True,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        "allow_unplayable_formats": True,
        "merge_output_format": "mp4",
        "no_color": True,
        "geo_bypass": True,
    }
    
    if cookiefile and os.path.exists(cookiefile):
        base_opts["cookiefile"] = cookiefile
    
    if not is_youtube:
        # Для не-YouTube просто пробуем best
        base_opts["format"] = "bestvideo+bestaudio/best"
        with YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return _get_filename(ydl, info, folder)
    
    # Для YouTube перебираем клиентов
    clients = [
        "android",      # часто работает
        "web",          # стандартный
        "ios",          # мобильный
        "android_embedded",
        "web_embedded",
        "android_vr",
        "web_safari",
    ]
    
    formats = [
        "bestvideo+bestaudio/best",
        "best",
        "best[height<=720]",
        "best[height<=480]",
        "worst",
    ]
    
    last_error = None
    for client in clients:
        for fmt in formats:
            try:
                opts = base_opts.copy()
                opts["extractor_args"] = {"youtube": {"player_client": [client]}}
                opts["format"] = fmt
                logger.info(f"Попытка YouTube client={client}, format={fmt}")
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return _get_filename(ydl, info, folder)
            except Exception as e:
                last_error = e
                logger.info(f"YouTube client={client}, format={fmt} не удался: {e}")
                continue
    
    # Если ничего не сработало, пробуем без указания клиента (стандартный)
    try:
        opts = base_opts.copy()
        opts["format"] = "best"
        logger.info("Попытка YouTube без указания клиента")
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return _get_filename(ydl, info, folder)
    except Exception as e:
        last_error = e
    
    raise last_error or Exception("Не удалось скачать YouTube видео")

def _get_filename(ydl, info, folder):
    """Вспомогательная функция для получения имени файла после скачивания."""
    if 'requested_downloads' in info and info['requested_downloads']:
        filename = info['requested_downloads'][0].get('filepath')
        if filename and os.path.exists(filename):
            return filename
    try:
        filename = ydl.prepare_filename(info)
        if os.path.exists(filename):
            return filename
        alt = os.path.splitext(filename)[0] + ".mp4"
        if os.path.exists(alt):
            return alt
    except Exception:
        pass
    # Ищем любой файл в папке
    files = [os.path.join(folder, f) for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    if not files:
        raise Exception("yt-dlp не сохранил ни одного файла")
    return sorted(files, key=os.path.getmtime, reverse=True)[0]

def safe_download_video(url: str, folder: str) -> None:
    """Основная функция скачивания: сначала yt-dlp, затем скрапинг."""
    logger.info("safe_download_video: %s", url)
    url = resolve_redirect(url)
    
    # Пытаемся через yt-dlp
    try:
        filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
        logger.info("yt-dlp успешно сохранил %s", filename)
        return
    except Exception as e:
        logger.info(f"yt-dlp полностью не удался: {e}")

    # Скрапинг HTML для поиска прямых ссылок на видео
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        html = r.text
        media_urls = find_media_urls_from_html(html)
        if media_urls:
            for m_url in media_urls:
                ext_m = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', m_url)
                ext = ext_m.group(1) if ext_m else "mp4"
                dst = os.path.join(folder, "scraped_video." + ext)
                if download_file_sync(m_url, dst):
                    logger.info("сохранено через скрапинг %s", dst)
                    return
    except Exception as e:
        logger.debug("скрапинг видео не удался: %s", e)

    # Последний шанс: картинки
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
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

# -------------------- ffprobe helpers --------------------
async def has_video_stream(path: str) -> bool:
    if os.path.getsize(path) == 0:
        return False
    if not HAS_FFPROBE:
        lower = path.lower()
        return any(lower.endswith(ext) for ext in (".mp4", ".mkv", ".mov", ".ts", ".webm"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=index", "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return bool(out and out.strip())
    except Exception:
        return False

async def has_audio_stream(path: str) -> bool:
    if os.path.getsize(path) == 0:
        return False
    if not HAS_FFPROBE:
        lower = path.lower()
        return any(lower.endswith(ext) for ext in AUDIO_EXTS + (".mp4", ".webm"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return bool(out and out.strip())
    except Exception:
        return False

# -------------------- merge helper --------------------
async def merge_video_and_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    if not HAS_FFMPEG:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", video_path, "-i", audio_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            output_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception:
        logger.exception("ошибка при слиянии")
        return False

# -------------------- extract audio via ffmpeg --------------------
async def extract_audio_ffmpeg(video_path: str, output_audio_path: str) -> bool:
    if not HAS_FFMPEG:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "mp3", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_audio_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 0
    except Exception:
        logger.exception("extract_audio_ffmpeg error")
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

# -------------------- Find audio-only candidate --------------------
async def find_audio_candidate(folder: str, video_path: str) -> Optional[str]:
    files = [os.path.join(folder, f) for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    audio_candidates = []
    for f in files:
        if os.path.abspath(f) == os.path.abspath(video_path):
            continue
        try:
            a = await has_audio_stream(f)
            v = await has_video_stream(f)
            if a and not v:
                audio_candidates.append(f)
        except Exception:
            pass
    if audio_candidates:
        audio_candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
        return audio_candidates[0]
    for f in files:
        if f.lower().endswith(AUDIO_EXTS):
            return f
    return None

# -------------------- Download worker --------------------
async def download_worker():
    logger.info("Download worker started")
    global download_queue
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            # Скачивание
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            # Собираем файлы, игнорируем нулевые
            candidates = []
            for f in os.listdir(tmp):
                full = os.path.join(tmp, f)
                if os.path.isfile(full) and os.path.getsize(full) > 0:
                    candidates.append(full)
            
            if not candidates:
                await bot.send_message(chat_id, "❌ Не удалось скачать ничего по этой ссылке.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # Выбираем видео
            chosen_video = None
            for p in candidates:
                if await has_video_stream(p):
                    chosen_video = p
                    break

            # Проверяем на картинки
            images = [p for p in candidates if p.lower().endswith(IMAGE_EXTS)]
            if images and not chosen_video:
                await bot.send_message(chat_id, "❌ Я не работаю с изображениями. Пришлите ссылку на видео.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            if not chosen_video:
                await bot.send_message(chat_id, "❌ Не удалось найти видео в скачанном контенте.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # Если нет аудио — ищем отдельный аудиофайл и сливаем
            if not await has_audio_stream(chosen_video):
                audio_candidate = await find_audio_candidate(tmp, chosen_video)
                if audio_candidate and HAS_FFMPEG:
                    merged_path = os.path.join(tmp, "merged_" + uuid.uuid4().hex + ".mp4")
                    merged_ok = await merge_video_and_audio(chosen_video, audio_candidate, merged_path)
                    if merged_ok:
                        chosen_video = merged_path
                        logger.info("успешно слили аудио и видео")

            # Проверяем размер (только предупреждение, не блокируем отправку)
            size = os.path.getsize(chosen_video)
            if size < MIN_VIDEO_BYTES:
                logger.warning(f"Видео имеет маленький размер: {size} байт, но отправляем.")

            # Заголовок
            title = None
            title_file = os.path.join(tmp, "title.txt")
            if os.path.exists(title_file):
                try:
                    with open(title_file, "r", encoding="utf-8") as f:
                        title = f.read().strip()
                except Exception:
                    pass
            if not title:
                title = os.path.splitext(os.path.basename(chosen_video))[0]

            # Отправка с кнопкой
            token = uuid.uuid4().hex
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
            ])
            caption = "✅ Готово!\nНажмите кнопку, чтобы получить аудио."
            
            try:
                await bot.send_video(chat_id, FSInputFile(chosen_video), caption=caption, reply_markup=kb)
            except Exception:
                try:
                    await bot.send_document(chat_id, FSInputFile(chosen_video), caption=caption, reply_markup=kb)
                except Exception as e:
                    await bot.send_message(chat_id, f"❌ Ошибка отправки: {e}")
                    shutil.rmtree(tmp, ignore_errors=True)
                    continue

            # Счётчик
            try:
                await increment_download(user_id)
            except Exception:
                pass

            # Предварительное извлечение аудио
            audio_path = os.path.join(tmp, "audio.mp3")
            audio_ok = await extract_audio_ffmpeg(chosen_video, audio_path)

            audio_cache[token] = {
                "audio": audio_path if audio_ok and os.path.exists(audio_path) else None,
                "tmpdir": tmp,
                "video": chosen_video,
                "url": url,
                "owner": user_id,
                "title": title or "Аудио из видео"
            }
            asyncio.create_task(cleanup_audio_after_delay(token, AUDIO_TTL_SECONDS))

        except Exception as exc:
            logger.exception("download_worker ошибка: %s", exc)
            try:
                await bot.send_message(chat_id, f"❌ Ошибка: {exc}")
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)

# -------------------- Callback: получить аудио --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("get_audio:"))
async def cb_get_audio(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    info = audio_cache.get(token)
    if not info:
        await cq.answer("⚠️ Аудио устарело. Пришлите ссылку ещё раз.", show_alert=True)
        return

    owner = info.get("owner")
    if owner and cq.from_user.id != owner and cq.from_user.id not in ADMINS:
        await cq.answer("Только автор запроса может получить аудио.", show_alert=True)
        return

    await cq.answer()
    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")
    video_path = info.get("video")
    url = info.get("url")
    title = info.get("title") or "Аудио из видео"

    # Если аудио уже готово
    if audio_path and os.path.exists(audio_path):
        try:
            await bot.send_chat_action(cq.from_user.id, "upload_audio")
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path), title=title)
        except Exception:
            await cq.answer("Ошибка отправки аудио.", show_alert=True)
        finally:
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
        return

    # Пробуем извлечь сейчас
    if video_path and os.path.exists(video_path) and HAS_FFMPEG:
        audio_now = os.path.join(tmpdir, "audio_on_demand.mp3")
        await bot.send_chat_action(cq.from_user.id, "record_audio")
        ok = await extract_audio_ffmpeg(video_path, audio_now)
        if ok and os.path.exists(audio_now):
            try:
                await bot.send_audio(cq.from_user.id, FSInputFile(audio_now), title=title)
            except Exception:
                await cq.answer("Ошибка отправки аудио.", show_alert=True)
            finally:
                try:
                    if os.path.exists(audio_now):
                        os.remove(audio_now)
                    if tmpdir and os.path.exists(tmpdir):
                        shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass
                audio_cache.pop(token, None)
            return

    audio_cache.pop(token, None)
    await cq.answer("Не удалось извлечь аудио.", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("group_dl:"))
async def cb_group_dl(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    info = audio_cache.get(token)
    if not info:
        await cq.answer("⚠️ Ссылка устарела.", show_alert=True)
        return

    owner = info.get("owner")
    if cq.from_user.id != owner and cq.from_user.id not in ADMINS:
        await cq.answer("Только автор может скачать видео.", show_alert=True)
        return

    url = info.get("url")
    chat_id = info.get("chat_id")

    audio_cache.pop(token, None)
    await download_queue.put((chat_id, cq.from_user.id, url))
    await cq.answer("📥 Видео добавлено в очередь...")

# -------------------- Команды --------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    txt = (
        "🔥 TikGram_bot\n\n"
        "Скачиваю видео по ссылкам (YouTube, TikTok, Pinterest и др.)\n"
        "После видео появится кнопка для получения MP3.\n\n"
        "Команды:\n"
        "/premium — Инфо о премиуме\n"
        "/profile — Профиль\n"
        "/farm — Фарм очков (раз в 20 часов, 10–35 очков)\n"
        "/list_admin — Админ-панель (только для админа)\n\n"
    )
    await m.answer(txt)

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
    text = (
        f"💎 Уровень: {premium}\n"
        f"⏳ Действует до: {expires}\n"
        f"🔹 Очки: {points}\n"
        f"• Золотой — 10 загрузок/день, {GOLD_DAYS} дней ({GOLD_PRICE} очков)\n"
        f"• Алмазный — безлимит, {DIAMOND_DAYS} дней ({DIAMOND_PRICE} очков)"
    )
    await m.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data in ("buy_gold_points","buy_diamond_points"))
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
        await cq.answer(f"✅ Куплен {level} на {days} дней", show_alert=True)
    else:
        await cq.answer(f"❌ Недостаточно очков. Нужно: {price}", show_alert=True)

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    await add_user(m.from_user.id)
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("Профиль не найден")
        return
    remaining, limit, premium = await get_remaining_downloads(m.from_user.id)
    downloads_text = "♾ Безлимит" if remaining is None else f"{remaining}/{limit}"
    points = user[2] or 0
    await m.answer(f"👤 Профиль\n💎 {premium}\n🔹 Очки: {points}\n📥 Осталось: {downloads_text}")

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
            await m.answer(f"⏳ Можно снова через {hours}ч {minutes}м.")
            return
    amount = random.randint(10, 35)
    await add_points(uid, amount)
    await set_last_farm(uid, now.isoformat())
    await m.answer(f"🎉 Получено {amount} очков!")

# -------------------- Админ-команда /list_admin --------------------
@dp.message(Command("list_admin"))
async def list_admin_handler(m: Message):
    if m.from_user.id not in ADMINS:
        return
    await m.answer(
        "🛠 Админ панель:\n"
        "/stats — статистика\n"
        "/give_gold ID — выдать золотой\n"
        "/give_diamond ID — выдать алмазный\n"
        "/give_points ID сумма — выдать очки"
    )

@dp.message(F.text.startswith("/stats"))
async def stats_handler_admin(m: Message):
    if m.from_user.id not in ADMINS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium, COUNT(*) FROM users GROUP BY premium") as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT id, points FROM users ORDER BY points DESC LIMIT 10") as cur:
            top = await cur.fetchall()
    premium_counts = {r[0]: r[1] for r in rows} if rows else {}
    total_premium = sum(v for k,v in premium_counts.items() if k in ("золотой","алмазный"))
    text = f"📊 Статистика\nВсего премиум: {total_premium}\n\n🏆 Топ-10 по очкам:\n"
    if not top:
        text += "Пока нет."
    else:
        for i,row in enumerate(top, start=1):
            uid, pts = row
            text += f"{i}. {uid} — {pts} очков\n"
    await m.answer(text)

@dp.message(F.text.startswith("/give_gold"))
async def give_gold(m: Message):
    if m.from_user.id not in ADMINS:
        return
    try:
        uid = int(m.text.split()[1])
        await set_premium(uid, "золотой", GOLD_DAYS)
        await m.answer(f"✅ Золотой выдан {uid}")
    except Exception:
        await m.answer("❌ Неверный формат. /give_gold ID")

@dp.message(F.text.startswith("/give_diamond"))
async def give_diamond(m: Message):
    if m.from_user.id not in ADMINS:
        return
    try:
        uid = int(m.text.split()[1])
        await set_premium(uid, "алмазный", DIAMOND_DAYS)
        await m.answer(f"✅ Алмазный выдан {uid}")
    except Exception:
        await m.answer("❌ Неверный формат. /give_diamond ID")

@dp.message(F.text.startswith("/give_points"))
async def give_points_handler(m: Message):
    if m.from_user.id not in ADMINS:
        return
    try:
        parts = m.text.split()
        uid = int(parts[1]); amount = int(parts[2])
        await add_points(uid, amount)
        await m.answer(f"✅ Начислено {amount} очков {uid}")
    except Exception:
        await m.answer("❌ Неверный формат. /give_points ID сумма")

# -------------------- Обработка ссылок --------------------
@dp.message()
async def general_message_handler(m: Message):
    text = m.text or m.caption or ""
    if not text:
        return
    link = extract_first_link_from_text(text)
    if not link:
        return

    chat_type = m.chat.type

    # Проверка лимита
    if not await can_download(m.from_user.id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return

    if chat_type == "private":
        await download_queue.put((m.chat.id, m.from_user.id, link))
        await m.answer("📥 Добавлено в очередь...")
        return

    if chat_type in ("group", "supergroup"):
        token = uuid.uuid4().hex
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Извлечь видео", callback_data=f"group_dl:{token}")]
        ])
        audio_cache[token] = {
            "url": link,
            "owner": m.from_user.id,
            "chat_id": m.chat.id,
        }
        await m.reply(
            f"ℹ️ {m.from_user.first_name}, чтобы скачать видео, нажмите кнопку ниже.",
            reply_markup=kb
        )

# -------------------- Startup и Shutdown --------------------
async def on_startup():
    global download_queue
    await init_db()
    me = await bot.get_me()
    global BOT_USERNAME
    BOT_USERNAME = me.username
    logger.info("Bot username: %s", BOT_USERNAME)
    
    # Создаём очередь и запускаем воркер
    download_queue = asyncio.Queue()
    asyncio.create_task(download_worker())
    
    # Устанавливаем вебхук, если есть RAILWAY_STATIC_URL
    if os.getenv('RAILWAY_STATIC_URL'):
        webhook_url = f"https://{os.getenv('RAILWAY_STATIC_URL')}/webhook"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info("Webhook set to %s", webhook_url)

async def on_shutdown():
    await bot.delete_webhook()
    logger.info("Webhook removed")

dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)

# -------------------- Запуск --------------------
def main():
    if not os.getenv('RAILWAY_STATIC_URL'):
        logger.info("RAILWAY_STATIC_URL not set, starting polling...")
        try:
            asyncio.run(dp.start_polling(bot))
        except KeyboardInterrupt:
            logger.info("Bot stopped")
        return

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()