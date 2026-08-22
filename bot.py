import os
import re
import html
import shutil
import uuid
import logging
import asyncio
from pathlib import Path
from collections import OrderedDict

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

# ----------------------------------------------------------------------------
# الإعدادات العامة
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 ميجابايت (حد بوتات تليجرام السحابية)
MAX_CAPTION_LEN = 950                      # هامش أمان تحت حد تليجرام (1024)
MAX_CONCURRENT_DOWNLOADS = 3               # عدد التحميلات المتزامنة المسموحة
PROGRESS_UPDATE_INTERVAL = 3               # ثواني بين كل تحديث لرسالة "جاري التحميل"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"(https?://\S+)")

# يمنع تحميل عشرات الفيديوهات بالتوازي وإرهاق السيرفر
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


class LRUCache(OrderedDict):
    """كاش بسيط محدود الحجم (Least Recently Used) لتفادي تضخم الذاكرة."""

    def __init__(self, max_size: int):
        super().__init__()
        self.max_size = max_size

    def put(self, key, value):
        if key in self:
            self.move_to_end(key)
        self[key] = value
        if len(self) > self.max_size:
            self.popitem(last=False)

    def get_value(self, key):
        if key in self:
            self.move_to_end(key)
            return self[key]
        return None


# رابط قصير -> رابط أصلي (يُستخدم في زر "تحميل الصوت")
URL_CACHE = LRUCache(max_size=1000)

# (رابط، نوع) -> file_id في تليجرام، لإعادة الإرسال الفوري دون تحميل مكرر
FILE_ID_CACHE = LRUCache(max_size=1000)


def cache_url(url: str) -> str:
    short_id = uuid.uuid4().hex[:10]
    URL_CACHE.put(short_id, url)
    return short_id


# ----------------------------------------------------------------------------
# دوال مساعدة عامة
# ----------------------------------------------------------------------------

def extract_urls(text: str) -> list[str]:
    """يستخرج كل الروابط الفريدة من نص الرسالة (يدعم أكثر من رابط بنفس الرسالة)."""
    found = URL_REGEX.findall(text)
    seen = []
    for u in found:
        if u not in seen:
            seen.append(u)
    return seen


def common_http_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }


def build_ydl_opts(out_dir: Path, unique_id: str, audio_only: bool = False, progress_hook=None) -> dict:
    output_template = str(out_dir / f"{unique_id}.%(ext)s")
    opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_TELEGRAM_FILE_SIZE,
        "http_headers": common_http_headers(),
        "retries": 3,
        "fragment_retries": 3,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        # نحاول أفضل جودة mp4 متوافقة، مع رجوع تلقائي لأفضل صيغة متاحة إن لم تتوفر
        opts["format"] = "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"
    return opts


def fetch_info(url: str) -> dict:
    """يجلب معلومات الوسائط (بدون تحميل) لعرض العنوان/الوصف/الناشر."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": common_http_headers(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_media(url: str, out_dir: Path, audio_only: bool = False, progress_hook=None) -> Path:
    unique_id = uuid.uuid4().hex
    opts = build_ydl_opts(out_dir, unique_id, audio_only=audio_only, progress_hook=progress_hook)

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
    """
    تنسيق HTML يشبه نص المنشور الأصلي: الناشر + الوصف/العنوان،
    مع رابط الفيديو كنص قابل للنقر بعنوان "رابط الفيديو".
    """
    uploader = (info.get("uploader") or info.get("channel") or info.get("uploader_id") or "").strip()
    description = (info.get("description") or "").strip()
    title = (info.get("title") or "").strip()
    webpage_url = info.get("webpage_url") or info.get("original_url") or ""

    body = description if description else title

    if len(body) > MAX_CAPTION_LEN:
        body = body[:MAX_CAPTION_LEN].rstrip() + "…"

    parts = []
    if uploader:
        parts.append(f"👤 <b>{html.escape(uploader)}</b>")
    if body:
        parts.append(html.escape(body))

    caption = "\n\n".join(parts).strip()

    if webpage_url:
        safe_url = html.escape(webpage_url, quote=True)
        caption += f'\n\n🔗 <a href="{safe_url}">رابط الفيديو</a>'

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


def check_ffmpeg() -> bool:
    found = shutil.which("ffmpeg") is not None
    if found:
        logger.info("✅ ffmpeg موجود، ميزة تحميل الصوت جاهزة.")
    else:
        logger.warning(
            "⚠️ ffmpeg غير موجود على السيرفر! ميزة 'تحميل الصوت' لن تعمل. "
            "تأكد من رفع ملف nixpacks.toml إلى المستودع وإعادة النشر."
        )
    return found


# ----------------------------------------------------------------------------
# متابعة تقدّم التحميل (Progress) وتحديث رسالة الحالة بشكل مباشر
# ----------------------------------------------------------------------------

def make_progress_hook(state: dict):
    """يُستدعى من داخل خيط yt-dlp، فقط يحدّث قاموسًا مشتركًا (آمن بفضل GIL)."""

    def hook(d):
        if d.get("status") == "downloading":
            state["percent"] = (d.get("_percent_str") or "").strip()
            state["speed"] = (d.get("_speed_str") or "").strip()
            state["eta"] = (d.get("_eta_str") or "").strip()
            state["stage"] = "downloading"
        elif d.get("status") == "finished":
            state["stage"] = "processing"

    return hook


async def progress_updater(bot, chat_id: int, message_id: int, state: dict, stop_event: asyncio.Event):
    last_text = None
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PROGRESS_UPDATE_INTERVAL)
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        if state.get("stage") == "processing":
            text = "⚙️ جاري المعالجة والدمج..."
        else:
            percent = state.get("percent", "")
            speed = state.get("speed", "")
            eta = state.get("eta", "")
            extra = " ".join(p for p in [speed, f"⏱ {eta}" if eta else ""] if p)
            text = f"⏳ جاري التحميل... {percent}\n{extra}".strip()

        if text != last_text:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                last_text = text
            except BadRequest:
                pass
            except Exception:
                pass


# ----------------------------------------------------------------------------
# معالجات الأوامر
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! 👋\n\n"
        "أرسل لي رابط فيديو أو صورة من تيك توك، يوتيوب، تويتر/X، إنستغرام أو أي منصة أخرى، "
        "وسأحمّله لك مع وصف المنشور الأصلي، وخيار تحميل الصوت فقط.\n\n"
        "✅ يدعم أكثر من رابط في نفس الرسالة\n"
        "✅ إعادة إرسال فورية لو طُلب نفس الرابط مرة ثانية\n"
        "✅ يعمل داخل المجموعات تلقائيًا\n\n"
        "أرسل /help لمزيد من التفاصيل."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>كيف أستخدم البوت؟</b>\n\n"
        "• أرسل رابط الفيديو مباشرة (أو أكثر من رابط بنفس الرسالة).\n"
        "• داخل المجموعات: أضفني كعضو وتأكد أن Privacy Mode متوقف من BotFather، "
        "وسأحمّل أي رابط يُرسله الأعضاء تلقائيًا.\n"
        "• بعد إرسال الفيديو سيظهر زر 🔊 لتحميل الصوت فقط بصيغة MP3.\n\n"
        "⚠️ الحد الأقصى لحجم أي ملف هو 50 ميجابايت (قيد من تليجرام نفسه).",
        parse_mode="HTML",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    urls = extract_urls(message.text)
    if not urls:
        return

    for url in urls:
        await process_video_url(message, context, url)


async def process_video_url(message, context: ContextTypes.DEFAULT_TYPE, url: str):
    async with DOWNLOAD_SEMAPHORE:
        status_msg = await message.reply_text("⏳ جاري التحميل...")
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

        file_path = None
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, fetch_info, url)

            caption = build_caption(info)
            short_id = cache_url(url)
            keyboard = build_keyboard(short_id)

            cached_file_id = FILE_ID_CACHE.get_value((url, "video"))
            if cached_file_id:
                try:
                    await message.reply_video(
                        video=cached_file_id, caption=caption, parse_mode="HTML", reply_markup=keyboard
                    )
                    await status_msg.delete()
                    return
                except BadRequest:
                    # الملف المخزن لم يعد صالحًا لدى تليجرام، نكمل بالتحميل العادي
                    pass

            state: dict = {}
            stop_event = asyncio.Event()
            updater_task = asyncio.create_task(
                progress_updater(context.bot, status_msg.chat_id, status_msg.message_id, state, stop_event)
            )
            try:
                hook = make_progress_hook(state)
                file_path = await loop.run_in_executor(
                    None, download_media, url, DOWNLOAD_DIR, False, hook
                )
            finally:
                stop_event.set()
                await updater_task

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
                    sent = await message.reply_photo(
                        photo=f, caption=caption, parse_mode="HTML", reply_markup=keyboard
                    )
                else:
                    sent = await message.reply_video(
                        video=f,
                        caption=caption,
                        parse_mode="HTML",
                        supports_streaming=True,
                        reply_markup=keyboard,
                    )

            file_id = None
            if getattr(sent, "video", None):
                file_id = sent.video.file_id
            elif getattr(sent, "photo", None):
                file_id = sent.photo[-1].file_id
            if file_id:
                FILE_ID_CACHE.put((url, "video"), file_id)

            await status_msg.delete()

        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"Download error for {url}: {e}")
            await status_msg.edit_text(
                "❌ لم أتمكن من تحميل هذا الرابط.\n"
                "تأكد أنه صحيح وعام (غير خاص)، أو أن المنصة مدعومة."
            )
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
    url = URL_CACHE.get_value(short_id)
    if not url:
        await query.answer("انتهت صلاحية هذا الطلب، أرسل الرابط من جديد.", show_alert=True)
        return

    chat_id = query.message.chat_id

    async with DOWNLOAD_SEMAPHORE:
        status_msg = await context.bot.send_message(chat_id=chat_id, text="🔊 جاري تحميل الصوت...")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

        cached_file_id = FILE_ID_CACHE.get_value((url, "audio"))
        if cached_file_id:
            try:
                await context.bot.send_audio(chat_id=chat_id, audio=cached_file_id)
                await status_msg.delete()
                return
            except BadRequest:
                pass

        file_path = None
        try:
            loop = asyncio.get_running_loop()

            state: dict = {}
            stop_event = asyncio.Event()
            updater_task = asyncio.create_task(
                progress_updater(context.bot, status_msg.chat_id, status_msg.message_id, state, stop_event)
            )
            try:
                hook = make_progress_hook(state)
                file_path = await loop.run_in_executor(
                    None, download_media, url, DOWNLOAD_DIR, True, hook
                )
            finally:
                stop_event.set()
                await updater_task

            if not file_path or not file_path.exists():
                await status_msg.edit_text("❌ تعذر تحميل الصوت من هذا الرابط.")
                return

            size = file_path.stat().st_size
            if size > MAX_TELEGRAM_FILE_SIZE:
                await status_msg.edit_text("❌ حجم ملف الصوت أكبر من الحد المسموح به (50 ميجابايت).")
                return

            with open(file_path, "rb") as f:
                sent = await context.bot.send_audio(chat_id=chat_id, audio=f)

            if getattr(sent, "audio", None):
                FILE_ID_CACHE.put((url, "audio"), sent.audio.file_id)

            await status_msg.delete()

        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"Audio download error for {url}: {e}")
            if not check_ffmpeg():
                await status_msg.edit_text(
                    "❌ لا يمكن تحميل الصوت: برنامج ffmpeg غير مثبت على السيرفر.\n"
                    "تأكد من رفع ملف nixpacks.toml وإعادة النشر."
                )
            else:
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

    check_ffmpeg()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_audio_button, pattern=r"^audio:"))

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
