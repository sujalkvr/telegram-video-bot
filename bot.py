import os
import re
import uuid
import asyncio
import logging
import tempfile
import subprocess
import threading
from datetime import date
import yt_dlp
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# --- Tiny web server, only so Render's free Web Service tier sees a live HTTP port ---
# This does nothing functional for the bot itself — Telegram polling (below) is the real logic.
web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    return "Bot is running."


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# --- Safety limits (tuned for Render's free tier: shared CPU, ~512MB RAM) ---
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024        # Telegram bot upload limit
MAX_DURATION_SECONDS = 20 * 60                # reject videos longer than 20 minutes
MAX_CONCURRENT_DOWNLOADS = 2                  # only 2 downloads run at once, globally
MAX_QUEUE_SIZE = 3                            # beyond this many waiting, reject new requests
MAX_DOWNLOADS_PER_USER_PER_DAY = 15           # per-person daily cap
SKIP_COMPRESSION_ABOVE_BYTES = 3 * MAX_FILE_SIZE_BYTES  # don't even try compressing huge files

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_url(text: str):
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def is_youtube_bot_block(error_message: str) -> bool:
    """Detects YouTube's anti-bot error so we can show a clearer message for it."""
    text = str(error_message).lower()
    return "sign in to confirm" in text and "not a bot" in text


YOUTUBE_BLOCKED_MESSAGE = (
    "YouTube is currently blocking downloads from this server — this is a known, "
    "widespread issue with YouTube's anti-bot detection on cloud-hosted bots, not something "
    "specific to your link. It may work again later, or you can try a different platform "
    "(Instagram, X, etc.) which don't have this issue."
)


# --- Global state for safety controls ---
active_downloads = set()        # user_ids currently downloading (prevents same user double-queueing)
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
waiting_queue = []              # list of CallbackQuery objects, in the order they're waiting
pending_links = {}              # short_id -> url (callback_data has a 64-byte limit)
daily_usage = {}                # user_id -> {"date": date, "count": int}

QUEUE_MESSAGES = [
    "You're #{pos} in line — grab a coffee, I've got you. ☕",
    "Position #{pos} in the queue. Almost there!",
    "#{pos} in line — your video's coming right up.",
]


async def update_queue_positions():
    """Refreshes the displayed position for everyone still waiting."""
    for i, waiting_query in enumerate(waiting_queue):
        position = i + 1
        text = QUEUE_MESSAGES[i % len(QUEUE_MESSAGES)].format(pos=position)
        try:
            await waiting_query.edit_message_text(text)
        except Exception:
            pass  # message may have been deleted/expired; safe to ignore

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# If a cookies.txt file is provided (via Render's Secret Files feature), use it.
# This helps avoid YouTube blocking requests from cloud/datacenter IPs.
import shutil

# Render's Secret Files are mounted read-only, but yt-dlp needs to write back to the
# cookies file after use — so we copy it to a writable location first.
RENDER_SECRET_COOKIES_PATH = "/etc/secrets/cookies.txt"
COOKIES_PATH = "/tmp/cookies.txt"

if os.path.exists(RENDER_SECRET_COOKIES_PATH):
    shutil.copyfile(RENDER_SECRET_COOKIES_PATH, COOKIES_PATH)
    COOKIES_AVAILABLE = True
else:
    COOKIES_AVAILABLE = False

print(f"[STARTUP CHECK] Cookies available: {COOKIES_AVAILABLE}")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError(
        "No token found. Set it before running, e.g.:\n"
        "  export TELEGRAM_BOT_TOKEN='your-token-here'   (Mac/Linux)\n"
        "  set TELEGRAM_BOT_TOKEN=your-token-here        (Windows)"
    )


def check_daily_limit(user_id: int) -> bool:
    """Returns True if the user still has downloads left today."""
    today = date.today()
    record = daily_usage.get(user_id)
    if record is None or record["date"] != today:
        daily_usage[user_id] = {"date": today, "count": 0}
        record = daily_usage[user_id]
    return record["count"] < MAX_DOWNLOADS_PER_USER_PER_DAY


def increment_daily_usage(user_id: int):
    daily_usage[user_id]["count"] += 1


def get_video_info(url: str):
    """Quick metadata-only lookup (no download) to check duration/validity upfront."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 2,
    }
    if COOKIES_AVAILABLE:
        ydl_opts["cookiefile"] = COOKIES_PATH
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get("duration"), info.get("title")


def download_video(url: str, output_dir: str, quality: str, progress_callback=None):
    """
    Downloads (or extracts audio from) the video at `url` into `output_dir`.
    quality: "720", "480", or "audio". Returns (filepath, duration_in_seconds).
    Blocking — call via asyncio.to_thread.
    """
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    def hook(d):
        if progress_callback and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress_callback(downloaded / total * 100)

    base_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "progress_hooks": [hook],
    }
    if COOKIES_AVAILABLE:
        base_opts["cookiefile"] = COOKIES_PATH

    if quality == "audio":
        ydl_opts = {
            **base_opts,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    else:
        ydl_opts = {
            **base_opts,
            "format": (
                f"best[ext=mp4][height<={quality}]/"
                f"bestvideo[ext=mp4][height<={quality}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/best"
            ),
            "merge_output_format": "mp4",
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        if quality == "audio":
            base, _ = os.path.splitext(filepath)
            filepath = base + ".mp3"
        return filepath, info.get("duration")


def compress_video(input_path: str, output_path: str, target_size_bytes: int, duration_seconds):
    """Re-encodes input_path to roughly fit under target_size_bytes. Requires ffmpeg."""
    if not duration_seconds or duration_seconds <= 0:
        duration_seconds = 60

    audio_bitrate_kbps = 128
    target_total_kbps = (target_size_bytes * 8 / 1000) / duration_seconds * 0.9
    video_bitrate_kbps = max(int(target_total_kbps - audio_bitrate_kbps), 100)

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", f"{video_bitrate_kbps}k",
        "-maxrate", f"{video_bitrate_kbps}k", "-bufsize", f"{video_bitrate_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{audio_bitrate_kbps}k",
        output_path
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a video link (YouTube, Instagram, X, and most other platforms work).\n\n"
        f"Note: videos longer than {MAX_DURATION_SECONDS // 60} minutes aren't supported, "
        f"and there's a {MAX_DOWNLOADS_PER_USER_PER_DAY}/day limit per person."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects a link, validates it (duration/daily limit), then asks for quality/format."""
    user_text = update.message.text
    user_id = update.effective_user.id
    url = extract_url(user_text)

    if url is None:
        await update.message.reply_text(
            "That doesn't look like a link. Send me a video link and I'll fetch it for you."
        )
        return

    if not check_daily_limit(user_id):
        await update.message.reply_text(
            f"You've hit your daily limit of {MAX_DOWNLOADS_PER_USER_PER_DAY} downloads. "
            "Try again tomorrow!"
        )
        return

    status_message = await update.message.reply_text("Checking link...")

    try:
        duration, title = await asyncio.to_thread(get_video_info, url)
    except Exception as e:
        logger.error(f"Info lookup failed: {e}")
        if is_youtube_bot_block(e):
            await status_message.edit_text(YOUTUBE_BLOCKED_MESSAGE)
        else:
            await status_message.edit_text(
                "Couldn't read that link — it may be unsupported, private, or invalid."
            )
        return

    if duration and duration > MAX_DURATION_SECONDS:
        await status_message.edit_text(
            f"That video is about {duration // 60} minutes long — "
            f"I only support videos under {MAX_DURATION_SECONDS // 60} minutes."
        )
        return

    link_id = uuid.uuid4().hex[:8]
    pending_links[link_id] = url

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("720p", callback_data=f"720|{link_id}"),
        InlineKeyboardButton("480p", callback_data=f"480|{link_id}"),
        InlineKeyboardButton("Audio (MP3)", callback_data=f"audio|{link_id}"),
    ]])
    await status_message.edit_text("What would you like?", reply_markup=keyboard)


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs when the user taps a quality/format button."""
    query = update.callback_query
    await query.answer()

    quality, link_id = query.data.split("|", 1)
    url = pending_links.pop(link_id, None)
    user_id = query.from_user.id

    if url is None:
        await query.edit_message_text("This link expired — please send it again.")
        return

    if user_id in active_downloads:
        await query.edit_message_text("You already have a download in progress — please wait for it to finish.")
        return

    # Global queue cap — protects the free-tier instance from pile-ups
    if len(waiting_queue) >= MAX_QUEUE_SIZE and download_semaphore.locked():
        await query.edit_message_text(
            "The bot is busy with other downloads right now. Please try again in a minute."
        )
        return

    active_downloads.add(user_id)

    joined_queue = download_semaphore.locked()
    if joined_queue:
        waiting_queue.append(query)
        position = len(waiting_queue)
        await query.edit_message_text(QUEUE_MESSAGES[(position - 1) % len(QUEUE_MESSAGES)].format(pos=position))

    try:
        async with download_semaphore:
            if joined_queue and query in waiting_queue:
                waiting_queue.remove(query)
                await update_queue_positions()  # shift everyone else's position down
            await query.edit_message_text("Starting download... 0%")

            loop = asyncio.get_running_loop()
            last_reported = {"percent": -10}

            def on_progress(percent):
                if percent - last_reported["percent"] >= 10:
                    last_reported["percent"] = percent
                    asyncio.run_coroutine_threadsafe(
                        query.edit_message_text(f"Downloading... {percent:.0f}%"),
                        loop
                    )

            with tempfile.TemporaryDirectory() as tmp_dir:
                try:
                    filepath, duration = await asyncio.to_thread(
                        download_video, url, tmp_dir, quality, on_progress
                    )
                except Exception as e:
                    logger.error(f"Download failed: {e}")
                    if is_youtube_bot_block(e):
                        await query.edit_message_text(YOUTUBE_BLOCKED_MESSAGE)
                    else:
                        await query.edit_message_text(
                            "Sorry, I couldn't download that. Either this site isn't supported, "
                            "the content is private, or the link is invalid."
                        )
                    return

                file_size = os.path.getsize(filepath)

                if quality != "audio" and file_size > MAX_FILE_SIZE_BYTES:
                    if file_size > SKIP_COMPRESSION_ABOVE_BYTES:
                        # Too large to reasonably compress on a free-tier CPU — don't even try
                        await query.edit_message_text(
                            f"This video is {file_size / (1024*1024):.0f}MB — too large to process here. "
                            "Please try 480p or audio-only instead."
                        )
                        return

                    await query.edit_message_text(
                        f"Video is {file_size / (1024*1024):.1f}MB — compressing to fit under 50MB..."
                    )
                    compressed_path = os.path.join(tmp_dir, "compressed.mp4")
                    try:
                        await asyncio.to_thread(
                            compress_video, filepath, compressed_path, MAX_FILE_SIZE_BYTES, duration
                        )
                        filepath = compressed_path
                        file_size = os.path.getsize(filepath)
                    except Exception as e:
                        logger.error(f"Compression failed: {e}")
                        await query.edit_message_text(
                            "The video is too large and compression failed. Try 480p instead."
                        )
                        return

                if file_size > MAX_FILE_SIZE_BYTES:
                    await query.edit_message_text(
                        f"Even after compression it's {file_size / (1024*1024):.1f}MB — "
                        f"still too large to send. Try 480p or audio-only instead."
                    )
                    return

                await query.edit_message_text("Sending...")

                with open(filepath, "rb") as f:
                    if quality == "audio":
                        await context.bot.send_audio(chat_id=query.message.chat_id, audio=f)
                    else:
                        await context.bot.send_video(chat_id=query.message.chat_id, video=f)

                await query.delete_message()
                increment_daily_usage(user_id)
    finally:
        active_downloads.discard(user_id)


def main():
    # Start the dummy web server in the background so Render sees a live port
    threading.Thread(target=run_web_server, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_quality_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is starting... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
