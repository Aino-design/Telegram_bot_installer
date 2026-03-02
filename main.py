# main.py — обновлённый: личные чаты — автоскачивание, группы — кнопка "Скачать видео ▶️",
# лимиты применяются к пользователю, который инициирует скачивание.
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

# -------------------- Config & logging --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "REPLACE_WITH_YOUR_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")
AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".aac", ".opus")
MIN_VIDEO_BYTES = 20_000

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}
pending_group_downloads: Dict[str, Dict[str, Any]] = {}  # token -> {link, chat_id, original_sender, created_at}
BOT_USERNAME: Optional[str] = None

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None
logger.info("ffmpeg: %s, ffprobe: %s", HAS_FFMPEG, HAS_FFPROBE)

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
def extract_first_link_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'(https?://[^\s\)\]\}\,]+)', text)
    if m:
        return m.group(1).rstrip('.,)')
    return None

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

# -------------------- yt-dlp wrapper (same robust approach) --------------------
def download_with_ytdlp(url: str, folder: str, cookiefile: Optional[str] = None) -> str:
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
        logger.info("yt-dlp attempt failed: %s — trying fallback", e)
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

# minimal ffmpeg/ffprobe helpers (merging/extraction)
async def has_video_stream(path: str) -> bool:
    if not shutil.which("ffprobe"):
        lower = path.lower()
        return any(lower.endswith(ext) for ext in (".mp4", ".mkv", ".mov", ".ts", ".webm"))
    try:
        proc = await asyncio.create_subprocess_exec("ffprobe","-v","error","-select_streams","v","-show_entries","stream=index","-of","csv=p=0", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return bool(out and out.strip())
    except Exception:
        return False

async def has_audio_stream(path: str) -> bool:
    if not shutil.which("ffprobe"):
        lower = path.lower()
        return any(lower.endswith(ext) for ext in AUDIO_EXTS + (".mp4", ".webm"))
    try:
        proc = await asyncio.create_subprocess_exec("ffprobe","-v","error","-select_streams","a","-show_entries","stream=index","-of","csv=p=0", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return bool(out and out.strip())
    except Exception:
        return False

async def extract_audio_ffmpeg(video_path: str, output_audio_path: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        proc = await asyncio.create_subprocess_exec("ffmpeg","-y","-i",video_path,"-vn","-acodec","mp3","-ar","44100","-ac","2",output_audio_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return os.path.exists(output_audio_path)
    except Exception:
        return False

async def merge_video_and_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        # try stream copy
        proc = await asyncio.create_subprocess_exec("ffmpeg","-y","-i",video_path,"-i",audio_path,"-c","copy",output_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
    except Exception:
        pass
    try:
        # fallback transcode
        proc2 = await asyncio.create_subprocess_exec(
            "ffmpeg","-y","-i",video_path,"-i",audio_path,"-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k",output_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc2.communicate()
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception:
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
        pass
    audio_cache.pop(token, None)

# -------------------- Worker (unchanged core) --------------------
async def safe_download_video(url: str, folder: str) -> None:
    # uses download_with_ytdlp to save files into folder; fallback scraping omitted for brevity
    try:
        download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
    except Exception as e:
        logger.info("safe_download_video error: %s", e)
        # try simple HTTP fetch (not robust) - left minimal
        try:
            r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
            open(os.path.join(folder, "page.html"), "w", encoding="utf-8").write(r.text)
        except Exception:
            pass

async def download_worker():
    while True:
        chat_id, requester_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            files = [os.path.join(tmp, f) for f in os.listdir(tmp) if os.path.isfile(os.path.join(tmp,f))]
            files_sorted = sorted(files, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)

            chosen_video = None
            for p in files_sorted:
                if await has_video_stream(p):
                    chosen_video = p
                    break

            images = [p for p in files_sorted if p.lower().endswith(IMAGE_EXTS)]
            if images and not chosen_video:
                await bot.send_message(chat_id, "❌ Я не работаю с изображениями. Пришлите, пожалуйста, ссылку на видео.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            if not chosen_video:
                await bot.send_message(chat_id, "❌ Не удалось скачать видео с этой ссылки.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # if chosen_video has no audio -> try to find audio and merge
            if not await has_audio_stream(chosen_video):
                # find audio candidate
                audio_candidate = None
                for f in files_sorted:
                    if f == chosen_video: continue
                    if await has_audio_stream(f) and not await has_video_stream(f):
                        audio_candidate = f
                        break
                if audio_candidate and shutil.which("ffmpeg"):
                    merged = os.path.join(tmp, "merged_" + uuid.uuid4().hex + ".mp4")
                    ok = await merge_video_and_audio(chosen_video, audio_candidate, merged)
                    if ok:
                        chosen_video = merged
                    else:
                        # try redownload single-file
                        try:
                            shutil.rmtree(tmp, ignore_errors=True)
                            tmp = tempfile.mkdtemp()
                            filename = download_with_ytdlp(url, tmp, cookiefile=COOKIES_FILE if USE_COOKIES else None)
                            if await has_video_stream(filename):
                                chosen_video = filename
                        except Exception:
                            pass

            if not chosen_video or not os.path.exists(chosen_video):
                await bot.send_message(chat_id, "❌ Не удалось получить корректное видео. Попробуйте другую ссылку.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            if os.path.getsize(chosen_video) < MIN_VIDEO_BYTES:
                await bot.send_message(chat_id, "❌ Полученное видео слишком маленькое — попробуйте другую ссылку.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # send video and attach audio-button
            token = uuid.uuid4().hex
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]])
            caption = "✅ Готово! Нажмите кнопку, чтобы получить MP3 из этого видео."
            sent_ok = False
            try:
                await bot.send_video(chat_id, FSInputFile(chosen_video), caption=caption, reply_markup=kb)
                sent_ok = True
            except Exception:
                try:
                    await bot.send_document(chat_id, FSInputFile(chosen_video), caption=caption, reply_markup=kb)
                    sent_ok = True
                except Exception as e:
                    await bot.send_message(chat_id, f"❌ Ошибка отправки видео: {e}")
                    sent_ok = False

            if not sent_ok:
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # increment downloads for requester
            try:
                await increment_download(requester_id)
            except Exception:
                logger.exception("increment_download failed")

            # try pre-extract audio
            audio_path = os.path.join(tmp, "audio.mp3")
            audio_ok = False
            if shutil.which("ffmpeg"):
                audio_ok = await extract_audio_ffmpeg(chosen_video, audio_path)

            audio_cache[token] = {"audio": audio_path if audio_ok and os.path.exists(audio_path) else None,
                                  "tmpdir": tmp, "video": chosen_video, "url": url, "owner": requester_id}
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

# -------------------- Callbacks --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("dl_video:"))
async def cb_download_button(cq: CallbackQuery):
    token = cq.data.split(":",1)[1]
    entry = pending_group_downloads.get(token)
    if not entry:
        await cq.answer("⚠️ Заявка устарела или недоступна.", show_alert=True)
        return
    link = entry.get("link")
    chat_id = entry.get("chat_id")
    # requester is the user who clicked the button (we'll charge/check them)
    requester_id = cq.from_user.id
    # check limit
    if not await can_download(requester_id):
        await cq.answer("❌ У вас исчерпан лимит загрузок для вашего уровня.", show_alert=True)
        return
    # enqueue with requester_id so downloads count to them
    await download_queue.put((chat_id, requester_id, link))
    await cq.answer("📥 Запрос на скачивание принят — начинаю загрузку.", show_alert=True)
    # remove pending (single-shot)
    pending_group_downloads.pop(token, None)

@dp.callback_query(lambda c: c.data and c.data.startswith("get_audio:"))
async def cb_get_audio(cq: CallbackQuery):
    token = cq.data.split(":",1)[1]
    info = audio_cache.get(token)
    if not info:
        await cq.answer("⚠️ Аудио устарело или недоступно — попробуйте запросить снова.", show_alert=True)
        return
    owner = info.get("owner")
    if owner and cq.from_user.id != owner and cq.from_user.id != ADMIN_ID:
        await cq.answer("Только тот, кто запросил видео, может получить аудио.", show_alert=True)
        return
    await cq.answer()
    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")
    video = info.get("video")
    title = "Аудио из видео"
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
    # try extract on-demand
    if video and os.path.exists(video) and shutil.which("ffmpeg"):
        audio_now = os.path.join(tmpdir, "audio_now.mp3")
        ok = await extract_audio_ffmpeg(video, audio_now)
        if ok and os.path.exists(audio_now):
            try:
                await bot.send_audio(cq.from_user.id, FSInputFile(audio_now), title=title)
            except Exception:
                await cq.answer("Ошибка при отправке аудио.", show_alert=True)
            finally:
                try:
                    if os.path.exists(audio_now):
                        os.remove(audio_now)
                except Exception:
                    pass
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
                audio_cache.pop(token, None)
            return
    # last resort: try redownload and extract
    new_tmp = tempfile.mkdtemp()
    try:
        await cq.answer("Попытка повторного скачивания и извлечения аудио...", show_alert=True)
        await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, info.get("url"), new_tmp))
        new_video = None
        for f in os.listdir(new_tmp):
            if await has_video_stream(os.path.join(new_tmp,f)):
                new_video = os.path.join(new_tmp,f)
                break
        if new_video and shutil.which("ffmpeg"):
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
                    except Exception:
                        pass
                    shutil.rmtree(new_tmp, ignore_errors=True)
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

# -------------------- Commands --------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    txt = (
        "🔥 TikGram_bot\n\n"
        "Личные чаты: пришлите ссылку — бот скачает и сразу отправит видео + кнопку «Получить песню 🎵».\n"
        "Группы: при вставке ссылки бот отвечает сообщением с кнопкой «Скачать видео ▶️». Нажмите кнопку — и начнётся скачивание (лимит применяется к тому, кто нажал).\n\n"
        "Команды:\n"
        "/convert <link> — скачать видео (в группе используйте /convert <link> если Privacy Mode включён)\n"
        "/premium — информация о премиуме и покупка за очки\n"
        "/profile — профиль (очки, скачиваний осталось)\n"
        "/farm — фарм очков (каждые 20 часов, 10–35 очков)\n\n"
        f"ffmpeg: {HAS_FFMPEG}, ffprobe: {HAS_FFPROBE}"
    )
    await m.answer(txt)

@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    await add_user(m.from_user.id)
    text = m.text or ""
    parts = text.split(maxsplit=1)
    link = None
    if len(parts) >= 2:
        link = extract_first_link_from_text(parts[1])
    if not link:
        if m.chat.type == "private":
            await m.answer("🔗 Пришлите ссылку на видео.")
            return
        else:
            await m.answer("❗ В группе: /convert <ссылка> или упомяните бота рядом с ссылкой.")
            return
    if not await can_download(m.from_user.id):
        await m.answer("❌ Вы исчерпали дневной лимит загрузок.")
        return
    await download_queue.put((m.chat.id, m.from_user.id, link))
    await m.answer("📥 Добавлено в очередь на скачивание...")

# premium/profile/farm (kept minimal)
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
    text = (f"💎 Уровень: {premium}\n⏳ До: {expires}\n🔹 Очки: {points}\n\n"
            "Золотой: лимит 10/day. Алмазный: безлимит.")
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
        await cq.answer(f"✅ Куплено: {level} на {days} дней.", show_alert=True)
    else:
        await cq.answer(f"❌ Недостаточно очков (нужно {price}).", show_alert=True)

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    await add_user(m.from_user.id)
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Профиль: не найден")
        return
    remaining, limit, premium = await get_remaining_downloads(m.from_user.id)
    downloads_text = "♾ Безлимит" if remaining is None else f"{remaining}/{limit}"
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
        if now - last < timedelta(hours=20):
            remain = timedelta(hours=20) - (now - last)
            hours = int(remain.total_seconds() // 3600)
            minutes = int((remain.total_seconds() % 3600) // 60)
            await m.answer(f"⏳ Можно фармить через {hours}ч {minutes}м.")
            return
    amount = random.randint(10,35)
    await add_points(uid, amount)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_farm=? WHERE id=?", (now.isoformat(), uid))
        await db.commit()
    await m.answer(f"🎉 Вы получили {amount} очков!")

# -------------------- Link handler: private auto-download, groups -> show button --------------------
@dp.message()
async def general_message_handler(m: Message):
    text = m.text or m.caption or ""
    if not text:
        return
    link = extract_first_link_from_text(text)
    if not link:
        return

    chat_type = m.chat.type
    # private: enqueue immediately, requester = sender
    if chat_type == "private":
        if not await can_download(m.from_user.id):
            await m.answer("❌ Превышен лимит загрузок.")
            return
        await download_queue.put((m.chat.id, m.from_user.id, link))
        await m.answer("📥 Добавлено в очередь... (личный чат)")
        return

    # group: only show button (do not auto-download)
    if chat_type in ("group", "supergroup"):
        # hint about privacy
        # create token and store pending
        token = uuid.uuid4().hex
        pending_group_downloads[token] = {"link": link, "chat_id": m.chat.id, "original_sender": m.from_user.id, "created_at": datetime.now(timezone.utc).isoformat()}
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Скачать видео ▶️", callback_data=f"dl_video:{token}")]
        ])
        # we reply with text + button; user that presses will be charged/checked
        await m.reply("ℹ️ Чтобы скачать это видео в группе, нажмите кнопку ↓ (скачивание запустит тот, кто нажмёт и будет учитываться в его лимитах).", reply_markup=kb)
        return

# -------------------- Admin hidden --------------------
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("Admin panel: /stats /give_points /give_gold /give_diamond")

# -------------------- Startup --------------------
async def main():
    global BOT_USERNAME
    await init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username or None
    logger.info("Bot username: %s", BOT_USERNAME)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    asyncio.create_task(download_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())