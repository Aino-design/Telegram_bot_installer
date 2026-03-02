# main.py — версия: работает только с видео; изображения отвергаются; admin скрыт
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
from typing import Dict, Any, Optional, Tuple, List
from functools import partial

import requests
import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)

# -------------------- Настройки / лог --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "REPLACE_WITH_YOUR_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

# премиум / цены (админ использует)
GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

# TTL временных файлов
AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)

# cookies.txt (опционально)
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()

audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")

# -------------------- DB --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            premium TEXT DEFAULT 'обычный',
            stars INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            downloads INTEGER DEFAULT 0,
            reset TEXT,
            expires TEXT,
            image_mode INTEGER DEFAULT 0,
            last_farm TEXT
        )""")
        await db.commit()
    logger.info("DB initialized")

async def add_user(uid: int):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, reset, image_mode) VALUES(?, ?, 0)", (uid, now_iso))
        await db.commit()

async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, premium, stars, points, downloads, reset, expires, image_mode, last_farm FROM users WHERE id=?", (uid,)) as cur:
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

async def add_points(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points=points+? WHERE id=?", (amount, uid))
        await db.commit()

async def use_points(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT points FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if (row[0] or 0) < amount:
            return False
        await db.execute("UPDATE users SET points=points-? WHERE id=?", (amount, uid))
        await db.commit()
    return True

async def use_stars(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT stars FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if (row[0] or 0) < amount:
            return False
        await db.execute("UPDATE users SET stars=stars-? WHERE id=?", (amount, uid))
        await db.commit()
    return True

async def increment_download(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET downloads = downloads + 1 WHERE id=?", (uid,))
        await db.commit()

# reset / limits
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
    premium, downloads = user[1], user[4]
    limit = LIMITS.get(premium, 4)
    if limit is None:
        return True
    return downloads < limit

# -------------------- Web helpers --------------------
def resolve_redirect(url: str, timeout: int = 10) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, allow_redirects=True)
        if r.status_code in (200, 301, 302):
            return r.url
    except Exception:
        pass
    return url

def sanitize_tiktok_photo_url(url: str) -> str:
    url = resolve_redirect(url)
    m = re.search(r'/photo/(\d+)', url)
    if m:
        photo_id = m.group(1)
        return f"https://www.tiktok.com/@user/photo/{photo_id}"
    return url

def find_image_urls_from_html(html: str) -> List[str]:
    urls = []
    for m in re.finditer(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp|gif)', html, flags=re.IGNORECASE):
        urls.append(m.group(0))
    for key in ('displayUrl','originCover','cover','downloadAddr','originImage','poster','og:image'):
        for m in re.finditer(rf'"{key}"\s*:\s*"(https?://[^"]+)"', html, flags=re.IGNORECASE):
            urls.append(m.group(1))
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        urls.append(m.group(1))
    seen = set(); out = []
    for u in urls:
        uu = u.replace('\\u002F', '/').replace('\\/', '/').replace('\\', '')
        if uu not in seen:
            seen.add(uu); out.append(uu)
    return out

def download_file_sync(url: str, dest_path: str, timeout: int = 20) -> bool:
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

def fetch_and_save_images_page(url: str, folder: str, limit: int = 10) -> List[str]:
    saved: List[str] = []
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        html = r.text
    except Exception as e:
        logger.debug("fetch page failed %s", e)
        return saved
    img_urls = find_image_urls_from_html(html)
    if not img_urls:
        m = re.search(r'<script[^>]*id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>', html, flags=re.DOTALL | re.IGNORECASE)
        if m:
            try:
                payload = m.group(1).strip()
                j = json.loads(payload)
                text = json.dumps(j)
                found = re.findall(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp|gif)', text)
                for u in found:
                    img_urls.append(u)
            except Exception:
                pass
    img_urls = list(dict.fromkeys(img_urls))
    for i, img_url in enumerate(img_urls[:limit], start=1):
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        ext_match = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', img_url)
        ext = ext_match.group(1) if ext_match else "jpg"
        fname = f"image_{i}.{ext}"
        dest = os.path.join(folder, fname)
        ok = download_file_sync(img_url, dest)
        if ok and os.path.exists(dest) and os.path.getsize(dest) > 0:
            saved.append(dest)
    return saved

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
            if not os.path.exists(filename):
                base = os.path.splitext(filename)[0]
                alt = base + ".mp4"
                if os.path.exists(alt):
                    filename = alt
        except Exception:
            files = [os.path.join(folder, f) for f in os.listdir(folder)]
            files = [f for f in files if os.path.isfile(f)]
            if files:
                filename = sorted(files, key=os.path.getmtime, reverse=True)[0]
            else:
                raise
        return filename

def safe_download_video(url: str, folder: str) -> None:
    logger.info("safe_download_video start url=%s", url)
    if any(s in url for s in ("vm.tiktok.com", "vt.tiktok.com", "https://vm.", "https://vt.")):
        url = resolve_redirect(url)

    # TikTok photo -> images (handled by caller as images)
    if "tiktok.com" in url and "/photo/" in url:
        saved = fetch_and_save_images_page(sanitize_tiktok_photo_url(url), folder, limit=10)
        if saved:
            return
        # try additional extraction
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            html = r.text
            m = re.search(r'<script[^>]*id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>', html, flags=re.DOTALL | re.IGNORECASE)
            found_urls = []
            if m:
                try:
                    payload = m.group(1).strip()
                    j = json.loads(payload)
                    text = json.dumps(j)
                    found_urls = re.findall(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp|gif)', text)
                except Exception:
                    found_urls = []
            if not found_urls:
                found_urls = re.findall(r'"displayUrl"\s*:\s*"(https?://[^"]+)"', html) + \
                             re.findall(r'"originCover"\s*:\s*"(https?://[^"]+)"', html)
            cleaned = []
            for u in found_urls:
                uu = u.replace('\\u002F', '/').replace('\\/', '/').replace('\\', '')
                if uu not in cleaned:
                    cleaned.append(uu)
            for i, img_url in enumerate(cleaned[:10], start=1):
                ext_m = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', img_url)
                ext = ext_m.group(1) if ext_m else "jpg"
                dest = os.path.join(folder, f"photo_{i}.{ext}")
                download_file_sync(img_url, dest)
            saved_local = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(("jpg","jpeg","png","webp","gif"))]
            if saved_local:
                return
        except Exception:
            pass
        raise Exception("Не удалось найти/скачать фото TikTok")

    # Pinterest: try yt-dlp first, else html images
    if "pinterest" in url or "pin.it" in url:
        try:
            filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
            return
        except Exception:
            saved = fetch_and_save_images_page(url, folder, limit=10)
            if saved:
                return
            raise

    # General: try yt-dlp then HTML fallback
    try:
        filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
        return
    except Exception:
        saved = fetch_and_save_images_page(url, folder, limit=10)
        if saved:
            return
        raise

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
        audio = info.get("audio")
        tmpdir = info.get("tmpdir")
        if audio and os.path.exists(audio):
            os.remove(audio)
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        logger.exception("cleanup_audio_after_delay failed")
    audio_cache.pop(token, None)

# -------------------- Download worker --------------------
MIN_VIDEO_BYTES = 50_000  # 50 KB threshold for "non-empty" video

async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token: Optional[str] = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            video_path: Optional[str] = None
            image_paths: List[str] = []
            for f in os.listdir(tmp):
                if f.lower().endswith(VIDEO_EXTS):
                    video_path = os.path.join(tmp, f)
                    break
            if not video_path:
                for f in os.listdir(tmp):
                    if f.lower().endswith(IMAGE_EXTS):
                        image_paths.append(os.path.join(tmp, f))

            # Если нашли только изображения -> уведомляем, что бот не работает с изображениями
            if image_paths and not video_path:
                try:
                    await bot.send_message(chat_id, "❌ Я не работаю с изображениями. Пожалуйста, пришлите ссылку на видео.")
                except Exception:
                    logger.exception("failed to send 'no images' message")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                continue

            # Если video найден — проверяем размер (чтобы избежать "пустых" видео)
            if video_path:
                try:
                    size = os.path.getsize(video_path)
                except Exception:
                    size = 0
                if size < MIN_VIDEO_BYTES:
                    await bot.send_message(chat_id, "❌ Скачанное видео слишком маленькое или отсутствует содержимое — попробуйте другую ссылку.")
                    shutil.rmtree(tmp, ignore_errors=True)
                    continue

                token = uuid.uuid4().hex
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
                ])
                caption_text = "✅ Готово!\nХотите конвертировать только песню?"
                sent_ok = False
                try:
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

                try:
                    await increment_download(user_id)
                except Exception:
                    logger.exception("increment_download failed for %s", user_id)

                audio_cache[token] = {
                    "audio": None,
                    "tmpdir": tmp,
                    "video": video_path,
                    "url": url,
                    "owner": user_id
                }
                asyncio.create_task(cleanup_audio_after_delay(token, AUDIO_TTL_SECONDS))
                continue

            # ничего не найдено
            await bot.send_message(chat_id, "❌ Не удалось скачать видео с этой ссылки.")
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

# -------------------- Callback: получение аудио --------------------
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

    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")
    video_path = info.get("video")
    url = info.get("url")

    await cq.answer()

    if audio_path and os.path.exists(audio_path):
        try:
            await bot.send_chat_action(cq.from_user.id, "upload_audio")
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path), title="Аудио из видео")
        except Exception:
            await cq.answer("Ошибка при отправке аудио.", show_alert=True)
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

    if video_path and os.path.exists(video_path):
        audio_path_new = os.path.join(tmpdir, "audio.mp3")
        await bot.send_chat_action(cq.from_user.id, "record_audio")
        success = await extract_audio_ffmpeg(video_path, audio_path_new)
        if not success:
            await cq.answer("Не удалось извлечь аудио из видео.", show_alert=True)
            try:
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            audio_cache.pop(token, None)
            return
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

    if url:
        new_tmp = tempfile.mkdtemp()
        try:
            await cq.answer("Аудио отсутствует — пробую повторно скачать видео...", show_alert=True)
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, new_tmp))
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

    await cq.answer("Аудио устарело или недоступно — пришлите ссылку ещё раз.", show_alert=True)
    audio_cache.pop(token, None)

# -------------------- Команды: /start, /about, /premium, /profile --------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    info = ("🔥TikGram_bot\n\nОтправь ссылку на видео (YouTube, TikTok, Pinterest и т. д.) — "
            "бот скачает и пришлёт видео.\n\nКоманды:\n/about — О боте\n/premium — Информация о вашем премиуме\n/profile — Мой профиль")
    if USE_COOKIES:
        info += "\n\n(Используется cookies.txt для авторизованных пинов)"
    await m.answer(info)

@dp.message(Command("about"))
async def about_handler(m: Message):
    info = ("🤖 Этот бот скачивает видео по ссылкам (YouTube, TikTok, Pinterest и др.).\n"
            "После отправки видео можно запросить аудио через кнопку «Получить песню 🎵».\n"
            "Требования: ffmpeg в PATH, yt-dlp и requests/aiosqlite установлены.")
    await m.answer(info)

@dp.message(Command("premium"))
async def premium_handler(m: Message):
    await add_user(m.from_user.id)
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Не найден профиль.")
        return
    premium = user[1] or "обычный"
    expires = user[6] or "—"
    stars = user[2] or 0
    points = user[3] or 0
    text = (f"💎 Уровень премиума: {premium}\n"
            f"⏳ Действует до: {expires}\n"
            f"⭐ Звёзды: {stars}\n"
            f"🔹 Очки: {points}\n\nЕсли хотите купить премиум — свяжитесь с админом.")
    await m.answer(text)

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Профиль: не найден")
        return
    await m.answer(f"👤 Профиль\n💎 {user[1]}\n⭐ Звёзды: {user[2]}\n🔹 Очки: {user[3]}")

# -------------------- Обработка ссылок: отклоняем явные изображения --------------------
def looks_like_image_url(url: str) -> bool:
    lower = url.lower()
    # explicit image extension
    if any(lower.endswith(ext) for ext in IMAGE_EXTS):
        return True
    # TikTok photo pattern
    if "tiktok.com" in lower and "/photo/" in lower:
        return True
    # Pinterest pins often are images; treat pinterest as image-only unless yt-dlp yields video later,
    # but to follow your request, we reject pinterest links as images in pre-check
    if "pinterest" in lower or "pin.it" in lower:
        return True
    return False

@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    url = m.text.strip()
    await add_user(user_id)

    # Reject obvious image links immediately
    if looks_like_image_url(url):
        await m.answer("❌ Я не работаю с изображениями. Пожалуйста, пришлите ссылку на видео.")
        return

    # check download limit
    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return

    await download_queue.put((m.chat.id, user_id, url))
    await m.answer("📥 Добавлено в очередь на скачивание...")

# -------------------- Админ: скрыта от пользователей (только ADMIN_ID) --------------------
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "🛠 Админ панель:\n"
        "/stats — Статистика\n"
        "/give_gold ID — Выдать Золотой\n"
        "/give_diamond ID — Выдать Алмазный\n"
        "/add_stars ID сумма — Начислить звёзды\n"
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
        total_premium = sum(v for k,v in premium_counts.items() if k in ("золотой", "алмазный"))
        async with db.execute("SELECT id, points, stars FROM users ORDER BY points DESC LIMIT 10") as cur:
            top = await cur.fetchall()
    text = f"📊 Статистика\nВсего премиум-пользователей (золотой/алмазный): {total_premium}\n\n🏆 Топ-10 по очкам:\n"
    if not top:
        text += "Пока нет игроков."
    else:
        for i, row in enumerate(top, start=1):
            uid, pts, stars = row
            text += f"{i}. {uid} — {pts} очков, {stars or 0}⭐\n"
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

@dp.message(F.text.startswith("/add_stars"))
async def add_stars_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        parts = m.text.split()
        uid = int(parts[1]); amount = int(parts[2])
        await add_stars(uid, amount)
        await m.answer(f"✅ Начислено {amount}⭐ пользователю {uid}")
    except Exception:
        await m.answer("❌ Неверный формат. /add_stars ID сумма")

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