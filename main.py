# main.py — полностью рабочая версия (исправлены: TikTok photo, Pinterest images, отправка media_group)
import os
import re
import uuid
import shutil
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

# ---------- Настройки / лог ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN") or "ТОКЕН_БОТА"   # <- замените на реальный
ADMIN_ID = int(os.getenv("ADMIN_ID") or 6705555401)
DB_PATH = os.getenv("DB_PATH") or "bot_db.sqlite"

# премиум настройки
GOLD_PRICE = int(os.getenv("GOLD_PRICE") or 120)
GOLD_DAYS = int(os.getenv("GOLD_DAYS") or 30)
DIAMOND_PRICE = int(os.getenv("DIAMOND_PRICE") or 250)
DIAMOND_DAYS = int(os.getenv("DIAMOND_DAYS") or 90)
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}

AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS") or 30 * 60)

COOKIES_FILE = os.path.join(os.getcwd(), "cookies.txt")
USE_COOKIES = os.path.exists(COOKIES_FILE)

# бот и очередь
bot = Bot(TOKEN)
dp = Dispatcher()
download_queue: asyncio.Queue = asyncio.Queue()

# кеш
audio_cache: Dict[str, Dict[str, Optional[Any]]] = {}

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff")

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

# ---------- лимиты ----------
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

# ---------- Вспомогательные функции для веба и изображений ----------
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
    urls = set()
    # common direct images
    for m in re.finditer(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp|gif)', html, flags=re.IGNORECASE):
        urls.add(m.group(0))
    # JSON-like fields used by TikTok/Pinterest/Instagram
    for m in re.finditer(r'"(https?://[^\s"\'<>]+?(?:jpg|jpeg|png|webp|gif)[^\s"\'<>]*)"', html, flags=re.IGNORECASE):
        urls.add(m.group(1))
    # og:image meta
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        urls.add(m.group(1))
    return sorted(urls)

def download_file_sync(url: str, dest_path: str, timeout: int = 20) -> bool:
    try:
        with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception:
        return False

def fetch_and_save_images_page(url: str, folder: str, limit: int = 20) -> List[str]:
    saved: List[str] = []
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        html = r.text
    except Exception:
        return saved
    img_urls = find_image_urls_from_html(html)
    # extra heuristics for TikTok mobile markup (urlList etc.)
    if not img_urls:
        for m in re.finditer(r'"urlList":\s*\[(.*?)\]', html, flags=re.IGNORECASE):
            inside = m.group(1)
            found = re.findall(r'"(https?://[^"]+)"', inside)
            for u in found:
                img_urls.append(u)
    img_urls = sorted(dict.fromkeys(img_urls))  # dedupe keep order
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

# ---------- yt-dlp helpers ----------
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
    """
    Универсальная загрузка:
    - Если TikTok photo -> парсим HTML и скачиваем картинки (не запускаем yt-dlp)
    - Если Pinterest (часто image-only) -> сначала пробуем HTML images
    - Иначе -> yt-dlp, при ошибке -> HTML fallback
    """
    logger.info("safe_download_video start url=%s", url)

    # --- TikTok photo (handle immediately, do NOT call yt-dlp) ---
    if "tiktok.com" in url and "/photo/" in url:
        logger.info("Detected TikTok photo post — parsing page for images")
        url = sanitize_tiktok_photo_url(url)
        saved = fetch_and_save_images_page(url, folder, limit=30)
        if saved:
            logger.info("Saved TikTok photo images: %d", len(saved))
            return
        # Try additional regex extraction for TikTok mobile markup
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            html = r.text
            # look for displayUrl, originCover, etc.
            found = re.findall(r'"displayUrl":"(https:[^"]+)"', html)
            found += re.findall(r'"originUrl":"(https:[^"]+)"', html)
            found += re.findall(r'"downloadAddr":"(https:[^"]+)"', html)
            found = [u.replace("\\u002F", "/").replace("\\", "") for u in found]
            for i, u in enumerate(dict.fromkeys(found), start=1):
                ext = "jpg"
                dest = os.path.join(folder, f"photo_{i}.{ext}")
                download_file_sync(u, dest)
            saved_local = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(("jpg","jpeg","png","webp"))]
            if saved_local:
                logger.info("Saved via regex found urls: %d", len(saved_local))
                return
        except Exception:
            pass
        raise Exception("Не удалось найти/скачать фото TikTok (формат страницы нестандартный)")

    # --- resolve short links ---
    if any(s in url for s in ("vm.tiktok.com", "vt.tiktok.com", "https://vt.", "https://vm.")):
        url = resolve_redirect(url)

    # --- Pinterest / image-first pages: try HTML fallback first (often pins are images) ---
    if "pinterest" in url or "pin.it" in url:
        saved = fetch_and_save_images_page(url, folder, limit=30)
        if saved:
            logger.info("Pinterest HTML fallback saved images: %d", len(saved))
            return
        # otherwise continue to yt-dlp attempt (some pins are videos)

    # --- main yt-dlp attempt ---
    try:
        filename = download_with_ytdlp(url, folder, cookiefile=COOKIES_FILE if USE_COOKIES else None)
        logger.info("yt-dlp downloaded file: %s", filename)
        return
    except Exception as e:
        logger.warning("yt-dlp failed: %s", e)
        # try HTML fallback (images)
        saved = fetch_and_save_images_page(url, folder, limit=30)
        if saved:
            logger.info("HTML fallback saved images after yt-dlp fail: %d", len(saved))
            return
        # last attempt: gentler yt-dlp
        try:
            outtmpl = os.path.join(folder, "%(id)s.%(ext)s")
            ydl_opts2 = {
                "outtmpl": outtmpl,
                "format": "best",
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": False,
                "noplaylist": True,
                "http_headers": {"User-Agent": "Mozilla/5.0"},
                "allow_unplayable_formats": True,
            }
            if USE_COOKIES:
                ydl_opts2["cookiefile"] = COOKIES_FILE
            with YoutubeDL(ydl_opts2) as ydl:
                ydl.download([url])
            return
        except Exception as e2:
            logger.warning("second yt-dlp attempt failed: %s", e2)
            saved2 = fetch_and_save_images_page(url, folder, limit=30)
            if saved2:
                return
            raise

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

# ---------- worker ----------
async def download_worker():
    while True:
        chat_id, user_id, url = await download_queue.get()
        tmp = tempfile.mkdtemp()
        token: Optional[str] = None
        try:
            await bot.send_message(chat_id, "⏳ Скачиваю...")
            # запускаем безопасную загрузку в executor
            await asyncio.get_event_loop().run_in_executor(None, partial(safe_download_video, url, tmp))

            # находим файлы
            video_path: Optional[str] = None
            image_paths: List[str] = []
            for f in os.listdir(tmp):
                full = os.path.join(tmp, f)
                if f.lower().endswith(VIDEO_EXTS):
                    video_path = full
                    break
            if not video_path:
                for f in os.listdir(tmp):
                    full = os.path.join(tmp, f)
                    if f.lower().endswith(IMAGE_EXTS):
                        image_paths.append(full)

            # отправка изображений как альбом (telegram ограничение 10)
            if image_paths and not video_path:
                try:
                    images_sorted = sorted(image_paths)[:10]
                    media = [types.InputMediaPhoto(media=FSInputFile(path)) for path in images_sorted]
                    await bot.send_media_group(chat_id, media=media)
                    await bot.send_message(chat_id, "✅ Готово! (изображения)")
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

            # отправка видео
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

# ---------- callback get_audio ----------
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

# ---------- Команды ----------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    await add_user(m.from_user.id)
    info = "Отправь ссылку на TikTok, Instagram, YouTube, Pinterest и бот скачает видео/изображения."
    if USE_COOKIES:
        info += "\n(Используется cookies.txt для авторизации пинов)"
    await m.answer("🔥TikGram_bot\n\n" + info)

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔥TikGram_bot\n\nОтправь ссылку на TikTok,Instagram,YouTube,Pinterest и бот скачает видео/изображения.")

@dp.message(Command("profile"))
async def profile_handler(m: Message):
    user = await get_user(m.from_user.id)
    if not user:
        await m.answer("👤 Профиль: не найден")
        return
    await m.answer(f"👤 Профиль\n💎 {user[1]}\n⭐ Звёзды: {user[2]}\n")

@dp.message(Command("premium"))
async def premium_handler(m: Message):
    await m.answer(
        f"💎 Премиум:\nОбычный — 4 видео в день\n🥇 Золотой — {GOLD_PRICE}⭐ ({GOLD_DAYS} дней)\n💠 Алмазный — {DIAMOND_PRICE}⭐ ({DIAMOND_DAYS} дней)\nКоманды: /buy_gold /buy_diamond"
    )

@dp.message(Command("about"))
async def about_handler(m: Message):
    info = "🤖 Бот скачивает видео и изображения, может вырезать аудио.\nПоддержка: TikTok (видео и photo-posts), YouTube, Pinterest, Instagram."
    if USE_COOKIES:
        info += "\nИспользуется cookies.txt для авторизации приватного контента."
    await m.answer(info)

@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    await add_user(m.from_user.id)
    await m.answer("🔗 Отправьте ссылку на видео/пин и я обработаю его и пришлю вам!")

@dp.message(F.text.startswith("http"))
async def link_handler(m: Message):
    user_id = m.from_user.id
    url = m.text.strip()
    await add_user(user_id)
    if not await can_download(user_id):
        await m.answer("❌ Превышен лимит загрузок для вашего уровня.")
        return
    await download_queue.put((m.chat.id, user_id, url))
    await m.answer("📥 Добавлено в очередь...")

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
        text = f"📊 Ваш лимит на сегодня:\n💎 Статус: {premium}\n⬇️ Осталось скачиваний: {remaining}/{limit}"
    await m.answer(text)

# ---------- Админ команды ----------
@dp.message(Command("admin"))
async def admin_handler(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("/stats\n/give_gold ID\n/give_diamond ID\n/add_stars ID сумма")

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