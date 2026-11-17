import os
from dotenv import load_dotenv

load_dotenv()


def _env_lines(key: str, default: str) -> str:
    """Reads multi-line strings from .env where lines are separated by \\n or |."""
    val = os.environ.get(key, default)
    return val.replace("\\n", "\n")


class Config:
    # --- 1. CORE CREDENTIALS ---
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    USER_SESSION_STRING = os.environ.get("USER_SESSION_STRING", "")

    MONGO_URI = os.environ.get("MONGO_URI", "")
    DB_NAME = os.environ.get("DB_NAME", "AnimeFileStoreBot")

    ADMINS = [
        int(x) for x in os.environ.get("ADMINS", "").replace(",", " ").split()
        if x.strip().lstrip("-").isdigit()
    ]
    DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "0"))
    MAIN_CHANNEL_ID = int(os.environ.get("MAIN_CHANNEL_ID", "0"))

    # --- 2. PROCESS, WATERMARK & AUDIO DEFAULTS ---
    DEFAULT_FSUB_CHANNELS = [
        int(x) for x in os.environ.get("DEFAULT_FSUB_CHANNELS", "").replace(",", " ").split()
        if x.strip().lstrip("-").isdigit()
    ]
    DEFAULT_FSUB_MODE = os.environ.get("DEFAULT_FSUB_MODE", "join")
    BASE_FSUB_COUNT = int(os.environ.get("BASE_FSUB_COUNT", "4"))
    AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", "300"))

    DEFAULT_UPLOAD_MODE = os.environ.get("DEFAULT_UPLOAD_MODE", "document").lower()
    DEFAULT_PREFERRED_AUDIO = os.environ.get("DEFAULT_PREFERRED_AUDIO", "Tamil")
    DEFAULT_HARDBURN_WM = os.environ.get("DEFAULT_HARDBURN_WM", "True").lower() in ("true", "1", "yes")
    DEFAULT_MAIN_WATERMARK = os.environ.get("DEFAULT_MAIN_WATERMARK", "")
    DEFAULT_SUB_WATERMARK = os.environ.get("DEFAULT_SUB_WATERMARK", "")
    DEFAULT_RENAME_FORMAT = os.environ.get("DEFAULT_RENAME_FORMAT", "")
    DEFAULT_CAPTION_FORMAT = _env_lines("DEFAULT_CAPTION_FORMAT", "")

    # --- 3. SHORTENER, LINKS & UPI PAYMENT ---
    SHORTENER_URL = os.environ.get("SHORTENER_URL", "https://api.gplinks.com/api")
    SHORTENER_API = os.environ.get("SHORTENER_API", "your_shortener_api_key")
    HOW_TO_DOWNLOAD_LINK = os.environ.get("HOW_TO_DOWNLOAD_LINK", "https://t.me/AniFlixTamil")
    ISSUE_LINK = os.environ.get("ISSUE_LINK", "")
    SHARE_LINK = os.environ.get("SHARE_LINK", "")

    UPI_ID = os.environ.get("UPI_ID", "yourupi@okaxis")
    PAYMENT_AMOUNT_INR = int(os.environ.get("PAYMENT_AMOUNT_INR", "1"))
    OCR_API_KEY = os.environ.get("OCR_API_KEY", "helloworld")

    START_IMG_URL = os.environ.get("START_IMG_URL", "")
    SUPPORT_LINK = os.environ.get("SUPPORT_LINK", "https://t.me/AniFlixTamil")
    UPDATE_LINK = os.environ.get("UPDATE_LINK", "https://t.me/AniFlixTamil")
    OWNER_LINK = os.environ.get("OWNER_LINK", "https://t.me/AniFlixTamil")

    # --- 4. MESSAGE STYLE TEMPLATES FROM .ENV ---
    MSG_START_TEXT = _env_lines(
        "MSG_START_TEXT",
        "⚡ Advanced Pro File Store + Auto Channel Uploader Bot\n"
        "📦 Permanent Cloud File Storage & Multi-Quality Batching (480p | 720p | 1080p)\n"
        "🔊 Smart Multi-Audio Tamil Extractor, Custom Rename & Top-Right Poster Editor\n"
        "💧 First & Last 30s Sub Watermark & Permanent Top-Right Video Watermark\n"
        "💎 Progressive Force-Subscribe & Instant Auto-Verified ₹1 VIP Unlock",
    )
    MSG_SETTINGS_DASHBOARD = _env_lines(
        "MSG_SETTINGS_DASHBOARD",
        "⚙️ Advanced Bot Settings Dashboard\n"
        "🎬 Saved Anime Channels : {ch_count}\n"
        "🛡️ Force-Sub Channels : {fsub_count} (First {base_fsub} Base + Progressive)\n"
        "📦 Upload Type : {up_mode} | 🔊 Audio : {pref_audio} Only\n"
        "🖼️ File Thumbnail : {thumb_status}\n"
        "✏️ Rename : {rename_status}\n"
        "📝 Caption : {cap_status}\n"
        "💧 Top-Right WM : {main_wm} | 💬 30s Sub WM : {sub_wm}\n"
        "🔗 Shortener : {shortener_url}\n"
        "🏦 UPI ID : {upi_id} (₹{pay_amount})\n"
        "⚠️ Issue Button : {issue_set} | 📤 Share Button : {share_set}\n"
        "⬇️ Select Any Setting Below To Configure :",
    )

    MSG_LIVE_PROGRESS = _env_lines(
        "MSG_LIVE_PROGRESS",
        "⚡ Uploading New Episode...\n"
        "🎬 Anime : {anime}\n"
        "📅 Season : {season}\n"
        "🔢 Episode : {episode}\n"
        "🔊 Audio : {audio}\n"
        "🎞️ Quality : 480p | 720p | 1080p\n"
        "{status_480p}\n"
        "{status_720p}\n"
        "{status_1080p}",
    )

    MSG_CHANNEL_POST = _env_lines(
        "MSG_CHANNEL_POST",
        "✨ New Episode Uploaded!\n"
        "🎬 Anime : {anime}\n"
        "📅 Season : {season}\n"
        "🔢 Episode : {episode}\n"
        "🔊 Audio : {audio}\n"
        "🎞️ Quality : {qualities}\n"
        "⬇️ Select Your Quality Below To Download :",
    )

    MSG_FSUB_RESTRICTED = _env_lines(
        "MSG_FSUB_RESTRICTED",
        "⚠️ Access Restricted — Join Required Channels\n"
        "📢 Join (or send a join request to) all the channels below to unlock your anime files.\n"
        "🔄 Click each channel button below to join, then tap Try Again.",
    )

    MSG_AUTO_DELETE_WARN = _env_lines(
        "MSG_AUTO_DELETE_WARN",
        "⏳ Important Notice — Auto-Delete in {minutes} Minutes!\n"
        "⚠️ This file will be automatically deleted in {minutes} minutes.\n"
        "💾 Please forward it to your Saved Messages right now to keep it permanently!",
    )

    MSG_AUTO_DELETED = _env_lines(
        "MSG_AUTO_DELETED",
        "🗑️ File Automatically Deleted ({minutes} Minutes Expired)\n"
        "🛡️ To protect files from copyright, delivered files are deleted after {minutes} minutes.\n"
        "📥 Click Get File below to unlock and receive the file again.",
    )

    MSG_VIP_PAYMENT = _env_lines(
        "MSG_VIP_PAYMENT",
        "💎 Automatic Instant VIP Unlock!\n"
        "🎬 Anime : {anime}\n"
        "📅 Season : {season}\n"
        "🔢 Episode : {episode}\n"
        "🔊 Audio : {audio}\n"
        "🎞️ Quality : {qualities}\n"
        "💰 Amount : ₹{amount}.00 Only\n"
        "🏦 UPI ID : {upi_id}\n"
        "📲 Scan QR & Pay ₹{amount} on PhonePe, GPay, or Paytm\n"
        "📤 Send Payment Screenshot OR 12-Digit UTR Here To Get Files Automatically!",
    )

    MSG_PAYMENT_VERIFIED = _env_lines(
        "MSG_PAYMENT_VERIFIED",
        "✅ Payment Verified & Approved!\n"
        "🧾 Verified ID : {utr} (₹{amount}.00)\n"
        "🔑 Receipt ID : {verify_code}\n"
        "📥 Delivering your files automatically right now...",
    )

    MSG_SHARE_TEXT = os.environ.get(
        "MSG_SHARE_TEXT",
        "Watch {anime} Episode {episode} ({audio})!",
    )

    # --- 5. BUTTON LABELS FROM .ENV ---
    BTN_SUPPORT = os.environ.get("BTN_SUPPORT", "💬 Support Channel")
    BTN_UPDATE = os.environ.get("BTN_UPDATE", "📢 Update Channel")
    BTN_OWNER = os.environ.get("BTN_OWNER", "👑 Owner")
    BTN_SETTINGS = os.environ.get("BTN_SETTINGS", "⚙️ Settings")

    BTN_ALL_FILES = os.environ.get("BTN_ALL_FILES", "📦 All {count} Files (Single Link)")
    BTN_VIP_DIRECT = os.environ.get("BTN_VIP_DIRECT", "💎 Direct Files (VIP Payment)")
    BTN_HOW_TO = os.environ.get("BTN_HOW_TO", "❓ How to Download")
    BTN_GET_FILE = os.environ.get("BTN_GET_FILE", "📥 Get File")
    BTN_TRY_AGAIN = os.environ.get("BTN_TRY_AGAIN", "🔄 Joined! Try Again")
    BTN_CANCEL_PAY = os.environ.get("BTN_CANCEL_PAY", "❌ Cancel Payment")

    BTN_MAIN_VIEW = os.environ.get("BTN_MAIN_VIEW", "👀 Click Here to View")
    BTN_MAIN_ISSUE = os.environ.get("BTN_MAIN_ISSUE", "⚠️ Any Issue")
    BTN_MAIN_SHARE = os.environ.get("BTN_MAIN_SHARE", "📤 Share Channel")
