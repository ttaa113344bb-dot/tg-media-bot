import os
import re
import uuid
import logging
import asyncio
from pathlib import Path

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 ميجابايت
MAX_CAPTION_LEN = 950  # نترك هامشًا تحت حد تليجرام (1024)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"(https?://\S+)")

# تخزين مؤقت في الذاكرة يربط معرفًا قصيرًا بالرابط الأصلي (لاستخدامه في زر "تحميل الصوت")
URL_CACHE: dict[str, str] = {}
URL_CACHE_MAX = 500  # لتفادي تضخم الذاكرة على المدى الطويل


def cache_url(url: str) -> str:
    if len(URL_CACHE) >= URL_CACHE_MAX:
        oldest_key = next(iter(URL_CACHE))
        URL_CACHE.pop(oldest_key, None)
    short_id = uuid.uuid4().hex[:10]
    URL_CACHE[short_id] = url
    return short_id


# ----------------------------------------------------------------------------
# دوال مساعدة
# ----------------------------------------------------------------------------

def extract_url(text: str):
    match = URL_REGEX.search(text)
    return match.group(1) if match else None


def build_ydl_opts(out_dir: Path, unique_id: str, audio_only: bool = False) -> dict:
    output_template = str(out_dir / f"{unique_id}.%(ext)s")
    opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_TELEGRAM_FILE_SIZE,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        },
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        opts["format"] = "mp4/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"
    return opts


def fetch_info(url: str) -> dict:
    """يجلب معلومات الوسائط (بدون تحميل) لعرض العنوان/الوصف/الناشر."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_media(url: str, out_dir: Path, audio_only: bool = False) -> Path:
    unique_id = uuid.uuid4().hex
    opts = build_ydl_opts(out_dir, unique_id, audio_only=audio_only)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    file_path = Path(filename)
    if audio_only:
        mp3_path = file_path.with_suffix(".mp3")
        if mp3_path.exists():
            return mp3_path

    if not file_path.exists():
        for f in out_dir.glob(f"{unique_id}.*"):
            file_path = f
            break

    return file_path


def build_caption(info: dict) -> str:
    """يبني نصًا يشبه نص المنشور الأصلي: الناشر + الوصف/العنوان + الرابط."""
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    description = (info.get("description") or "").strip()
    title = (info.get("title") or "").strip()
    webpage_url = info.get("webpage_url") or info.get("original_url") or ""

    body = description if description else title

    parts = []
    if uploader:
        parts.append(f"👤 {uploader}")
    if body:
        parts.append(body)

    caption = "\n\n".join(parts).strip()

    if len(caption) > MAX_CAPTION_LEN:
        caption = caption[:MAX_CAPTION_LEN].rstrip() + "…"

    if webpage_url:
        link_line = f"\n\n🔗 {webpage_url}"
        if len(caption) + len(link_line) <= MAX_CAPTION_LEN + 100:
            caption += link_line

    return caption or "🎬"


def build_keyboard(short_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔊 تحميل الصوت", callback_data=f"audio:{short_id}")]]
    )


async def cleanup_file(file_path):
    if file_path and file_path.exists():
        try:
            file_path.unlink()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# معالجات الأوامر والرسائل
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! 👋\n\n"
        "أرسل لي رابط فيديو أو صورة من تيك توك، يوتيوب، تويتر/X، إنستغرام أو أي منصة أخرى "
        "وسأقوم بتحميله مع وصف المنشور الأصلي، وسأعطيك خيار تحميل الصوت فقط.\n\n"
        "يمكنك أيضًا إضافتي إلى مجموعة، وسأقوم تلقائيًا بتحميل أي رابط يُرسله الأعضاء."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    url = extract_url(message.text)
    if not url:
        return

    status_msg = await message.reply_text("⏳ جاري التحميل...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    file_path = None
    try:
        loop = asyncio.get_running_loop()

        info = await loop.run_in_executor(None, fetch_info, url)
        file_path = await loop.run_in_executor(None, download_media, url, DOWNLOAD_DIR, False)

        if not file_path or not file_path.exists():
            await status_msg.edit_text("❌ تعذر تحميل هذا الرابط.")
            return

        size = file_path.stat().st_size
        if size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text(
                "❌ حجم الملف أكبر من الحد المسموح به لإرساله عبر بوتات تليجرام (50 ميجابايت)."
            )
            return

        caption = build_caption(info)
        short_id = cache_url(url)
        keyboard = build_keyboard(short_id)

        suffix = file_path.suffix.lower()
        with open(file_path, "rb") as f:
            if suffix in (".jpg", ".jpeg", ".png", ".webp"):
                await message.reply_photo(photo=f, caption=caption, reply_markup=keyboard)
            else:
                await message.reply_video(
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                    reply_markup=keyboard,
                )

        await status_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"Download error for {url}: {e}")
        await status_msg.edit_text("❌ لم أتمكن من تحميل هذا الرابط. تأكد أنه صحيح وعام (غير خاص).")
    except Exception:
        logger.exception(f"Unexpected error for {url}")
        await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء التحميل.")
    finally:
        await cleanup_file(file_path)


async def handle_audio_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("audio:"):
        return

    short_id = data.split(":", 1)[1]
    url = URL_CACHE.get(short_id)
    if not url:
        await query.answer("انتهت صلاحية هذا الطلب، أرسل الرابط من جديد.", show_alert=True)
        return

    chat_id = query.message.chat_id
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔊 جاري تحميل الصوت...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

    file_path = None
    try:
        loop = asyncio.get_running_loop()
        file_path = await loop.run_in_executor(None, download_media, url, DOWNLOAD_DIR, True)

        if not file_path or not file_path.exists():
            await status_msg.edit_text("❌ تعذر تحميل الصوت من هذا الرابط.")
            return

        size = file_path.stat().st_size
        if size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text("❌ حجم ملف الصوت أكبر من الحد المسموح به (50 ميجابايت).")
            return

        with open(file_path, "rb") as f:
            await context.bot.send_audio(chat_id=chat_id, audio=f)

        await status_msg.delete()

    except yt_dlp.utils.DownloadError:
        await status_msg.edit_text("❌ لم أتمكن من تحميل الصوت من هذا الرابط.")
    except Exception:
        logger.exception(f"Unexpected error while downloading audio for {url}")
        await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء تحميل الصوت.")
    finally:
        await cleanup_file(file_path)


# ----------------------------------------------------------------------------
# نقطة التشغيل
# ----------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "خطأ: لم يتم ضبط متغير البيئة BOT_TOKEN. "
            "احصل على التوكن من @BotFather وأضفه في إعدادات المتغيرات."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_audio_button, pattern=r"^audio:"))

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
