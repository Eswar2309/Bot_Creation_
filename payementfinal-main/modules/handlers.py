import re
import html
import time
import asyncio
from urllib.parse import urlparse, parse_qsl
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from config import Config
from database import (
    app,
    user_client,
    batches_col,
    channels_col,
    settings_col,
    fsub_reqs_col,
    payments_col,
    admin_upload_state,
    admin_interactive_state,
    release_memory,
    safe_edit_message,
    get_settings,
)
from modules.formatting import qline, qcard, render_env_template, parse_anime_filename
from modules.poster_thumb import stamp_season_episode_on_poster
from modules.uploader import (
    safe_resolve_chat,
    update_channel_live_progress,
    finalize_and_publish_batch,
    process_leech_url_item,
)
from modules.fsub_delivery import check_user_fsub, deliver_files_with_5min_timer
from modules.payments import (
    compute_image_fingerprints,
    extract_valid_tx_id_from_text,
    extract_utr_from_payment_screenshot,
    is_valid_upi_transaction_id,
    inject_amount_into_upi_uri,
    generate_qr_image_bytes,
    decode_qr_from_file_bytes,
    send_auto_1rs_qr_payment,
    verify_and_approve_utr,
)
from modules.settings_panel import process_admin_interactive_input

BOT_API_MAX_BYTES = 2 * 1024 * 1024 * 1024
PREMIUM_MAX_BYTES = 4 * 1024 * 1024 * 1024


def is_premium_session_active() -> bool:
    return bool(user_client and getattr(user_client, "is_connected", False))


def extract_clean_urls(text: str) -> list[str]:
    """Extracts clean HTTP/HTTPS URLs (Drive, HubCloud, GDFlix, TGFiles, Direct HTTP) without trailing punctuation."""
    raw_urls = re.findall(r"https?://[^\s<>\"'`]+", text)
    cleaned = []
    for u in raw_urls:
        u = u.rstrip(".,;:!?)]}")
        if u and u not in cleaned:
            cleaned.append(u)
    return cleaned


@app.on_chat_join_request()
async def record_join_request(client: Client, request: ChatJoinRequest):
    await fsub_reqs_col.update_one(
        {"user_id": request.from_user.id, "chat_id": request.chat.id},
        {"$set": {"timestamp": int(time.time())}},
        upsert=True,
    )


def build_start_text_and_buttons(user_id: int):
    text = render_env_template(Config.MSG_START_TEXT, user_id=user_id)

    btn_rows = [
        [
            InlineKeyboardButton(Config.BTN_SUPPORT, url=Config.SUPPORT_LINK),
            InlineKeyboardButton(Config.BTN_UPDATE, url=Config.UPDATE_LINK),
        ],
        [
            InlineKeyboardButton(Config.BTN_OWNER, url=Config.OWNER_LINK),
            InlineKeyboardButton(Config.BTN_SETTINGS, callback_data="stg_main"),
        ],
    ]
    return text, InlineKeyboardMarkup(btn_rows)


@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    user_id = message.from_user.id
    payload = message.command[1] if len(message.command) > 1 else None

    missing_fsub_btns = await check_user_fsub(client, user_id)
    if missing_fsub_btns:
        if payload:
            bot_uname = (await client.get_me()).username
            retry_url = f"https://t.me/{bot_uname}?start={payload}"
            missing_fsub_btns.append(
                [InlineKeyboardButton(Config.BTN_TRY_AGAIN, url=retry_url)]
            )

        await message.reply_text(
            render_env_template(Config.MSG_FSUB_RESTRICTED),
            reply_markup=InlineKeyboardMarkup(missing_fsub_btns),
        )
        return

    if payload:
        if payload.startswith("get_"):
            parts = payload.split("_")
            if len(parts) == 3:
                _, batch_id, quality = parts
                batch = await batches_col.find_one({"_id": batch_id})
                if not batch or quality not in batch.get("files", {}):
                    await message.reply_text(
                        qline("❌ File not found or link has expired.")
                    )
                    return

                await deliver_files_with_5min_timer(
                    client=client,
                    chat_id=message.chat.id,
                    user_id=user_id,
                    batch=batch,
                    qualities=[quality],
                    retry_payload=f"get_{batch_id}_{quality}",
                )
                return

        elif payload.startswith("all_"):
            batch_id = payload.split("_", 1)[1]
            batch = await batches_col.find_one({"_id": batch_id})
            if not batch:
                await message.reply_text(
                    qline("❌ Batch not found or link has expired.")
                )
                return

            avail_qualities = [
                q for q in ["480p", "720p", "1080p"] if q in batch.get("files", {})
            ]
            await deliver_files_with_5min_timer(
                client=client,
                chat_id=message.chat.id,
                user_id=user_id,
                batch=batch,
                qualities=avail_qualities,
                retry_payload=f"all_{batch_id}",
            )
            return

        elif payload.startswith("pay_"):
            batch_id = payload.split("_", 1)[1]
            await send_auto_1rs_qr_payment(client, message, batch_id)
            return

    start_text, start_markup = build_start_text_and_buttons(user_id)
    if getattr(Config, "START_IMG_URL", "") and Config.START_IMG_URL.startswith("http"):
        try:
            await message.reply_photo(
                photo=Config.START_IMG_URL,
                caption=start_text,
                reply_markup=start_markup,
            )
            return
        except Exception:
            pass

    await message.reply_text(start_text, reply_markup=start_markup)


@app.on_callback_query(filters.regex("^pay_cancel$"))
async def cancel_vip_payment_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    await payments_col.delete_many({"user_id": user_id, "status": "waiting_utr"})
    try:
        await query.message.delete()
    except Exception:
        pass
    await client.send_message(
        chat_id=user_id,
        text=qline("❌ VIP Payment process cancelled. You are back to normal mode."),
    )
    await query.answer("Payment Cancelled!")


@app.on_message(filters.photo & ~filters.user(Config.ADMINS) & filters.private)
async def handle_user_payment_screenshot(client: Client, message: Message):
    user_id = message.from_user.id

    missing_fsub_btns = await check_user_fsub(client, user_id)
    if missing_fsub_btns:
        await message.reply_text(
            render_env_template(Config.MSG_FSUB_RESTRICTED),
            reply_markup=InlineKeyboardMarkup(missing_fsub_btns),
        )
        return

    pending_pay = await payments_col.find_one(
        {"user_id": user_id, "status": "waiting_utr"}
    )
    if not pending_pay:
        await message.reply_text(
            qcard(
                "⚠️ No Active Payment Session!",
                "💎 Click the Direct Files (VIP Payment) button in the channel post first.",
            )
        )
        return

    scan_msg = await message.reply_text(
        qline("🔍 Scanning Payment Screenshot & Verifying Transaction...")
    )
    photo_io = await client.download_media(message.photo.file_id, in_memory=True)
    raw_bytes = photo_io.getvalue()
    photo_io.close()

    img_sha256, img_dhash = compute_image_fingerprints(raw_bytes)

    dup_img = await payments_col.find_one(
        {
            "$or": [
                {"img_sha256": img_sha256, "status": "verified"},
                {"img_dhash": img_dhash, "status": "verified"},
            ]
        }
    )
    if dup_img:
        await safe_edit_message(
            scan_msg,
            qcard(
                "❌ Duplicate Payment Screenshot Detected!",
                f"🚫 This screenshot was already used (ID: {html.escape(str(dup_img.get('utr', 'Used')))}).",
                "⚠️ Sending duplicate screenshots is strictly blocked.",
            ),
        )
        return

    caption_tx = extract_valid_tx_id_from_text(message.caption or "")
    if caption_tx:
        extracted_utr = caption_tx
    else:
        extracted_utr, _ = await extract_utr_from_payment_screenshot(raw_bytes)

    if not extracted_utr:
        await safe_edit_message(
            scan_msg,
            qcard(
                "⚠️ Could Not Read UTR From Image Clearly!",
                "📲 Please copy and send your 12-digit UTR or PhonePe / GPay Transaction ID as text right here.",
            ),
        )
        return

    try:
        await scan_msg.delete()
    except Exception:
        pass
    await verify_and_approve_utr(
        client,
        message,
        user_id,
        extracted_utr,
        img_sha256=img_sha256,
        img_dhash=img_dhash,
    )


@app.on_message(filters.command("upload") & filters.user(Config.ADMINS) & filters.private)
async def start_upload_buttons(client: Client, message: Message):
    channels = await channels_col.find().to_list(100)
    if not channels:
        await message.reply_text(
            qcard(
                "⚠️ No Channels Added Yet!",
                "⚙️ Open /settings -> Manage Channels -> Add New Channel first.",
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⚙️ Open Settings", callback_data="stg_channels")]]
            ),
        )
        return

    btn_rows = []
    row = []
    for ch in channels:
        kw = ch["keyword"]
        row.append(InlineKeyboardButton(f"🎬 {kw.title()}", callback_data=f"selch_{kw}"))
        if len(row) == 2:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)

    btn_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="selch_cancel")])

    prem_status = "💎 Connected (Up to 4GB Turbo)" if is_premium_session_active() else "⚡ Standard Bot (2GB)"
    await message.reply_text(
        qcard(
            "📤 Select Target Channel To Upload",
            f"🚀 Premium Engine : {prem_status}",
            "🎬 Click any channel button below, then directly send your links (Drive, HubCloud, GDFlix, TGFiles, HTTP) or files (480p, 720p, 1080p).",
        ),
        reply_markup=InlineKeyboardMarkup(btn_rows),
    )


@app.on_callback_query(filters.regex("^selch_") & filters.user(Config.ADMINS))
async def select_upload_channel_callback(client: Client, query: CallbackQuery):
    kw = query.data.split("_", 1)[1]
    if kw == "cancel":
        admin_upload_state.pop(query.from_user.id, None)
        await safe_edit_message(query.message, qline("❌ Upload Cancelled."))
        return

    ch_doc = await channels_col.find_one({"keyword": kw})
    if not ch_doc:
        await query.answer("❌ Channel not found!", show_alert=True)
        return

    poster_status = (
        "Saved (Top-Right Season & Episode Stamp)"
        if ch_doc.get("poster_file_id")
        else "None (Add in /settings)"
    )
    target_cid = int(ch_doc["channel_id"])
    await safe_resolve_chat(client, client, target_cid)

    cfg = await get_settings()
    pref_audio = cfg.get("preferred_audio", Config.DEFAULT_PREFERRED_AUDIO)
    prem_status = "💎 Active (4GB Limit)" if is_premium_session_active() else "⚡ Bot Mode (2GB Limit)"

    admin_upload_state[query.from_user.id] = {
        "keyword": kw,
        "target_channel": target_cid,
        "channel_title": ch_doc.get("title", kw.title()),
        "poster_file_id": ch_doc.get("poster_file_id"),
        "anime": None,
        "season": None,
        "episode": "1",
        "audio": pref_audio if pref_audio != "All" else "Tamil",
        "files": {},
        "claimed_slots": set(),
        "active_tasks": 0,
        "status_lines": {
            "480p": "📥 480P : Waiting...",
            "720p": "📥 720P : Waiting...",
            "1080p": "📥 1080P : Waiting...",
        },
        "live_post_id": None,
        "last_live_edit": 0.0,
        "edit_lock": asyncio.Lock(),
        "lock": asyncio.Lock(),
    }

    await safe_edit_message(
        query.message,
        qcard(
            f"✅ Selected Channel — {html.escape(ch_doc.get('title', kw.title()))}",
            f"🖼️ Channel Poster : {poster_status}",
            f"🔊 Audio Mode : {html.escape(pref_audio)} Only",
            f"💎 Session Status : {prem_status}",
            "📥 Send GDrive, HubCloud, GDFlix, TGFiles, HTTP links or direct files (480p, 720p, 1080p).",
        ),
    )
    await query.answer(f"Selected {kw.title()}!")


@app.on_callback_query(filters.regex("^pubnow$") & filters.user(Config.ADMINS))
async def publish_partial_batch_callback(client: Client, query: CallbackQuery):
    admin_id = query.from_user.id
    state = admin_upload_state.get(admin_id)
    if not state or not state.get("files"):
        await query.answer("⚠️ No uploaded files ready to publish yet!", show_alert=True)
        return
    if state.get("active_tasks", 0) > 0:
        await query.answer(
            "⏳ Wait for the active download/upload to finish first!", show_alert=True
        )
        return

    await query.answer("🚀 Publishing batch now...")
    await safe_edit_message(
        query.message, qline("🔗 Generating Shortener Links & Publishing...")
    )
    await finalize_and_publish_batch(client, admin_id, state, query.message)


@app.on_message(filters.command("done") & filters.user(Config.ADMINS) & filters.private)
async def done_publish_command(client: Client, message: Message):
    admin_id = message.from_user.id
    state = admin_upload_state.get(admin_id)
    if not state or not state.get("files"):
        await message.reply_text(
            qline("⚠️ No uploaded files in the current session to publish.")
        )
        return
    if state.get("active_tasks", 0) > 0:
        await message.reply_text(
            qline("⏳ Please wait for the active download/upload to finish first!")
        )
        return

    status_msg = await message.reply_text(
        qline("🔗 Generating Shortener Links & Publishing...")
    )
    await finalize_and_publish_batch(client, admin_id, state, status_msg)


@app.on_message(filters.command("cancel") & filters.user(Config.ADMINS) & filters.private)
async def cancel_upload_batch(client: Client, message: Message):
    admin_upload_state.pop(message.from_user.id, None)
    admin_interactive_state.pop(message.from_user.id, None)
    release_memory()
    await message.reply_text(qline("❌ Active session cancelled."))


@app.on_message(filters.photo & filters.user(Config.ADMINS) & filters.private)
async def handle_admin_photos(client: Client, message: Message):
    admin_id = message.from_user.id
    st = admin_interactive_state.get(admin_id)
    if not st:
        return

    if st.get("action") == "wait_file_thumb":
        admin_interactive_state.pop(admin_id, None)
        thumb_fid = message.photo.file_id
        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"thumb_file_id": thumb_fid}},
            upsert=True,
        )
        await message.reply_photo(
            photo=thumb_fid,
            caption=qcard(
                "✅ Custom MKV File Thumbnail Saved!",
                "🖼️ This thumbnail will automatically be applied to all renamed .mkv files.",
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_thumb")]]
            ),
        )
        return

    if st.get("action") == "pos_wait_photo":
        kw = st["keyword"]
        admin_interactive_state.pop(admin_id, None)
        poster_fid = message.photo.file_id

        await channels_col.update_one(
            {"keyword": kw},
            {"$set": {"poster_file_id": poster_fid}},
            upsert=True,
        )

        preview_bio = await stamp_season_episode_on_poster(
            client, poster_fid, "18", "385"
        )
        await message.reply_photo(
            photo=preview_bio,
            caption=qcard(
                f"✅ Permanent Poster Saved — {html.escape(kw.title())}",
                "📐 Season and Episode numbers will automatically be stamped at the Top-Right corner on every upload.",
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_posters")]]
            ),
        )
        preview_bio.close()
        release_memory()
        return

    if st.get("action") == "wait_upi_qr":
        admin_interactive_state.pop(admin_id, None)
        status = await message.reply_text(
            qline("🔍 Reading your QR code and injecting ₹1.00...")
        )
        cfg = await get_settings()
        amt = cfg.get("pay_amount", Config.PAYMENT_AMOUNT_INR)

        photo_io = await client.download_media(message.photo.file_id, in_memory=True)
        decoded_uri = await decode_qr_from_file_bytes(photo_io.getvalue())
        photo_io.close()

        if not decoded_uri or not decoded_uri.lower().startswith("upi://"):
            await safe_edit_message(
                status,
                qline(
                    "⚠️ Could not read UPI link from that photo. Try cropping closer to the QR square!"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_upi")]]
                ),
            )
            return

        parsed = urlparse(decoded_uri)
        params = dict(parse_qsl(parsed.query))
        extracted_pa = params.get("pa", cfg.get("upi_id"))

        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"raw_upi_uri": decoded_uri, "upi_id": extracted_pa}},
            upsert=True,
        )

        injected_uri = inject_amount_into_upi_uri(decoded_uri, amt)
        preview_qr = generate_qr_image_bytes(injected_uri)

        try:
            await status.delete()
        except Exception:
            pass
        await message.reply_photo(
            photo=preview_qr,
            caption=qcard(
                f"✅ Auto-₹{amt} QR Code Saved!",
                f"🏦 UPI ID : {html.escape(extracted_pa)}",
                f"💰 Pre-Filled Amount : ₹{amt}.00",
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_upi")]]
            ),
        )
        preview_qr.close()
        release_memory()
        return


@app.on_message(
    (filters.document | filters.video) & filters.user(Config.ADMINS) & filters.private
)
async def handle_auto_parsed_files(client: Client, message: Message):
    admin_id = message.from_user.id

    if admin_id in admin_interactive_state and message.forward_from_chat:
        await process_admin_interactive_input(client, message)
        return

    state = admin_upload_state.get(admin_id)
    if not state:
        await message.reply_text(
            qline("ℹ️ First send /upload and click your channel button!")
        )
        return

    media = message.video or message.document
    file_size = getattr(media, "file_size", 0) or 0
    if file_size > PREMIUM_MAX_BYTES:
        await message.reply_text(
            qline("❌ File exceeds Telegram Premium 4.00 GB limit.")
        )
        return

    raw_filename = (
        getattr(media, "file_name", None)
        or message.caption
        or f"{state['keyword']}.mkv"
    )
    size_mb = round(file_size / (1024 * 1024), 2)

    parsed = parse_anime_filename(raw_filename)

    async with state["lock"]:
        detected_q = parsed["quality"]
        if not detected_q or detected_q in state["files"] or detected_q in state["claimed_slots"]:
            for q_slot in ["480p", "720p", "1080p"]:
                if q_slot not in state["files"] and q_slot not in state["claimed_slots"]:
                    detected_q = q_slot
                    break
        if not detected_q:
            detected_q = "720p"

        if parsed["anime"] and parsed["anime"] != "Anime":
            state["anime"] = parsed["anime"]
        elif not state["anime"]:
            state["anime"] = state["keyword"].title()

        if parsed["season"] is not None:
            state["season"] = parsed["season"]
        if parsed["episode"]:
            state["episode"] = parsed["episode"]
        if parsed["audio"]:
            state["audio"] = parsed["audio"]

        db_ch = await safe_resolve_chat(client, client, Config.DB_CHANNEL_ID)
        stored_msg = await message.forward(db_ch)
        state["files"][detected_q] = {"msg_id": stored_msg.id, "size": size_mb}
        state["claimed_slots"].add(detected_q)
        state["status_lines"][detected_q] = f"✅ {detected_q.upper()} : Uploaded ({size_mb} MB)"
        await update_channel_live_progress(client, state, force=True)

        uploaded_count = len(state["files"])
        if uploaded_count < 3:
            remaining = [
                q.upper() for q in ["480p", "720p", "1080p"] if q not in state["files"]
            ]
            pub_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"🚀 Publish Now ({uploaded_count} File{'s' if uploaded_count > 1 else ''})",
                            callback_data="pubnow",
                        )
                    ]
                ]
            )
            has_s = bool(
                state.get("season")
                and str(state["season"]).strip()
                and str(state["season"]).lower() != "none"
            )
            await message.reply_text(
                qcard(
                    f"✅ {detected_q.upper()} Uploaded ({size_mb} MB) — ({uploaded_count}/3)",
                    f"🎬 Anime : {html.escape(state['anime'])}",
                    f"📅 Season : {html.escape(str(state['season']))}" if has_s else None,
                    f"🔢 Episode : {html.escape(str(state['episode']))}",
                    f"🔊 Audio : {html.escape(state['audio'])}",
                    f"📤 Remaining : {', '.join(remaining)}",
                ),
                reply_markup=pub_markup,
            )
            return

        status_msg = await message.reply_text(
            qline("🔗 Generating Shortener Links & Publishing...")
        )
        await finalize_and_publish_batch(client, admin_id, state, status_msg)


@app.on_message(
    filters.private
    & ~filters.command(["start", "settings", "upload", "done", "cancel"])
)
async def handle_private_messages(client: Client, message: Message):
    user_id = message.from_user.id

    if user_id in Config.ADMINS and user_id in admin_interactive_state:
        await process_admin_interactive_input(client, message)
        return

    if not message.text:
        return

    text = message.text.strip()

    if user_id in Config.ADMINS and user_id in admin_upload_state:
        urls = extract_clean_urls(text)
        if urls:
            ordered_slots = ["480p", "720p", "1080p"]
            state = admin_upload_state[user_id]
            for idx, link_url in enumerate(urls[:3]):
                # Check if user explicitly tagged the quality next to the link or in direct URL
                line_context = next(
                    (ln for ln in text.splitlines() if link_url in ln), link_url
                )
                q_match = re.search(r"\b(480p|720p|1080p)\b", line_context, re.IGNORECASE)
                slot_hint = q_match.group(1).lower() if q_match else None

                if (
                    not slot_hint
                    and len(urls) > 1
                    and idx < len(ordered_slots)
                    and ordered_slots[idx] not in state["claimed_slots"]
                ):
                    slot_hint = ordered_slots[idx]

                asyncio.create_task(
                    process_leech_url_item(
                        client,
                        user_client,
                        user_id,
                        message,
                        link_url,
                        forced_slot=slot_hint,
                    )
                )
            return

    if text.upper().startswith("VERIFY-"):
        missing_fsub_btns = await check_user_fsub(client, user_id)
        if missing_fsub_btns:
            await message.reply_text(
                render_env_template(Config.MSG_FSUB_RESTRICTED),
                reply_markup=InlineKeyboardMarkup(missing_fsub_btns),
            )
            return

        pay_doc = await payments_col.find_one({"verify_code": text.upper()})
        if not pay_doc:
            await message.reply_text(qline("❌ Invalid Verification Code!"))
            return
        if pay_doc.get("redeemed"):
            await message.reply_text(
                qline("⚠️ This Verification Code has already been used!")
            )
            return

        batch_id = pay_doc["batch_id"]
        batch = await batches_col.find_one({"_id": batch_id})
        if not batch:
            await message.reply_text(
                qline("❌ Associated episode files were not found.")
            )
            return

        await payments_col.update_one(
            {"_id": pay_doc["_id"]}, {"$set": {"redeemed": True}}
        )

        avail_qualities = [
            q for q in ["480p", "720p", "1080p"] if q in batch.get("files", {})
        ]
        await deliver_files_with_5min_timer(
            client=client,
            chat_id=message.chat.id,
            user_id=user_id,
            batch=batch,
            qualities=avail_qualities,
            retry_payload=f"all_{batch_id}",
        )
        return

    extracted_tx = extract_valid_tx_id_from_text(text)
    if extracted_tx or is_valid_upi_transaction_id(text):
        tx_to_verify = extracted_tx or re.sub(r"[\s\-]+", "", text).upper()
        missing_fsub_btns = await check_user_fsub(client, user_id)
        if missing_fsub_btns:
            await message.reply_text(
                render_env_template(Config.MSG_FSUB_RESTRICTED),
                reply_markup=InlineKeyboardMarkup(missing_fsub_btns),
            )
            return

        await verify_and_approve_utr(client, message, user_id, tx_to_verify)
        return

    pending_pay = await payments_col.find_one(
        {"user_id": user_id, "status": "waiting_utr"}
    )
    if pending_pay:
        await message.reply_text(
            qcard(
                "⚠️ Invalid Transaction ID / Proof!",
                "📲 Send your Payment Screenshot OR paste your 12-digit UTR / PhonePe Transaction ID after paying ₹1.",
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(Config.BTN_CANCEL_PAY, callback_data="pay_cancel")]]
            ),
        )
