import os
import html
import math
import time
import shutil
import asyncio
import aiohttp
from urllib.parse import quote, unquote, urlparse
from pyrogram import Client, utils, raw
from pyrogram.session import Session
from pyrogram.enums import ParseMode
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified
from config import Config
from database import (
    batches_col,
    admin_upload_state,
    PIPELINE_SEMAPHORE,
    WORK_BASE_DIR,
    release_memory,
    safe_edit_message,
    get_settings,
)
from modules.formatting import (
    qline,
    qcard,
    render_env_template,
    parse_anime_filename,
    apply_custom_rename,
    build_channel_post_caption,
    build_bold_file_caption,
)
from modules.poster_thumb import stamp_season_episode_on_poster
from modules.subtitles_mkv import process_video_to_mkv
from modules.leecher import download_from_url_with_progress

# Telegram size limits
BOT_API_MAX_BYTES = 2 * 1024 * 1024 * 1024          # 2.00 GB standard bot limit
PREMIUM_MAX_BYTES = 4 * 1024 * 1024 * 1024          # 4.00 GB Telegram Premium limit

# Domains whose URL paths contain opaque IDs rather than anime filenames
OPAQUE_HOST_DOMAINS = (
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
    "hubcloud",
    "gdflix",
    "tgfiles",
)


async def safe_resolve_chat(bot_app: Client, target_client: Client, chat_id):
    cid = int(chat_id)
    try:
        await target_client.get_chat(cid)
        return cid
    except Exception:
        if target_client != bot_app:
            try:
                chat_obj = await bot_app.get_chat(cid)
                if chat_obj and chat_obj.username:
                    await target_client.get_chat(chat_obj.username)
                    return cid
                inv_link = getattr(chat_obj, "invite_link", None) or await bot_app.export_chat_invite_link(cid)
                if inv_link:
                    try:
                        await target_client.join_chat(inv_link)
                    except Exception:
                        pass
                    await target_client.get_chat(cid)
                    return cid
            except Exception:
                pass
        return cid


class PersistentMTProtoPool:
    def __init__(self, pool_size: int = 12):
        self.pool_size = pool_size
        self.client: Client = None
        self.sessions: list[Session] = []
        self.lock = asyncio.Lock()

    async def get_warm_sessions(self, uploader: Client, count: int) -> list[Session]:
        async with self.lock:
            if self.client != uploader or len(self.sessions) < count:
                await self.close_all()
                self.client = uploader
                dc_id = await uploader.storage.dc_id()
                auth_key = await uploader.storage.auth_key()
                test_mode = await uploader.storage.test_mode()

                async def create_one():
                    s = Session(uploader, dc_id, auth_key, test_mode, is_media=True)
                    await s.start()
                    return s

                target_n = min(max(count, self.pool_size), 12)
                self.sessions = list(
                    await asyncio.gather(*(create_one() for _ in range(target_n)))
                )
            return self.sessions[:count]

    async def close_all(self):
        if self.sessions:
            await asyncio.gather(
                *(s.stop() for s in self.sessions), return_exceptions=True
            )
            self.sessions.clear()


MTPROTO_POOL = PersistentMTProtoPool(pool_size=12)


async def shorten_url(long_url: str) -> str:
    cfg = await get_settings()
    db_api = cfg.get("shortener_api", "")
    api_key = db_api if (db_api and db_api != "your_shortener_api_key") else Config.SHORTENER_API

    db_url = cfg.get("shortener_url", "")
    api_url = db_url if (db_url and "your_" not in db_url) else Config.SHORTENER_URL

    if not api_key or api_key == "your_shortener_api_key":
        return long_url

    api_url = api_url.strip()
    if not api_url.startswith("http"):
        api_url = "https://" + api_url
    if not api_url.endswith("/api"):
        api_url = api_url.rstrip("/") + "/api"

    params = {"api": api_key, "url": long_url}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, params=params, timeout=15) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    short_link = data.get("shortenedUrl") or data.get("short_url")
                    if short_link:
                        return short_link
    except Exception as e:
        print(f"Shortener Error: {e}")

    return long_url


async def parallel_upload_to_telegram(
    bot: Client,
    user_client: Client | None,
    db_channel_id: int,
    file_path: str,
    file_name: str,
    caption: str,
    as_video: bool = False,
    duration: int = 0,
    width: int = 1280,
    height: int = 720,
    thumb_path: str = None,
    progress_cb=None,
    num_workers: int = 12,
) -> int:
    file_size = os.path.getsize(file_path)
    use_premium = bool(user_client and getattr(user_client, "is_connected", False))

    # Enforce Telegram upload size caps clearly
    if file_size > PREMIUM_MAX_BYTES:
        raise RuntimeError(
            f"Encoded file ({file_size / (1024**3):.2f} GB) exceeds Telegram Premium 4.00 GB limit."
        )
    if file_size > BOT_API_MAX_BYTES and not use_premium:
        raise RuntimeError(
            f"File is {file_size / (1024**3):.2f} GB (>2 GB). Connect your Telegram Premium STRING_SESSION to upload up to 4 GB."
        )

    await safe_resolve_chat(bot, bot, db_channel_id)

    # Prefer Premium string session for all large/turbo uploads; fallback to bot for small files
    uploader = user_client if use_premium else bot
    direct_db_peer = None

    if use_premium:
        try:
            await safe_resolve_chat(bot, user_client, db_channel_id)
            direct_db_peer = await user_client.resolve_peer(int(db_channel_id))
        except Exception:
            direct_db_peer = await user_client.resolve_peer("me")
    else:
        direct_db_peer = await bot.resolve_peer(int(db_channel_id))

    # 512 KB is the maximum MTProto upload part size allowed by Telegram
    part_size = 512 * 1024
    total_parts = max(1, math.ceil(file_size / part_size))
    file_id = uploader.rnd_id()
    is_big = file_size > 10 * 1024 * 1024

    # Scale worker count dynamically: 12 workers for Premium sessions, 8 for Bot API
    max_allowed_workers = 12 if use_premium else 8
    actual_workers = min(num_workers, max_allowed_workers, max(1, total_parts))
    sessions = await MTPROTO_POOL.get_warm_sessions(uploader, actual_workers)

    next_part_idx = [0]
    uploaded_bytes = [0]
    upload_done_event = asyncio.Event()
    start_ts = time.time()

    async def progress_reporter():
        if not progress_cb:
            return
        while not upload_done_event.is_set():
            await asyncio.sleep(5.0)
            cur = min(uploaded_bytes[0], file_size)
            elapsed = max(0.1, time.time() - start_ts)
            spd = (cur / (1024 * 1024)) / elapsed
            try:
                await progress_cb(cur, file_size, spd)
            except Exception:
                pass

    reporter_task = asyncio.create_task(progress_reporter())
    fd = os.open(file_path, os.O_RDONLY)

    async def worker(sess: Session):
        while True:
            part_idx = next_part_idx[0]
            if part_idx >= total_parts:
                break
            next_part_idx[0] = part_idx + 1

            offset = part_idx * part_size
            chunk = os.pread(fd, part_size, offset)
            if not chunk:
                continue

            if is_big:
                rpc = raw.functions.upload.SaveBigFilePart(
                    file_id=file_id,
                    file_part=part_idx,
                    file_total_parts=total_parts,
                    bytes=chunk,
                )
            else:
                rpc = raw.functions.upload.SaveFilePart(
                    file_id=file_id,
                    file_part=part_idx,
                    bytes=chunk,
                )

            for attempt in range(6):
                try:
                    await sess.invoke(rpc, timeout=30)
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except Exception:
                    if attempt == 5:
                        raise
                    if attempt >= 2:
                        try:
                            await sess.stop()
                            await sess.start()
                        except Exception:
                            pass
                    await asyncio.sleep(0.2 * (attempt + 1))

            uploaded_bytes[0] += len(chunk)
            del chunk

    try:
        await asyncio.gather(*(worker(s) for s in sessions))
    finally:
        os.close(fd)
        upload_done_event.set()
        reporter_task.cancel()
        release_memory()

    if progress_cb:
        final_spd = (file_size / (1024 * 1024)) / max(0.1, time.time() - start_ts)
        try:
            await progress_cb(file_size, file_size, final_spd)
        except Exception:
            pass

    if is_big:
        input_file = raw.types.InputFileBig(
            id=file_id,
            parts=total_parts,
            name=file_name,
        )
    else:
        input_file = raw.types.InputFile(
            id=file_id,
            parts=total_parts,
            name=file_name,
            md5_checksum="",
        )

    input_thumb = None
    if thumb_path and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        try:
            input_thumb = await uploader.save_file(thumb_path)
        except Exception:
            input_thumb = None

    if as_video:
        attributes = [
            raw.types.DocumentAttributeVideo(
                duration=int(duration or 0),
                w=int(width or 1280),
                h=int(height or 720),
                supports_streaming=True,
            ),
            raw.types.DocumentAttributeFilename(file_name=file_name),
        ]
        mime_type = "video/mp4" if file_name.lower().endswith(".mp4") else "video/x-matroska"
    else:
        attributes = [
            raw.types.DocumentAttributeFilename(file_name=file_name),
        ]
        mime_type = "video/x-matroska"

    media = raw.types.InputMediaUploadedDocument(
        file=input_file,
        thumb=input_thumb,
        mime_type=mime_type,
        attributes=attributes,
        force_file=not as_video,
    )

    parsed_dict = await utils.parse_text_entities(uploader, caption, ParseMode.HTML, None)
    parsed_text = parsed_dict["message"]
    entities = parsed_dict.get("entities") or []

    updates = await uploader.invoke(
        raw.functions.messages.SendMedia(
            peer=direct_db_peer,
            media=media,
            message=parsed_text,
            entities=entities,
            random_id=uploader.rnd_id(),
        )
    )

    sent_msg_id = None
    for upd in getattr(updates, "updates", []):
        if isinstance(upd, (raw.types.UpdateNewChannelMessage, raw.types.UpdateNewMessage)):
            sent_msg_id = upd.message.id
            break

    # Bridge from Saved Messages ("me") to DB channel if Premium user wasn't direct member
    if use_premium and isinstance(direct_db_peer, raw.types.InputPeerSelf) and sent_msg_id:
        try:
            bot_me = await bot.get_me()
            fwd_msg = await user_client.forward_messages(bot_me.username, "me", sent_msg_id)
            copied_db = await bot.copy_message(
                chat_id=int(db_channel_id),
                from_chat_id=fwd_msg.chat.id,
                message_id=fwd_msg.id,
                caption=caption,
            )
            await user_client.delete_messages("me", sent_msg_id)
            return copied_db.id
        except Exception as e:
            print(f"Self-bridge copy notice: {e}")

    if sent_msg_id:
        return sent_msg_id

    async for m in bot.get_chat_history(int(db_channel_id), limit=1):
        return m.id


async def update_channel_live_progress(client: Client, state: dict, force: bool = False):
    edit_lock = state.get("edit_lock")
    if edit_lock and edit_lock.locked() and not force:
        return

    now = time.time()
    if not force and state.get("live_post_id") and (now - state.get("last_live_edit", 0.0) < 7.0):
        return

    async with (edit_lock or asyncio.Lock()):
        now = time.time()
        if not force and state.get("live_post_id") and (now - state.get("last_live_edit", 0.0) < 6.5):
            return
        state["last_live_edit"] = now

        anime_esc = html.escape(state["anime"] or state["keyword"].title())
        season_val = state.get("season")
        has_season = bool(
            season_val and str(season_val).strip() and str(season_val).strip().lower() != "none"
        )
        season_esc = html.escape(str(season_val)) if has_season else None
        ep_esc = html.escape(str(state["episode"]))
        audio_esc = html.escape(state["audio"])

        live_progress_text = render_env_template(
            Config.MSG_LIVE_PROGRESS,
            anime=anime_esc,
            season=season_esc,
            episode=ep_esc,
            audio=audio_esc,
            status_480p=state["status_lines"]["480p"],
            status_720p=state["status_lines"]["720p"],
            status_1080p=state["status_lines"]["1080p"],
        )

        target_ch = int(state["target_channel"])
        if not state["live_post_id"]:
            try:
                msg = await client.send_message(target_ch, live_progress_text)
                state["live_post_id"] = msg.id
            except Exception:
                pass
        else:
            try:
                await client.edit_message_text(target_ch, state["live_post_id"], live_progress_text)
            except (MessageNotModified, FloodWait):
                pass
            except Exception:
                pass


async def finalize_and_publish_batch(client: Client, admin_id: int, state: dict, dm_notify_msg: Message):
    anime_esc = html.escape(state["anime"] or state["keyword"].title())
    season_val = state.get("season")
    has_season = bool(
        season_val and str(season_val).strip() and str(season_val).strip().lower() != "none"
    )
    season_esc = html.escape(str(season_val)) if has_season else None
    ep_esc = html.escape(str(state["episode"]))
    audio_esc = html.escape(state["audio"])
    target_ch = await safe_resolve_chat(client, client, state["target_channel"])

    avail_qualities = [q for q in ["480p", "720p", "1080p"] if q in state.get("files", {})]

    batch_id = f"{int(time.time())}"
    await batches_col.insert_one({
        "_id": batch_id,
        "keyword": state["keyword"],
        "anime": state["anime"] or state["keyword"].title(),
        "season": season_val if has_season else None,
        "episode": state["episode"],
        "audio": state["audio"],
        "files": state["files"],
        "created_at": int(time.time()),
    })

    bot_uname = (await client.get_me()).username
    pay_deep_link = f"https://t.me/{bot_uname}?start=pay_{batch_id}"
    raw_all = f"https://t.me/{bot_uname}?start=all_{batch_id}"

    quality_buttons_row = []
    for q in avail_qualities:
        raw_q_url = f"https://t.me/{bot_uname}?start=get_{batch_id}_{q}"
        short_q_url = await shorten_url(raw_q_url)
        quality_buttons_row.append(InlineKeyboardButton(q, url=short_q_url))

    short_all = await shorten_url(raw_all)

    cfg = await get_settings()
    how_to_link = cfg.get("how_to_link", Config.HOW_TO_DOWNLOAD_LINK)

    final_post_text = build_channel_post_caption(
        anime_esc,
        season_esc,
        ep_esc,
        audio_esc,
        qualities_list=avail_qualities,
    )

    all_btn_label = Config.BTN_ALL_FILES.replace("{count}", str(len(avail_qualities)))
    buttons = InlineKeyboardMarkup([
        quality_buttons_row,
        [InlineKeyboardButton(all_btn_label, url=short_all)],
        [InlineKeyboardButton(Config.BTN_VIP_DIRECT, url=pay_deep_link)],
        [InlineKeyboardButton(Config.BTN_HOW_TO, url=how_to_link)],
    ])

    stamped_poster_bio = None
    if state.get("poster_file_id"):
        try:
            stamped_poster_bio = await stamp_season_episode_on_poster(
                client,
                state["poster_file_id"],
                season_val if has_season else None,
                str(state["episode"]),
            )
        except Exception as e:
            print(f"Poster Stamp Error: {e}")

    if stamped_poster_bio:
        if state["live_post_id"]:
            try:
                await client.delete_messages(target_ch, state["live_post_id"])
            except Exception:
                pass
        final_msg = await client.send_photo(
            target_ch,
            photo=stamped_poster_bio,
            caption=final_post_text,
            reply_markup=buttons,
        )
    else:
        if state["live_post_id"]:
            final_msg = await client.edit_message_text(
                target_ch,
                state["live_post_id"],
                final_post_text,
                reply_markup=buttons,
            )
        else:
            final_msg = await client.send_message(
                target_ch,
                final_post_text,
                reply_markup=buttons,
            )

    if Config.MAIN_CHANNEL_ID:
        try:
            main_ch = await safe_resolve_chat(client, client, Config.MAIN_CHANNEL_ID)
            issue_url = (
                cfg.get("issue_link")
                or Config.ISSUE_LINK
                or Config.SUPPORT_LINK
                or f"https://t.me/{bot_uname}"
            )
            raw_share_target = cfg.get("share_link") or Config.SHARE_LINK or final_msg.link
            share_text = (
                Config.MSG_SHARE_TEXT
                .replace("{anime}", str(state["anime"] or state["keyword"].title()))
                .replace("{season}", str(season_val or ""))
                .replace("{episode}", str(state["episode"]))
                .replace("{audio}", str(state["audio"]))
            )
            telegram_share_url = (
                f"https://t.me/share/url?url={quote(raw_share_target)}&text={quote(share_text)}"
            )

            main_text = build_channel_post_caption(
                anime_esc,
                season_esc,
                ep_esc,
                audio_esc,
                qualities_list=avail_qualities,
            )

            main_btn = InlineKeyboardMarkup([
                [InlineKeyboardButton(Config.BTN_MAIN_VIEW, url=final_msg.link)],
                [
                    InlineKeyboardButton(Config.BTN_MAIN_ISSUE, url=issue_url),
                    InlineKeyboardButton(Config.BTN_MAIN_SHARE, url=telegram_share_url),
                ],
            ])

            if stamped_poster_bio:
                stamped_poster_bio.seek(0)
                await client.send_photo(
                    main_ch,
                    photo=stamped_poster_bio,
                    caption=main_text,
                    reply_markup=main_btn,
                )
            else:
                await client.send_message(
                    main_ch,
                    main_text,
                    reply_markup=main_btn,
                )
        except Exception as e:
            print(f"Main Channel Update Error: {e}")

    if stamped_poster_bio:
        stamped_poster_bio.close()

    admin_upload_state.pop(admin_id, None)
    release_memory()

    q_str = " | ".join(q for q in ["480p", "720p", "1080p"] if q in avail_qualities)
    await safe_edit_message(
        dm_notify_msg,
        qcard(
            "🎉 Episode Successfully Published!",
            f"🎬 Anime : {anime_esc}",
            f"📅 Season : {season_esc}" if has_season else None,
            f"🔢 Episode : {ep_esc}",
            f"🔊 Audio : {audio_esc}",
            f"🎞️ Quality : {q_str}",
        ),
    )


async def process_leech_url_item(
    client: Client,
    user_client: Client | None,
    admin_id: int,
    message: Message,
    url: str,
    forced_slot: str = None,
):
    state = admin_upload_state.get(admin_id)
    if not state:
        return

    domain = urlparse(url).netloc.lower()
    is_opaque_host = any(h in domain for h in OPAQUE_HOST_DOMAINS)

    # Only pre-parse URLs whose path actually contains a real filename (direct HTTP links)
    if is_opaque_host:
        pre_parsed = {"anime": "", "season": None, "episode": "", "quality": ""}
    else:
        url_decoded = unquote(unquote(url))
        pre_parsed = parse_anime_filename(url_decoded)

    async with state["lock"]:
        state["active_tasks"] = state.get("active_tasks", 0) + 1
        q_slot = forced_slot or pre_parsed["quality"]
        if not q_slot or q_slot in state["claimed_slots"]:
            for slot in ["480p", "720p", "1080p"]:
                if slot not in state["claimed_slots"]:
                    q_slot = slot
                    break
        if not q_slot:
            q_slot = "720p"

        state["claimed_slots"].add(q_slot)

        if pre_parsed["anime"]:
            state["anime"] = pre_parsed["anime"]
        elif not state["anime"]:
            state["anime"] = state["keyword"].title()

        if pre_parsed["season"] is not None:
            state["season"] = pre_parsed["season"]
        if pre_parsed["episode"]:
            state["episode"] = pre_parsed["episode"]

        state["status_lines"][q_slot] = f"⏳ {q_slot.upper()} : Queued..."
        await update_channel_live_progress(client, state, force=True)

    premium_badge = "💎 Premium 4GB" if (user_client and getattr(user_client, "is_connected", False)) else "⚡ Standard"
    dm_status = await message.reply_text(
        qline(f"⏳ Queued {q_slot.upper()} for Turbo Leech ({premium_badge})...")
    )
    job_dir = os.path.join(WORK_BASE_DIR, f"job_{q_slot}_{int(time.time()*1000)}")
    os.makedirs(job_dir, exist_ok=True)

    try:
        async with PIPELINE_SEMAPHORE:
            state["status_lines"][q_slot] = f"📥 {q_slot.upper()} : Resolving & Downloading..."
            await update_channel_live_progress(client, state, force=True)
            await safe_edit_message(dm_status, qline(f"📥 Resolving & Downloading {q_slot.upper()}..."))

            last_dl_dm = [0.0]

            async def on_dl_progress(done_bytes: int, total_bytes: int, speed_mbps: float = 0.0):
                done_mb = round(done_bytes / (1024 * 1024), 1)
                spd_str = f" ⚡ {speed_mbps:.1f} MB/s" if speed_mbps > 0 else ""
                if total_bytes > 0:
                    tot_mb = round(total_bytes / (1024 * 1024), 1)
                    pct = int((done_bytes / total_bytes) * 100)
                    state["status_lines"][q_slot] = (
                        f"📥 {q_slot.upper()} : {done_mb}/{tot_mb} MB ({pct}%){spd_str}"
                    )
                else:
                    state["status_lines"][q_slot] = f"📥 {q_slot.upper()} : {done_mb} MB{spd_str}"
                await update_channel_live_progress(client, state)
                now = time.time()
                if now - last_dl_dm[0] >= 5.0:
                    last_dl_dm[0] = now
                    await safe_edit_message(dm_status, qline(state["status_lines"][q_slot]))

            # Pass user_client if supported by leecher.py, fallback gracefully if signature omits it
            try:
                raw_path, orig_filename = await download_from_url_with_progress(
                    url, job_dir, progress_cb=on_dl_progress, user_client=user_client
                )
            except TypeError:
                raw_path, orig_filename = await download_from_url_with_progress(
                    url, job_dir, progress_cb=on_dl_progress
                )
            release_memory()

            # Parse clean filename returned by leecher (Drive, HubCloud, GDFlix, TGFiles, HTTP)
            parsed = parse_anime_filename(orig_filename)
            cfg = await get_settings()
            preferred_audio = cfg.get("preferred_audio", Config.DEFAULT_PREFERRED_AUDIO).strip()

            async with state["lock"]:
                if parsed["anime"]:
                    state["anime"] = parsed["anime"]
                if parsed["season"] is not None:
                    state["season"] = parsed["season"]
                if parsed["episode"]:
                    state["episode"] = parsed["episode"]
                if preferred_audio and preferred_audio.lower() != "all":
                    state["audio"] = preferred_audio.title()
                elif parsed["audio"]:
                    state["audio"] = parsed["audio"]

            main_wm = (cfg.get("main_watermark") or Config.DEFAULT_MAIN_WATERMARK).strip()
            sub_wm = (cfg.get("sub_watermark") or Config.DEFAULT_SUB_WATERMARK).strip()
            hardburn_wm = bool(cfg.get("hardburn_wm", Config.DEFAULT_HARDBURN_WM))

            temp_out_mkv = os.path.join(job_dir, f"processed_{q_slot}.mkv")
            state["status_lines"][q_slot] = (
                f"⚙️ {q_slot.upper()} : Extracting {state['audio']} Audio & Burning Watermarks..."
            )
            await update_channel_live_progress(client, state, force=True)
            await safe_edit_message(
                dm_status,
                qcard(
                    f"⚙️ Processing {q_slot.upper()}",
                    f"📁 Source : {html.escape(orig_filename)}",
                    f"🔊 Audio : {html.escape(state['audio'])} Only",
                    f"💧 Top-Right WM : {html.escape(main_wm or 'Off')}",
                    f"💬 First/Last 30s Sub WM : {html.escape(sub_wm or 'Off')}",
                ),
            )

            final_mkv_path, duration_sec, width, height, extracted_lang = await process_video_to_mkv(
                input_path=raw_path,
                output_mkv_path=temp_out_mkv,
                main_watermark=main_wm,
                sub_watermark=sub_wm,
                preferred_audio_lang=preferred_audio,
                hardburn_video_wm=hardburn_wm,
                temp_dir=job_dir,
            )

            async with state["lock"]:
                if extracted_lang:
                    state["audio"] = extracted_lang

            renamed_mkv = apply_custom_rename(
                cfg.get("rename_format", Config.DEFAULT_RENAME_FORMAT),
                state["anime"] or state["keyword"].title(),
                state["season"],
                state["episode"],
                state["audio"],
                q_slot,
            )
            renamed_mkv_path = os.path.join(job_dir, renamed_mkv)
            shutil.move(final_mkv_path, renamed_mkv_path)
            final_mkv_path = renamed_mkv_path

            release_memory()

            size_bytes = os.path.getsize(final_mkv_path)
            size_mb = round(size_bytes / (1024 * 1024), 2)
            state["status_lines"][q_slot] = f"📤 {q_slot.upper()} : Uploading ({size_mb} MB)..."
            await update_channel_live_progress(client, state, force=True)
            await safe_edit_message(
                dm_status, qline(f"💎 Uploading {renamed_mkv} ({size_mb} MB)...")
            )

            thumb_path = None
            thumb_fid = cfg.get("thumb_file_id", "")
            if thumb_fid:
                try:
                    thumb_path = await client.download_media(
                        thumb_fid, file_name=os.path.join(job_dir, "thumb.jpg")
                    )
                except Exception as e:
                    print(f"Thumb download error: {e}")

            file_cap = build_bold_file_caption(
                state["anime"] or state["keyword"].title(),
                state["season"],
                state["episode"],
                state["audio"],
                q_slot,
                custom_template=cfg.get("caption_format", Config.DEFAULT_CAPTION_FORMAT),
            )

            db_ch = int(Config.DB_CHANNEL_ID)
            as_video = cfg.get("upload_mode", Config.DEFAULT_UPLOAD_MODE) == "video"
            last_up_dm = [0.0]

            async def on_tg_upload_progress(current: int, total: int, speed_mbps: float = 0.0):
                if total > 0:
                    pct = int((current / total) * 100)
                    cur_mb = round(current / (1024 * 1024), 1)
                    spd_txt = f" ⚡ {speed_mbps:.1f} MB/s" if speed_mbps > 0 else ""
                    state["status_lines"][q_slot] = (
                        f"📤 {q_slot.upper()} : {cur_mb}/{size_mb} MB ({pct}%){spd_txt}"
                    )
                    await update_channel_live_progress(client, state)
                    now = time.time()
                    if now - last_up_dm[0] >= 5.0:
                        last_up_dm[0] = now
                        await safe_edit_message(
                            dm_status,
                            qline(
                                f"💎 Uploading {q_slot.upper()} : {cur_mb}/{size_mb} MB ({pct}%){spd_txt}"
                            ),
                        )

            stored_msg_id = await parallel_upload_to_telegram(
                bot=client,
                user_client=user_client,
                db_channel_id=db_ch,
                file_path=final_mkv_path,
                file_name=renamed_mkv,
                caption=file_cap,
                as_video=as_video,
                duration=duration_sec,
                width=width,
                height=height,
                thumb_path=thumb_path,
                progress_cb=on_tg_upload_progress,
                num_workers=12,
            )

            shutil.rmtree(job_dir, ignore_errors=True)
            release_memory()

        async with state["lock"]:
            state["active_tasks"] = max(0, state.get("active_tasks", 1) - 1)
            state["files"][q_slot] = {"msg_id": stored_msg_id, "size": size_mb}
            state["status_lines"][q_slot] = f"✅ {q_slot.upper()} : Uploaded ({size_mb} MB)"
            await update_channel_live_progress(client, state, force=True)

            uploaded_count = len(state["files"])
            if uploaded_count < 3:
                remaining = [
                    q.upper() for q in ["480p", "720p", "1080p"] if q not in state["files"]
                ]
                pub_markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f"🚀 Publish Now ({uploaded_count} File{'s' if uploaded_count > 1 else ''})",
                            callback_data="pubnow",
                        )
                    ]
                ])
                has_s = bool(
                    state.get("season")
                    and str(state["season"]).strip()
                    and str(state["season"]).lower() != "none"
                )
                await safe_edit_message(
                    dm_status,
                    qcard(
                        f"✅ {q_slot.upper()} Uploaded ({size_mb} MB) — ({uploaded_count}/3)",
                        f"🎬 Anime : {html.escape(state['anime'])}",
                        f"📅 Season : {html.escape(str(state['season']))}" if has_s else None,
                        f"🔢 Episode : {html.escape(str(state['episode']))}",
                        f"🔊 Audio : {html.escape(state['audio'])}",
                        f"📁 File : {html.escape(renamed_mkv)}",
                        f"📤 Remaining : {', '.join(remaining)}",
                    ),
                    reply_markup=pub_markup,
                )
            else:
                await safe_edit_message(
                    dm_status, qline("🔗 Generating Shortener Links & Publishing...")
                )
                await finalize_and_publish_batch(client, admin_id, state, dm_status)

    except Exception as e:
        async with state["lock"]:
            state["active_tasks"] = max(0, state.get("active_tasks", 1) - 1)
            state["claimed_slots"].discard(q_slot)
            state["status_lines"][q_slot] = f"❌ {q_slot.upper()} : Failed"
            await update_channel_live_progress(client, state, force=True)
        await safe_edit_message(
            dm_status,
            qline(f"❌ Error processing {q_slot.upper()} link: {html.escape(str(e))}"),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        release_memory()
