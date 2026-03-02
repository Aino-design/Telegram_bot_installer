# main.py
import os
import re
import uuid
import shutil
import tempfile
import logging
import asyncio
import random
from functools import partial
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests
import aiosqlite
from yt_dlp import YoutubeDL

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)

# ----------------- CONFIG -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "REPLACE_WITH_YOUR_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)  # <-- твой телеграм id
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

# pricing / limits
GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)
COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".aac", ".opus")
MIN_VIDEO_BYTES = 20_000

bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()
audio_cache: Dict[str, Dict[str, Any]] = {}
format_cache: Dict[str, Dict[str, Any]] = {}  # token -> {url, owner, options}
BOT_USERNAME: Optional[str] = None

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None
logger.info("ffmpeg: %s, ffprobe: %s, cookies present: %s", HAS_FFMPEG, HAS_FFPROBE, USE_COOKIES)

# ----------------- DB helpers -----------------
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

# ----------------- web helpers -----------------
def resolve_redirect(url: str, timeout: int = 10) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, allow_redirects=True)
        if r.status_code in (200, 301, 302):
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

# ----------------- yt-dlp: list formats -----------------
def list_formats_for_url(url: str) -> Dict[str, Any]:
    """Return dict: {'title':..., 'formats': [...]} — each format is dict from yt-dlp."""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "forcejson": True, "noplaylist": True, "format": "best",
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    if USE_COOKIES:
        opts["cookiefile"] = COOKIES_FILE
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {"title": info.get("title") if isinstance(info, dict) else "", "formats": info.get("formats", [])}

def pick_heights_from_formats(formats: List[Dict[str, Any]]) -> List[int]:
    heights = set()
    for f in formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        # vcodec 'none' means audio-only
        if vcodec and vcodec != "none" and h:
            heights.add(int(h))
    # keep only common heights and sort
    res = sorted([h for h in heights if h >= 100])
    # reduce to a nice list: include typical set
    typical = [144, 240, 360, 480, 720, 1080, 1440, 2160]
    out = []
    for t in typical:
        if t in res:
            out.append(t)
    # if none of typical, include some from res (max 5)
    if not out and res:
        out = sorted(res)[:5]
    return out

# ----------------- building format expressions -----------------
def format_expr_for_height(h: int) -> str:
    # choose adaptive merge when possible, fallback to best
    # this expression tries to get bestvideo <= h + bestaudio, else best[height<=h], else best
    return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"

# ----------------- download with yt-dlp (format expression) -----------------
def download_with_ytdlp(url: str, folder: str, format_expr: Optional[str] = None) -> str:
    outtmpl = os.path.join(folder, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": format_expr or "best",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "noplaylist": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "allow_unplayable_formats": True,
        "merge_output_format": "mp4",
    }
    if USE_COOKIES:
        ydl_opts["cookiefile"] = COOKIES_FILE
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
        # fallback to whatever file was created
        files = [os.path.join(folder, f) for f in os.listdir(folder)]
        files = [f for f in files if os.path.isfile(f)]
        if not files:
            raise Exception("yt-dlp didn't save any file")
        return sorted(files, key=os.path.getmtime, reverse=True)[0]

# ----------------- ffprobe helpers -----------------
async def has_video_stream(path: str) -> bool:
    if not shutil.which("ffprobe"):
        lower = path.lower()
        return any(lower.endswith(ext) for ext in (".mp4", ".mkv", ".mov", ".ts", ".webm"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=index",
            "-of", "csv=p=0", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        return bool(out and out.strip())
    except Exception:
        return False

async def has_audio_stream(path: str) -> bool:
    if not shutil.which("ffprobe"):
        lower = path.lower()
        return any(lower.endswith(ext) for ext in AUDIO_EXTS + (".mp4", ".webm"))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
            "-of", "csv=p=0", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        return bool(out and out.strip())
    except Exception:
        return False

# ----------------- worker -----------------
async def download_worker():
    while True:
        chat_id, user_id, url, format_expr = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю... (может занять время)")
            # call download_with_ytdlp in executor
            filename = None
            try:
                filename = await asyncio.get_event_loop().run_in_executor(None, partial(download_with_ytdlp, url, tmp, format_expr))
                logger.info("download saved: %s", filename)
            except Exception as e:
                logger.exception("yt-dlp download failed: %s", e)
                # send user a helpful message if it's likely due to cookies/sign-in
                err_msg = str(e)
                if "Sign in to confirm" in err_msg or "login_required" in err_msg:
                    await bot.send_message(chat_id, "⚠️ YouTube требует входа (sign-in). Положите cookies.txt в папку бота и перезапустите.")
                else:
                    await bot.send_message(chat_id, f"❌ Ошибка при скачивании: {e}")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # pick chosen video file
            chosen_video = filename
            if not chosen_video or not os.path.exists(chosen_video):
                await bot.send_message(chat_id, "❌ Не удалось скачать видео (файл не найден).")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # check size
            if os.path.getsize(chosen_video) < MIN_VIDEO_BYTES:
                await bot.send_message(chat_id, "❌ Полученное видео слишком маленькое или пустое, попробуйте другой формат.")
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            # get title
            title = os.path.splitext(os.path.basename(chosen_video))[0]

            # prepare audio cache token
            token = uuid.uuid4().hex
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Получить песню 🎵", callback_data=f"get_audio:{token}")]
            ])
            caption = f"✅ Готово! {title}\nНажмите кнопку, чтобы получить MP3."

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

            # increment downloads
            try:
                await increment_download(user_id)
            except Exception:
                logger.exception("increment_download failed")

            # try pre-extract audio
            audio_path = os.path.join(tmp, "audio.mp3")
            audio_ok = False
            if shutil.which("ffmpeg"):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", chosen_video, "-vn", "-acodec", "mp3", "-ar", "44100", "-ac", "2",
                        audio_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    await proc.communicate()
                    audio_ok = os.path.exists(audio_path)
                except Exception:
                    audio_ok = False

            audio_cache[token] = {
                "audio": audio_path if audio_ok else None,
                "tmpdir": tmp,
                "video": chosen_video,
                "url": url,
                "owner": user_id,
                "title": title
            }

            # cleanup scheduled
            asyncio.create_task(cleanup_audio_after_delay(token, AUDIO_TTL_SECONDS))

        except Exception as exc:
            logger.exception("download_worker exception: %s", exc)
            try:
                await bot.send_message(chat_id, f"❌ Ошибка: {exc}")
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            download_queue.task_done()

async def cleanup_audio_after_delay(token: str, delay: int = AUDIO_TTL_SECONDS):
    await asyncio.sleep(delay)
    info = audio_cache.get(token)
    if not info:
        return
    try:
        tmpdir = info.get("tmpdir")
        audio = info.get("audio")
        if audio and os.path.exists(audio):
            try:
                os.remove(audio)
            except Exception:
                pass
        if tmpdir and os.path.exists(tmpdir):
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        logger.exception("cleanup failed")
    audio_cache.pop(token, None)

# ----------------- callbacks -----------------
@dp.callback_query(lambda c: c.data and c.data.startswith("choose_fmt:"))
async def cb_choose_fmt(cq: CallbackQuery):
    # cb format: choose_fmt:{token}:{key}
    try:
        parts = cq.data.split(":", 2)
        token = parts[1]
        key = parts[2]
    except Exception:
        await cq.answer("Неверные данные.", show_alert=True)
        return
    info = format_cache.get(token)
    if not info:
        await cq.answer("⚠️ Сессия устарела. Отправьте ссылку ещё раз.", show_alert=True)
        return
    owner = info.get("owner")
    url = info.get("url")
    chat_id = cq.message.chat.id
    # only owner or admin can choose
    if owner and cq.from_user.id != owner and cq.from_user.id != ADMIN_ID:
        await cq.answer("Только пользователь, который запросил список форматов, может выбрать качество.", show_alert=True)
        return

    # decode key to format_expr
    mapping = info.get("mapping", {})
    expr = mapping.get(key)
    if not expr:
        await cq.answer("Не могу найти выбранный формат.", show_alert=True)
        return

    await cq.answer("Добавляю в очередь скачивания...")

    # enqueue download: (chat_id, user_id, url, format_expr)
    await download_queue.put((chat_id, cq.from_user.id, url, expr))
    # remove format_cache entry to avoid reuse
    format_cache.pop(token, None)

@dp.callback_query(lambda c: c.data and c.data.startswith("get_audio:"))
async def cb_get_audio(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    info = audio_cache.get(token)
    if not info:
        await cq.answer("⚠️ Аудио устарело или недоступно.", show_alert=True)
        return
    owner = info.get("owner")
    if owner and cq.from_user.id != owner and cq.from_user.id != ADMIN_ID:
        await cq.answer("Только тот, кто запросил видео, может получить аудио.", show_alert=True)
        return
    await cq.answer()
    audio_path = info.get("audio")
    tmpdir = info.get("tmpdir")
    video_path = info.get("video")
    title = info.get("title") or "Аудио из видео"

    # if extracted
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

    # extract on demand if ffmpeg available
    if video_path and os.path.exists(video_path) and shutil.which("ffmpeg"):
        audio_now = os.path.join(tmpdir, "audio_on_demand.mp3")
        await bot.send_chat_action(cq.from_user.id, "record_audio")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "mp3", "-ar", "44100", "-ac", "2",
                audio_now, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()
        except Exception:
            audio_now = None
        if audio_now and os.path.exists(audio_now):
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

    # final fallback: try re-download and extract
    new_tmp = tempfile.mkdtemp()
    try:
        await cq.answer("Попытка повторного скачивания для извлечения аудио...", show_alert=True)
        await asyncio.get_event_loop().run_in_executor(None, partial(download_with_ytdlp, url, new_tmp, "bestaudio"))
        new_audio = None
        for f in os.listdir(new_tmp):
            if f.lower().endswith((".mp3", ".m4a", ".webm", ".aac", ".opus")):
                new_audio = os.path.join(new_tmp, f); break
        if new_audio:
            try:
                await bot.send_audio(cq.from_user.id, FSInputFile(new_audio), title=title)
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

# ----------------- commands -----------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    txt = (
        "🔥 TikGram_bot\n\n"
        "Я скачиваю видео по ссылкам (YouTube / Shorts, TikTok, Pinterest и др.) и отправляю их вам.\n"
        "Процесс: вы шлёте ссылку → бот показывает доступные качества → вы выбираете → бот скачивает и пришлёт видео.\n\n"
        "Команды:\n"
        "/premium — Информация о премиуме\n"
        "/profile — Профиль (очки, уровень, скачиваний осталось)\n"
        "/farm — Фарм очков (раз в 20 часов, 10–35 очков)\n\n"
        f"ffmpeg: {HAS_FFMPEG}, ffprobe: {HAS_FFPROBE}\n"
        "Если YouTube просит вход — добавьте cookies.txt в папку бота (формат cookies для yt-dlp).\n"
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
        f"• Золотой — лимит {LIMITS['золотой']} загрузок в день, срок {GOLD_DAYS} дней.\n"
        f"• Алмазный — безлимит, срок {DIAMOND_DAYS} дней.\n\n"
        "Нажмите кнопку, чтобы купить премиум за очки."
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

# ----------------- main message handler: show formats then let user choose -----------------
@dp.message()
async def general_message_handler(m: Message):
    text = m.text or m.caption or ""
    if not text:
        return
    link = extract_first_link_from_text(text)
    if not link:
        return

    # limit check
    if not await can_download(m.from_user.id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return

    # resolve redirects (short links)
    link = resolve_redirect(link)

    # try to list formats (yt-dlp)
    await add_user(m.from_user.id)
    info_msg = await m.answer("🔎 Получаю доступные форматы (yt-dlp)...")
    try:
        fmt_info = await asyncio.get_event_loop().run_in_executor(None, partial(list_formats_for_url, link))
    except Exception as e:
        logger.exception("list_formats_for_url error: %s", e)
        await info_msg.edit_text("❌ Не удалось получить форматы. Возможно YouTube требует вход (cookies). " +
                                 ("Положите cookies.txt и перезапустите." if not USE_COOKIES else ""))
        return

    formats = fmt_info.get("formats", [])
    title = fmt_info.get("title") or "Видео"

    heights = pick_heights_from_formats(formats)
    mapping: Dict[str, str] = {}
    keyboard = []
    # build typical buttons for heights
    for h in heights:
        key = f"h{h}"
        mapping[key] = format_expr_for_height(h)
        keyboard.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"choose_fmt:{key}:{key}")])  # we'll fix callback parsing below

    # add special options
    mapping["best"] = "best"
    mapping["audio"] = "bestaudio"
    # create keyboard rows (mapping keys used later; we'll store mapping in cache)
    kb_rows = []
    for h in heights:
        key = f"h{h}"
        kb_rows.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"choose_fmt:{{token}}:{key}")])
    # finalize rows with best/audio
    kb_rows.append([InlineKeyboardButton(text="Best", callback_data=f"choose_fmt:{{token}}:best"),
                    InlineKeyboardButton(text="Audio only", callback_data=f"choose_fmt:{{token}}:audio")])

    # generate token to store mapping (must replace {token} in callbacks)
    token = uuid.uuid4().hex
    # store mapping where keys map to expressions
    format_cache[token] = {"url": link, "owner": m.from_user.id, "mapping": mapping, "title": title}

    # now build InlineKeyboard with token embedded
    keyboard = []
    for h in heights:
        key = f"h{h}"
        keyboard.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"choose_fmt:{token}:{key}")])
    keyboard.append([
        InlineKeyboardButton(text="Best", callback_data=f"choose_fmt:{token}:best"),
        InlineKeyboardButton(text="Audio only", callback_data=f"choose_fmt:{token}:audio")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
    # edit message with title and buttons
    try:
        await info_msg.edit_text(f"📥 {m.from_user.first_name}, выберите качество для:\n{title}", reply_markup=kb)
    except Exception:
        await m.answer(f"📥 {m.from_user.first_name}, выберите качество для:\n{title}", reply_markup=kb)

# ----------------- admin -----------------
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        # do nothing for others
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

# ----------------- startup -----------------
async def main():
    global BOT_USERNAME
    await init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username or None
    logger.info("Bot username: %s", BOT_USERNAME)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook (ok to ignore)")
    asyncio.create_task(download_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())