import os
import sys
import signal
import shutil
import asyncio

# --- 1. CONFIGURE HEROKU APT BINARY & LIBRARY PATHS BEFORE MODULE IMPORTS ---
apt_bin_dir = "/app/.apt/usr/bin"
if os.path.isdir(apt_bin_dir):
    cur_path = os.environ.get("PATH", "")
    if apt_bin_dir not in cur_path:
        os.environ["PATH"] = f"{apt_bin_dir}:{cur_path}".strip(":")

apt_pulse_dir = "/app/.apt/usr/lib/x86_64-linux-gnu/pulseaudio"
apt_lib_dir = "/app/.apt/usr/lib/x86_64-linux-gnu"
ld_parts = [p for p in (apt_pulse_dir, apt_lib_dir) if os.path.isdir(p)]
if ld_parts:
    cur_ld = os.environ.get("LD_LIBRARY_PATH", "")
    prefix_ld = ":".join(ld_parts)
    if prefix_ld not in cur_ld:
        os.environ["LD_LIBRARY_PATH"] = f"{prefix_ld}:{cur_ld}".strip(":")

# --- 2. ENABLE UVLOOP BEFORE CREATING EVENT LOOP ---
try:
    import uvloop
    uvloop.install()
except Exception:
    pass

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from config import Config  # noqa: E402
from database import app, user_client, settings_col, WORK_BASE_DIR, release_memory  # noqa: E402
from modules.uploader import MTPROTO_POOL, safe_resolve_chat  # noqa: E402

# Register all Pyrogram handlers & callbacks cleanly (zero circular imports)
import modules.settings_panel  # noqa: E402, F401
import modules.handlers        # noqa: E402, F401


def log_engine_capabilities():
    try:
        import tgcrypto  # noqa: F401
        tg_accel = "tgcrypto (C-Speed Active)"
    except ImportError:
        tg_accel = "⚠️ Missing tgcrypto (Install for 10x faster uploads)"

    aria2_status = "aria2c (16-Split Active)" if shutil.which("aria2c") else "HTTP Range (16-Part)"
    ffmpeg_status = "FFmpeg Ready" if shutil.which("ffmpeg") else "⚠️ FFmpeg Missing"
    print(f"⚡ Engine Check | Crypto: {tg_accel} | Downloader: {aria2_status} | Encoder: {ffmpeg_status}")


async def shutdown_services(stop_event: asyncio.Event):
    print("🛑 Shutting down services cleanly...")
    try:
        await MTPROTO_POOL.close_all()
    except Exception:
        pass
    if user_client and getattr(user_client, "is_connected", False):
        try:
            await user_client.stop()
        except Exception:
            pass
    if getattr(app, "is_connected", False):
        try:
            await app.stop()
        except Exception:
            pass
    shutil.rmtree(WORK_BASE_DIR, ignore_errors=True)
    release_memory()
    stop_event.set()


async def start_services():
    shutil.rmtree(WORK_BASE_DIR, ignore_errors=True)
    os.makedirs(WORK_BASE_DIR, exist_ok=True)

    log_engine_capabilities()

    await app.start()
    bot_me = await app.get_me()
    print(f"🤖 Bot Started: @{bot_me.username} (ID: {bot_me.id})")

    await settings_col.update_one(
        {"_id": "bot_settings"},
        {"$setOnInsert": {"hardburn_wm": Config.DEFAULT_HARDBURN_WM}},
        upsert=True,
    )

    if Config.DB_CHANNEL_ID:
        await safe_resolve_chat(app, app, Config.DB_CHANNEL_ID)
    if getattr(Config, "MAIN_CHANNEL_ID", None):
        await safe_resolve_chat(app, app, Config.MAIN_CHANNEL_ID)

    if user_client:
        try:
            await user_client.start()
            me = await user_client.get_me()
            is_prem = bool(getattr(me, "is_premium", False))
            max_gb = "4.00 GB" if is_prem else "2.00 GB (Non-Premium Account)"

            if Config.DB_CHANNEL_ID:
                await safe_resolve_chat(app, user_client, Config.DB_CHANNEL_ID)

            # Warm up MTProto media pipes for maximum parallel upload speed
            warm_pipes = getattr(MTPROTO_POOL, "pool_size", 12)
            await MTPROTO_POOL.get_warm_sessions(user_client, warm_pipes)
            print(
                f"💎 User Session Warmed ({warm_pipes} Pipes): {me.first_name} "
                f"(ID: {me.id} | Premium: {is_prem} | Max Upload: {max_gb})"
            )
        except Exception as e:
            print(f"⚠️ User Session failed to start (falling back to bot 2GB mode): {e}")

    release_memory()
    print("🚀 Pro Anime File Store & Multi-Host Leech Bot is running...")

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(shutdown_services(stop_event))
            )
        except NotImplementedError:
            # Windows fallback where add_signal_handler is unsupported
            pass

    await stop_event.wait()


if __name__ == "__main__":
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        pass
    finally:
        if not loop.is_closed():
            loop.close()
