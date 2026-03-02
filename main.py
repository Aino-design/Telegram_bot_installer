# main.py — полный рабочий бот (обновлённый)
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
from typing import Dict, Any, Optional, List, Tuple
from functools import partial

import requests
import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

# -------------------- Лог и настройки --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "REPLACE_WITH_YOUR_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

# премиум (в очках) и лимиты
GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

# временные параметры
AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")
MIN_VIDEO_BYTES = 50_000  # минимальный размер принимаемого видео (байты)

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}

# ---------------- Проверка ffmpeg / ffprobe ----------------
HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None
logger.info("ffmpeg available: %s, ffprobe available: %s", HAS_FFMPEG, HAS_FFPROBE)

# -------------------- DB helpers --------------------
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

# -------------------- Web helpers --------------------
def resolve_redirect(url: str, timeout: int = 10) -> str:
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=timeout, allow_redirects=True)
        if r.status_code in (200,301,302):
            return r.url
    except Exception:
        pass
    return url

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

# -------------------- yt-dlp wrapper (improved) --------------------
def download_with_ytdlp(url: str, folder: str, cookiefile: Optional[str] = None) -> str:
    """
    Try to download video:
      1) bestvideo+bestaudio/best (merge if ffmpeg present)
      2) fallback: best (single file)
    Records title.txt
    Returns path to saved file.
    """
    def _run_with_opts(opts):
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") if isinstance(info, dict) else ""
            try:
                with open(os.path.join(folder, "title.txt"), "w", encoding="utf-8") as f:
                    f.write(title or "")
            except Exception:
                pass
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

    base_opts = {
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "quiet": True, "no_warnings": True, "ignoreerrors": False,
        "noplaylist": True, "http_headers": {"User-Agent":"Mozilla/5.0"},
        "allow_unplayable_formats": True, "merge_output_format": "mp4", "no_color": True,
    }
    if cookiefile and os.path.exists(cookiefile):
        base_opts["cookiefile"] = cookiefile

    try:
        return _run_with_opts(base_opts)
    except Exception as e:
        logger.info("yt-dlp attempt (bestvideo+bestaudio) failed: %s — trying fallback 'best'", e)

    fallback_opts = base_opts.copy()
    fallback_opts["format"] = "best"
    if "cookiefile" in base_opts:
        fallback_opts["cookiefile"] = base_opts["cookiefile"]
    with YoutubeDL(fallback_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") if isinstance(info, dict) else ""
        try:
            with open(os.path.join(folder, "title.txt"), "w", encoding="utf-8") as f:
                f.write(title or "")
        except Exception:
            pass
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
            raise Exception("yt-dlp fallback didn't save any file")
        return sorted(files, key=os.path.getmtime, reverse=True)[0]

def safe_download_video(url: str, folder: str) -> None:
    logger.info("safe_download_video start url=%s", url)
    url = resolve_redirect(url)
    # try yt-dlp
    try:
        filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
        logger.info("download_with_ytdlp saved %s", filename)
        return
    except Exception as e:
        logger.info("yt-dlp failed: %s", e)

    # html scraping for direct video urls
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

    # as last resort save images (caller will reject)
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

# -------------------- ffprobe helpers --------------------
async def has_video_stream(path: str) -> bool:
    if not HAS_FFPROBE:
        lower = path.lower()
        return any(lower.endswith(ext) for ext in (".mp4", ".mkv", ".mov", ".ts"))
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
    if not HAS_FFPROBE:
        lower = path.lower()
        return any(lower.endswith(ext) for ext in (".mp3", ".m4a", ".webm", ".mp4", ".aac"))
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

# -------------------- ffmpeg audio extraction --------------------
async def extract_audio_ffmpeg(video_path: str, output_audio_path: str) -> bool:
    if not HAS_FFMPEG:
        logger.info("ffmpeg not available — cannot extract audio with ffmpeg")
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "mp3", "-ar", "44100", "-ac", "2",
            output_audio_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
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

# -------------------- Download worker --------------------
async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            # collect files sorted by size descending
            candidates = []
            for f in os.listdir(tmp):
                full = os.path.join(tmp, f)
                if os.path.isfile(full):
                    candidates.append(full)
            candidates_sorted = sorted(candidates, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)

            # find first file with video stream
            chosen_video = None
            for p in candidates_sorted:
                if await has_video_stream(p):
                    chosen_video = p
                    break

            # gather images (if any)
            image_paths = [p for p in candidates_sorted if p.lower().endswith(IMAGE_EXTS)]

            if image_paths and not chosen_video:
                await bot.send_message(chat_id, "❌ Я не работаю с изображениями. Пожалуйста, пришлите ссылку на видео.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            if not chosen_video:
                await bot.send_message(chat_id, "❌ Не удалось скачать видео с этой ссылки.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            size = os.path.getsize(chosen_video) if os.path.exists(chosen_video) else 0
            if size < MIN_VIDEO_BYTES:
                # try strict ytdlp re-download
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                    tmp = tempfile.mkdtemp()
                    filename = download_with_ytdlp(url, tmp, cookiefile=COOKIES_FILE if USE_COOKIES else None)
                    if await has_video_stream(filename):
                        chosen_video = filename
                        size = os.path.getsize(chosen_video)
                except Exception:
                    pass

            if size < MIN_VIDEO_BYTES:
                await bot.send_message(chat_id, "❌ Скачанное видео слишком маленькое или не содержит контента — попробуйте другую ссылку.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # read title.txt if present
            title = None
            title_file = os.path.join(tmp, "title.txt")
            if os.path.exists(title_file):
                try:
                    with open(title_file, "r", encoding="utf-8") as f:
                        title = f.read().strip()
                except Exception:
                    title = None
            if not title:
                title = os.path.splitext(os.path.basename(chosen_video))[0]

            # send video with audio button
            token = uuid.uuid4().hex
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
            ])
            caption = "✅ Готово!\nНажмите кнопку ниже, чтобы получить аудио из видео."
            sent_ok = False
            try:
                await bot.send_video(chat_id, FSInputFile(chosen_video), caption=caption, reply_markup=kb)
                sent_ok = True
            except Exception:
                logger.exception("send_video failed; trying send_document")
                try:
                    await bot.send_document(chat_id, FSInputFile(chosen_video), caption=caption, reply_markup=kb)
                    sent_ok = True
                except Exception as e:
                    logger.exception("send_document failed: %s", e)
                    await bot.send_message(chat_id, f"❌ Ошибка отправки видео: {e}")
                    sent_ok = False

            if not sent_ok:
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            try:
                await increment_download(user_id)
            except Exception:
                logger.exception("increment_download failed for %s", user_id)

            # try to pre-extract audio (best-effort)
            audio_path = os.path.join(tmp, "audio.mp3")
            audio_ok = False
            try:
                audio_ok = await extract_audio_ffmpeg(chosen_video, audio_path)
            except Exception:
                audio_ok = False

            # store in cache
            audio_cache[token] = {
                "audio": audio_path if audio_ok and os.path.exists(audio_path) else None,
                "tmpdir": tmp,
                "video": chosen_video,
                "url": url,
                "owner": user_id,
                "title": title or "Аудио из видео"
            }
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

    await cq.answer()
    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")
    video_path = info.get("video")
    url = info.get("url")
    title = info.get("title") or "Аудио из видео"

    # if we already have audio file
    if audio_path and os.path.exists(audio_path):
        try:
            await bot.send_chat_action(cq.from_user.id, "upload_audio")
            await bot.send_audio(cq.from_user.id, FSInputFile(audio_path), title=title)
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

    # attempt to extract now from saved video
    if video_path and os.path.exists(video_path) and HAS_FFMPEG:
        audio_now = os.path.join(tmpdir, "audio_on_demand.mp3")
        await bot.send_chat_action(cq.from_user.id, "record_audio")
        ok = await extract_audio_ffmpeg(video_path, audio_now)
        if ok and os.path.exists(audio_now):
            try:
                await bot.send_audio(cq.from_user.id, FSInputFile(audio_now), title=title)
            except Exception:
                await cq.answer("Ошибка при отправке аудио.", show_alert=True)
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

    # if can't extract here, try re-download and extract
    new_tmp = tempfile.mkdtemp()
    try:
        await cq.answer("Попытка повторного скачивания для извлечения аудио...", show_alert=True)
        await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, new_tmp))
        new_video = None
        for f in os.listdir(new_tmp):
            full = os.path.join(new_tmp, f)
            if await has_video_stream(full):
                new_video = full
                break
        if new_video and HAS_FFMPEG:
            audio_retry = os.path.join(new_tmp, "audio_retry.mp3")
            ok2 = await extract_audio_ffmpeg(new_video, audio_retry)
            if ok2 and os.path.exists(audio_retry):
                try:
                    await bot.send_audio(cq.from_user.id, FSInputFile(audio_retry), title=title)
                except Exception:
                    await cq.answer("Ошибка при отправке аудио.", show_alert=True)
                finally:
                    try:
                        if os.path.exists(audio_retry):
                            os.remove(audio_retry)
                        shutil.rmtree(new_tmp, ignore_errors=True)
                    except Exception:
                        pass
                audio_cache.pop(token, None)
                return
        # if couldn't get video but obtained audio-only file(s), try to find an audio file
        # send audio-only if present (best-effort)
        for f in os.listdir(new_tmp):
            if f.lower().endswith((".mp3", ".m4a", ".webm", ".aac")):
                try:
                    await bot.send_audio(cq.from_user.id, FSInputFile(os.path.join(new_tmp, f)), title=title)
                    audio_cache.pop(token, None)
                    shutil.rmtree(new_tmp, ignore_errors=True)
                    return
                except Exception:
                    pass
        await cq.answer("Не удалось извлечь аудио.", show_alert=True)
    except Exception:
        await cq.answer("Ошибка при повторном скачивании/конвертации.", show_alert=True)
    finally:
        try:
            shutil.rmtree(new_tmp, ignore_errors=True)
        except Exception:
            pass
    audio_cache.pop(token, None)

# -------------------- Commands: /start, /premium, /profile, /farm --------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    txt = (
        "🔥 TikGram_bot\n\n"
        "Я скачиваю видео по ссылкам (YouTube / Shorts, TikTok, Pinterest и др.) и отправляю их вам.\n"
        "После отправки видео появится кнопка «Получить песню 🎵» — нажмите, чтобы получить MP3.\n\n"
        "Команды:\n"
        "/premium — Информация о премиуме и кнопки покупки (за очки)\n"
        "/profile — Профиль (очки, уровень, скачиваний осталось)\n"
        "/farm — Фарм очков (раз в 20 часов, 10–35 очков)\n\n"
        "Просто пришлите ссылку на видео — бот сам скачает и пришлёт файл.\n\n"
    )
    await m.answer(txt)

@dp.message(Command("premium"))
async def premium_handler(m: Message):
    await add_user(m.from_user.id)
    user = await get_user(m.from_user.id)
    premium = user[1] if user else "обычный"
    expires = user[5] or "—"
    points = (user[2] or 0) if user else 0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium, COUNT(*) FROM users GROUP BY premium") as cur:
            rows = await cur.fetchall()
    premium_counts = {r[0]: r[1] for r in rows} if rows else {}
    total_premium = sum(v for k,v in premium_counts.items() if k in ("золотой","алмазный"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Купить GOLD ({GOLD_PRICE} очков)", callback_data="buy_gold_points")],
        [InlineKeyboardButton(text=f"Купить DIAMOND ({DIAMOND_PRICE} очков)", callback_data="buy_diamond_points")]
    ])
    text = (
        f"💎 Уровень премиума: {premium}\n"
        f"⏳ Действует до: {expires}\n"
        f"🔹 Очки: {points}\n"
        f"📦 Всего премиум-пользователей (gold/diamond): {total_premium}\n\n"
        "Что дают уровни:\n"
        "• Золотой — лимит загрузок 10 в день (вместо 4), приоритет очереди, срок: "
        f"{GOLD_DAYS} дней (стоимость {GOLD_PRICE} очков).\n"
        "• Алмазный — безлимит скачиваний, приоритет, срок: "
        f"{DIAMOND_DAYS} дней (стоимость {DIAMOND_PRICE} очков).\n\n"
        "Нажмите кнопку, чтобы купить премиум за очки (если у вас достаточно очков)."
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
        await cq.answer(f"✅ Вы купили {level} на {days} дней за {price} очков.", show_alert=True)
    else:
        await cq.answer(f"❌ У вас недостаточно очков. Цена: {price} очков.", show_alert=True)

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    await add_user(m.from_user.id)
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Профиль: не найден")
        return
    remaining, limit, premium = await get_remaining_downloads(m.from_user.id)
    if remaining is None:
        downloads_text = "♾ Безлимит"
    else:
        downloads_text = f"{remaining}/{limit}"
    points = user[2] or 0
    await m.answer(f"👤 Профиль\n💎 {premium}\n🔹 Очки: {points}\n📥 Скачиваний осталось: {downloads_text}")

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

# -------------------- Link handler --------------------
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

# -------------------- Admin (hidden) --------------------
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
        async with db.execute("SELECT id, points FROM users ORDER BY points DESC LIMIT 10") as cur:
            top = await cur.fetchall()
    premium_counts = {r[0]: r[1] for r in rows} if rows else {}
    total_premium = sum(v for k,v in premium_counts.items() if k in ("золотой","алмазный"))
    text = f"📊 Статистика\nВсего премиум-пользователей (gold/diamond): {total_premium}\n\n🏆 Топ-10 по очкам:\n"
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