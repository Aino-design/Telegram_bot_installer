# main.py — финальная версия: видео + отдельная команда для изображений (TikTok photo / Pinterest),
# "Очки" (points) с фармингом каждые 20 часов, покупка премиума очками или звёздами,
# /stats показывает количество премиум-пользователей и топ-10 по очкам.
# Внимание: перед запуском убедитесь, что в requirements.txt есть:
# aiogram, aiosqlite, yt-dlp, requests
# И ffmpeg доступен в PATH (для извлечения аудио).
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

TOKEN = os.getenv("TOKEN") or "ТОКЕН_БОТА"   # <- замените
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

# премиум / цены (с использованием тех же чисел что и раньше)
GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)      # цена в звёздах или очках
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

# TTL временных файлов
AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)

# cookies.txt
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()

# кеш для аудио
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")

# -------------------- БД: init и помощники --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # добавляем колонки points (очки), image_mode (флаг ожидания изображений), last_farm (для cooldown)
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

async def set_image_mode(uid: int, mode: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET image_mode=? WHERE id=?", (mode, uid))
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
    premium, downloads = user[1], user[4]
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
    downloads_today = user[4] or 0
    limit = LIMITS.get(premium, 4)
    if limit is None:
        return None, None, premium
    remaining = max(limit - downloads_today, 0)
    return remaining, limit, premium

# -------------------- Вспомогательные функции для веба --------------------
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
    # JSON-like search
    for key in ('displayUrl','originCover','cover','downloadAddr','originImage','poster','og:image'):
        for m in re.finditer(rf'"{key}"\s*:\s*"(https?://[^"]+)"', html, flags=re.IGNORECASE):
            urls.append(m.group(1))
    # OG meta fallback
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        urls.append(m.group(1))
    # dedupe preserve order
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
    # Try extracting TikTok SIGI_STATE JSON block for more images
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
    img_urls = list(dict.fromkeys(img_urls))  # dedupe keep order
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

# -------------------- yt-dlp core + safe wrapper --------------------
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
    # Resolve redirects for short urls
    if any(s in url for s in ("vm.tiktok.com", "vt.tiktok.com", "https://vm.", "https://vt.")):
        url = resolve_redirect(url)

    # --- TikTok photo: DO NOT call yt-dlp; parse HTML and save up to 10 images ---
    if "tiktok.com" in url and "/photo/" in url:
        logger.info("Detected TikTok photo post — parsing page for images")
        url = sanitize_tiktok_photo_url(url)
        saved = fetch_and_save_images_page(url, folder, limit=10)
        if saved:
            logger.info("TikTok photos saved count=%d", len(saved))
            return
        # try SIGI_STATE extractions
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
                ext = "jpg"
                ext_m = re.search(r'\.([a-zA-Z0-9]+)(?:\?|$)', img_url)
                if ext_m:
                    ext = ext_m.group(1)
                dest = os.path.join(folder, f"photo_{i}.{ext}")
                download_file_sync(img_url, dest)
            saved_local = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(("jpg","jpeg","png","webp","gif"))]
            if saved_local:
                logger.info("TikTok photos saved via JSON/regex: %d", len(saved_local))
                return
        except Exception:
            pass
        raise Exception("Не удалось найти/скачать фото TikTok (формат страницы нестандартный)")

    # --- Pinterest: try ytdlp first (pins can be videos), else HTML images ---
    if "pinterest" in url or "pin.it" in url:
        try:
            filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
            logger.info("Pinterest downloaded by yt-dlp: %s", filename)
            return
        except Exception as e:
            logger.warning("Pinterest yt-dlp failed: %s — trying HTML fallback", e)
            saved = fetch_and_save_images_page(url, folder, limit=10)
            if saved:
                logger.info("Pinterest HTML fallback saved images: %d", len(saved))
                return
            raise

    # --- General: try ytdlp then fallback to HTML images ---
    try:
        filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
        logger.info("yt-dlp downloaded: %s", filename)
        return
    except Exception as e:
        logger.warning("yt-dlp failed: %s — trying HTML fallback", e)
        saved = fetch_and_save_images_page(url, folder, limit=10)
        if saved:
            logger.info("HTML fallback saved images: %d", len(saved))
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
async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token: Optional[str] = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            # выполняем безопасную загрузку в executor
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            # найти видео/изображения
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

            # если изображения (и не видео) — отправляем (до 10)
            if image_paths and not video_path:
                try:
                    images_to_send = sorted(image_paths)[:10]
                    # Попытка отправить media_group
                    try:
                        media = [types.InputMediaPhoto(media=FSInputFile(path)) for path in images_to_send]
                        await bot.send_media_group(chat_id, media=media)
                    except Exception:
                        # fallback: отправка по одному
                        for path in images_to_send:
                            await bot.send_photo(chat_id, FSInputFile(path))
                    await bot.send_message(chat_id, f"✅ Готово! (изображения: {len(images_to_send)})")
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

            # если видео — отправляем + кладём в кеш для получения аудио
            if video_path:
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

# -------------------- Команды: базовые --------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    info = "🔥TikGram_bot\n\nОтправь ссылку на видео (YouTube, TikTok, Pinterest и т. д.) — бот скачает видео.\n" \
           "Если хочешь скачивать ИЗОБРАЖЕНИЯ (TikTok photo / Pinterest), сперва введи команду /images, " \
           "а затем отправь ссылку на изображение. Иначе бот предложит сначала использовать /images."
    if USE_COOKIES:
        info += "\n(Используется cookies.txt для авторизованных пинов)"
    await m.answer(info)

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔥TikGram_bot\n\n/convert — отправьте ссылку на видео\n/images — режим для отправки ссылок на изображения (TikTok photo, Pinterest)\n/farm — фарм очков (раз в 20 часов)\n/stats — статистика и топ по очкам")

@dp.message(Command("about"))
async def about_handler(m: Message):
    info = "🤖 Бот скачивает видео и изображения (отдельный режим). Может вырезать аудио из видео.\n" \
           "Платежи: звёзды ⭐ и очки (points)."
    await m.answer(info)

@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔗 Отправьте ссылку на видео и я обработаю его и пришлю вам!")

# -------------------- Команда /images (включить режим изображений) --------------------
@dp.message(Command("images"))
async def images_mode_on(m: Message):
    await add_user(m.from_user.id)
    await set_image_mode(m.from_user.id, 1)
    await m.answer("🖼 Режим изображений включён. Теперь пришлите ссылку на TikTok photo или Pinterest (одно сообщение). После обработки режим выключится автоматически.")

# -------------------- Команды для покупки премиума очками или звёздами --------------------
@dp.message(Command("buy_gold_points"))
async def buy_gold_points(m: Message):
    await add_user(m.from_user.id)
    uid = m.from_user.id
    ok = await use_points(uid, GOLD_PRICE)
    if ok:
        await set_premium(uid, "золотой", GOLD_DAYS)
        await m.answer(f"✅ Вы купили Золотой премиум за {GOLD_PRICE} очков на {GOLD_DAYS} дней.")
    else:
        await m.answer(f"❌ У вас недостаточно очков. Цена: {GOLD_PRICE} очков.")

@dp.message(Command("buy_diamond_points"))
async def buy_diamond_points(m: Message):
    await add_user(m.from_user.id)
    uid = m.from_user.id
    ok = await use_points(uid, DIAMOND_PRICE)
    if ok:
        await set_premium(uid, "алмазный", DIAMOND_DAYS)
        await m.answer(f"✅ Вы купили Алмазный премиум за {DIAMOND_PRICE} очков на {DIAMOND_DAYS} дней.")
    else:
        await m.answer(f"❌ У вас недостаточно очков. Цена: {DIAMOND_PRICE} очков.")

@dp.message(Command("buy_gold_stars"))
async def buy_gold_stars(m: Message):
    await add_user(m.from_user.id)
    uid = m.from_user.id
    ok = await use_stars(uid, GOLD_PRICE)
    if ok:
        await set_premium(uid, "золотой", GOLD_DAYS)
        await m.answer(f"✅ Вы купили Золотой премиум за {GOLD_PRICE}⭐ на {GOLD_DAYS} дней.")
    else:
        await m.answer(f"❌ У вас недостаточно звёзд. Цена: {GOLD_PRICE}⭐.")

@dp.message(Command("buy_diamond_stars"))
async def buy_diamond_stars(m: Message):
    await add_user(m.from_user.id)
    uid = m.from_user.id
    ok = await use_stars(uid, DIAMOND_PRICE)
    if ok:
        await set_premium(uid, "алмазный", DIAMOND_DAYS)
        await m.answer(f"✅ Вы купили Алмазный премиум за {DIAMOND_PRICE}⭐ на {DIAMOND_DAYS} дней.")
    else:
        await m.answer(f"❌ У вас недостаточно звёзд. Цена: {DIAMOND_PRICE}⭐.")

# -------------------- Фарм очков (каждые 20 часов) --------------------
@dp.message(Command("farm"))
async def farm_points(m: Message):
    await add_user(m.from_user.id)
    uid = m.from_user.id
    user = await get_user(uid)
    last_farm_iso = user[8] if user else None
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
    # give random 10-35
    amount = random.randint(10, 35)
    await add_points(uid, amount)
    await set_last_farm(uid, now.isoformat())
    await m.answer(f"🎉 Вы получили {amount} очков! (Можно фармить снова через 20 часов)")

# -------------------- Команда /stats — premium count + top-10 by points --------------------
@dp.message(Command("stats"))
async def stats_handler(m: Message):
    await add_user(m.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        # premium counts
        async with db.execute("SELECT premium, COUNT(*) FROM users GROUP BY premium") as cur:
            rows = await cur.fetchall()
        premium_counts = {r[0]: r[1] for r in rows}
        total_premium = sum(v for k,v in premium_counts.items() if k in ("золотой", "алмазный"))
        # top-10 by points
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

# -------------------- Обработка входящих ссылок --------------------
@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    url = m.text.strip()
    await add_user(user_id)

    # check if link looks like image-only (TikTok photo or Pinterest pin)
    is_tiktok_photo = ("tiktok.com" in url and "/photo/" in url) or ("/photo/" in url and "tiktok" in url)
    is_pinterest = "pinterest" in url or "pin.it" in url

    # check user's image_mode
    user = await get_user(user_id)
    image_mode = user[7] if user else 0

    # If link is image-type but user hasn't activated /images, instruct them
    if (is_tiktok_photo or is_pinterest) and not image_mode:
        await m.answer("🖼 Похоже, это ссылка на изображение (TikTok photo или Pinterest). Чтобы скачивать изображения, сперва введите команду /images и затем отправьте ссылку. Если вы хотите скачать видео — пришлите ссылку на видео.")
        return

    # If image_mode active — handle as image (and then reset image_mode to 0)
    if image_mode:
        await set_image_mode(user_id, 0)  # one-shot mode
        # Process in executor: safe_download_video handles image pages and videos.
        await download_queue.put((m.chat.id, user_id, url))
        await m.answer("📥 Добавлено в очередь на скачивание изображения/видео... (режим изображений).")
        return

    # Normal video flow
    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return
    await download_queue.put((m.chat.id, user_id, url))
    await m.answer("📥 Добавлено в очередь...")

# -------------------- Админ и покупка (старые команды) --------------------
@dp.message(Command("buy_gold"))
async def buy_gold_invoice(m: Message):
    prices = [LabeledPrice(label=f"Золотой ({GOLD_DAYS} дней)", amount=GOLD_PRICE)]
    # provider_token пустой — если у вас есть провайдер, заполните
    try:
        await bot.send_invoice(m.chat.id, title="Золотой премиум", description="Покупка премиума",
                               payload=f"gold:{m.from_user.id}", provider_token="", currency="XTR", prices=prices,
                               start_parameter="premium")
    except Exception:
        await m.answer("⚠️ Оплата через invoice не настроена на этом сервере. Используйте /buy_gold_stars или /buy_gold_points.")

@dp.message(Command("buy_diamond"))
async def buy_diamond_invoice(m: Message):
    prices = [LabeledPrice(label=f"Алмазный ({DIAMOND_DAYS} дней)", amount=DIAMOND_PRICE)]
    try:
        await bot.send_invoice(m.chat.id, title="Алмазный премиум", description="Покупка премиума",
                               payload=f"diamond:{m.from_user.id}", provider_token="", currency="XTR", prices=prices,
                               start_parameter="premium")
    except Exception:
        await m.answer("⚠️ Оплата через invoice не настроена на этом сервере. Используйте /buy_diamond_stars или /buy_diamond_points.")

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

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
        f"🔹 Очки: {user[3]}\n"
    )

# -------------------- Админ команды --------------------
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "🛠 Админ панель:\n"
        "/stats — Статистика и топ\n"
        "/give_gold ID — Выдать Золотой\n"
        "/give_diamond ID — Выдать Алмазный\n"
        "/add_stars ID сумма — Начислить звёзды\n/give_points ID сумма — Выдать очки"
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