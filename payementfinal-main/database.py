import os
import gc
import ctypes
import shutil
import asyncio
from datetime import timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, utils
from pyrogram.enums import ParseMode
from pyrogram.types import Message, InlineKeyboardMarkup
from pyrogram.errors import FloodWait, MessageNotModified
from config import Config

# --- FIX FOR MODERN 13-DIGIT TELEGRAM CHANNEL IDs (-10022xxxxxxxx+) ---
utils.MIN_CHANNEL_ID = -100999999999999
utils.MAX_CHANNEL_ID = -1000000000000

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

# --- INITIALIZE BOT & PREMIUM USER CLIENT HERE (PREVENTS CIRCULAR IMPORTS) ---
app = Client(
    "AnimeFileStoreBot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    parse_mode=ParseMode.HTML,
    workers=8,
    max_concurrent_transmissions=8,
    sleep_threshold=0
)

USER_SESSION_STR = getattr(Config, "USER_SESSION_STRING", "").strip()
user_client = None
if USER_SESSION_STR:
    user_client = Client(
        "PremiumUploaderSession",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=USER_SESSION_STR,
        workers=8,
        max_concurrent_transmissions=8,
        sleep_threshold=30,
        no_updates=True
    )

mongo_client = AsyncIOMotorClient(Config.MONGO_URI)
db = mongo_client[Config.DB_NAME]

batches_col = db["batches"]          # Stores 480p, 720p, 1080p file batches permanently
channels_col = db["channels"]        # Stores keyword -> channel_id + permanent poster_file_id
settings_col = db["settings"]        # Stores dynamic bot settings
fsub_reqs_col = db["fsub_requests"]  # Tracks join requests in 'request' mode
users_col = db["users"]              # Tracks user download count for progressive FSub
payments_col = db["payments"]        # Tracks user pending QR sessions, UTRs & Screenshot hashes
bank_utrs_col = db["bank_utrs"]      # Stores auto-forwarded GPay/PhonePe/Bank UTRs

admin_upload_state = {}
admin_interactive_state = {}

PIPELINE_SEMAPHORE = asyncio.Semaphore(1)

IST = timezone(timedelta(hours=5, minutes=30))
AUTO_DELETE_SECONDS = Config.AUTO_DELETE_SECONDS

WORK_BASE_DIR = os.path.abspath("./anime_leech_work")
os.makedirs(WORK_BASE_DIR, exist_ok=True)

AUDIO_LANG_ALIASES = {
    "tamil": ["tam", "ta", "tamil", "தமிழ்"],
    "english": ["eng", "en", "english"],
    "malayalam": ["mal", "ml", "malayalam", "மலையாளம்"],
    "telugu": ["tel", "te", "telugu", "తెలుగు"],
    "hindi": ["hin", "hi", "hindi", "हिंदी"],
    "japanese": ["jpn", "ja", "jap", "japanese"]
}


def release_memory():
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


async def safe_edit_message(msg: Message, text: str, reply_markup: InlineKeyboardMarkup = None):
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await msg.edit_text(text=text, reply_markup=reply_markup)
    except (MessageNotModified, FloodWait):
        pass
    except Exception:
        pass


async def get_settings():
    doc = await settings_col.find_one({"_id": "bot_settings"})
    if not doc:
        default_fsubs = [
            {"chat_id": cid, "title": f"Channel {i+1}", "mode": Config.DEFAULT_FSUB_MODE}
            for i, cid in enumerate(Config.DEFAULT_FSUB_CHANNELS)
        ]
        default_doc = {
            "_id": "bot_settings",
            "fsub_list": default_fsubs,
            "shortener_url": Config.SHORTENER_URL,
            "shortener_api": Config.SHORTENER_API,
            "how_to_link": Config.HOW_TO_DOWNLOAD_LINK,
            "issue_link": Config.ISSUE_LINK,
            "share_link": Config.SHARE_LINK,
            "upi_id": Config.UPI_ID,
            "pay_amount": Config.PAYMENT_AMOUNT_INR,
            "raw_upi_uri": "",
            "utr_mode": "smart",
            "upload_mode": Config.DEFAULT_UPLOAD_MODE,
            "preferred_audio": Config.DEFAULT_PREFERRED_AUDIO,
            "hardburn_wm": Config.DEFAULT_HARDBURN_WM,
            "rename_format": Config.DEFAULT_RENAME_FORMAT,
            "caption_format": Config.DEFAULT_CAPTION_FORMAT,
            "main_watermark": Config.DEFAULT_MAIN_WATERMARK,
            "sub_watermark": Config.DEFAULT_SUB_WATERMARK,
            "thumb_file_id": ""
        }
        await settings_col.insert_one(default_doc)
        return default_doc

    if "fsub_list" not in doc:
        legacy_ids = doc.get("fsub_channels", [])
        legacy_mode = doc.get("fsub_mode", Config.DEFAULT_FSUB_MODE)
        fsub_list = [{"chat_id": cid, "title": f"Channel {i+1}", "mode": legacy_mode} for i, cid in enumerate(legacy_ids)]
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"fsub_list": fsub_list}})
        doc["fsub_list"] = fsub_list
    if "preferred_audio" not in doc:
        doc["preferred_audio"] = Config.DEFAULT_PREFERRED_AUDIO
    if "hardburn_wm" not in doc:
        doc["hardburn_wm"] = Config.DEFAULT_HARDBURN_WM
    return doc
    
