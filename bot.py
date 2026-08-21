import os
import re
import uuid
import logging
import asyncio
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# الحد الأقصى لحجم الملف الذي يمكن لبوت تليجرام إرساله (تقريبًا) بدون سيرفر محلي
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 ميجابايت

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"(https?://\S+)")


# ----------------------------------------------------------------------------
# دوال مساعدة
# ----------------------------------------------------------------------------

def extract_url(text: str) -> str | None:
    """يستخرج أول رابط موجود في نص الرسالة."""
    match = URL_REGEX.search(text)
    return match.group(1) if match else None


def download_media(url: str, out_dir: Path) -> Path:
    """
    يحمّل الفيديو أو الصورة من الرابط باستخدام yt-dlp.
    يدعم يوتيوب، تيك توك، تويتر/X، إنستغرام، فيسبوك، وغيرها الكثير
    من المنصات التي يدعمها yt-dlp تلقائيًا.
    """
    unique_id = uuid.uuid4().hex
    output_template = str(out_dir / f"{unique_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "mp4/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_TELEGRAM_FILE_SIZE,
        # بعض الروابط (تيك توك بدون علامة مائية مثلاً) تحتاج user-agent
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    file_path = Path(filename)
    if not file_path.exists():
        # قد يغيّر yt-dlp الامتداد بعد الدمج (mkv -> mp4 مثلاً)
        for f in out_dir.glob(f"{unique_id}.*"):
            file_path = f
            break

    return file_path


# ----------------------------------------------------------------------------
# معالجات الأوامر والرسائل
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! 👋\n\n"
        "أرسل لي رابط فيديو أو صورة من تيك توك، يوتيوب، تويتر/X، إنستغرام أو أي منصة أخرى "
        "وسأقوم بتحميله وإرساله لك مباشرة.\n\n"
        "يمكنك أيضًا إضافتي إلى مجموعة، وسأقوم تلقائيًا بتحميل أي رابط يُرسله الأعضاء."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    url = extract_url(message.text)
    if not url:
        return  # تجاهل الرسائل التي لا تحتوي على رابط (مهم جدًا داخل المجموعات)

    status_msg = await message.reply_text("⏳ جاري التحميل...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    file_path = None
    try:
        loop = asyncio.get_running_loop()
        file_path = await loop.run_in_executor(None, download_media, url, DOWNLOAD_DIR)

        if not file_path or not file_path.exists():
            await status_msg.edit_text("❌ تعذر تحميل هذا الرابط.")
            return

        size = file_path.stat().st_size
        if size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text(
                "❌ حجم الملف أكبر من الحد المسموح به لإرساله عبر بوتات تليجرام (50 ميجابايت)."
            )
            return

        suffix = file_path.suffix.lower()
        with open(file_path, "rb") as f:
            if suffix in (".jpg", ".jpeg", ".png", ".webp"):
                await message.reply_photo(photo=f)
            else:
                await message.reply_video(video=f, supports_streaming=True)

        await status_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"Download error for {url}: {e}")
        await status_msg.edit_text("❌ لم أتمكن من تحميل هذا الرابط. تأكد أنه صحيح وعام (غير خاص).")
    except Exception as e:
        logger.exception(f"Unexpected error for {url}")
        await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء التحميل.")
    finally:
        if file_path and file_path.exists():
            try:
                file_path.unlink()
            except Exception:
                pass


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

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
