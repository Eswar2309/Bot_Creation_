import html
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from config import Config
from database import (
    app,
    channels_col,
    settings_col,
    admin_interactive_state,
    get_settings
)
from modules.formatting import qline, qcard, render_env_template


def get_settings_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Manage Channels", callback_data="stg_channels"),
            InlineKeyboardButton("🖼️ Manage Posters", callback_data="stg_posters")
        ],
        [
            InlineKeyboardButton("📄 File / Video Mode", callback_data="stg_upmode"),
            InlineKeyboardButton("🔊 Target Audio (Tamil/Multi)", callback_data="stg_audio")
        ],
        [
            InlineKeyboardButton("🖼️ File Thumbnail", callback_data="stg_thumb"),
            InlineKeyboardButton("✏️ Rename Format", callback_data="stg_rename")
        ],
        [
            InlineKeyboardButton("📝 Caption Format", callback_data="stg_caption"),
            InlineKeyboardButton("💧 Video & Sub Watermark", callback_data="stg_watermark")
        ],
        [
            InlineKeyboardButton("🛡️ Force-Sub (Base + Step-Up)", callback_data="stg_fsub"),
            InlineKeyboardButton("🔗 Shortener & How-To", callback_data="stg_shortener")
        ],
        [
            InlineKeyboardButton("💳 UPI & Auto-₹1 QR", callback_data="stg_upi"),
            InlineKeyboardButton("📢 Issue & Share Links", callback_data="stg_mainlinks")
        ],
        [
            InlineKeyboardButton("🔄 Sync / Reset to .ENV Defaults", callback_data="stg_sync_env")
        ],
        [InlineKeyboardButton("❌ Close Settings", callback_data="stg_close")]
    ])


async def build_settings_dashboard_text() -> str:
    cfg = await get_settings()
    ch_count = await channels_col.count_documents({})
    fsub_count = len(cfg.get("fsub_list", []))
    up_mode_val = cfg.get("upload_mode") or Config.DEFAULT_UPLOAD_MODE
    up_mode = "📄 Document (File)" if up_mode_val == "document" else "🎬 Video (Stream)"
    pref_audio = html.escape(cfg.get("preferred_audio") or Config.DEFAULT_PREFERRED_AUDIO)
    thumb_status = "✅ Saved" if cfg.get("thumb_file_id") else "❌ Not Set"

    active_rename = cfg.get("rename_format") or Config.DEFAULT_RENAME_FORMAT or "Default Auto Format"
    active_cap = cfg.get("caption_format") or Config.DEFAULT_CAPTION_FORMAT or "Default Step-Down Blockquote"
    active_main_wm = cfg.get("main_watermark") or Config.DEFAULT_MAIN_WATERMARK or "Off"
    active_sub_wm = cfg.get("sub_watermark") or Config.DEFAULT_SUB_WATERMARK or "Off"

    issue_set = "✅ Set" if (cfg.get("issue_link") or Config.ISSUE_LINK) else "❌ Not Set"
    share_set = "✅ Set" if (cfg.get("share_link") or Config.SHARE_LINK) else "❌ Not Set"

    dashboard_tpl = getattr(
        Config,
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
        "⬇️ Select Any Setting Below To Configure :"
    )

    return render_env_template(
        dashboard_tpl,
        ch_count=ch_count,
        fsub_count=fsub_count,
        base_fsub=Config.BASE_FSUB_COUNT,
        up_mode=up_mode,
        pref_audio=pref_audio,
        thumb_status=thumb_status,
        rename_status=html.escape(active_rename),
        cap_status=html.escape(active_cap.replace("\n", " | ")),
        main_wm=html.escape(active_main_wm),
        sub_wm=html.escape(active_sub_wm),
        shortener_url=html.escape(cfg.get("shortener_url") or Config.SHORTENER_URL),
        upi_id=html.escape(cfg.get("upi_id") or Config.UPI_ID),
        pay_amount=cfg.get("pay_amount") or Config.PAYMENT_AMOUNT_INR,
        issue_set=issue_set,
        share_set=share_set
    )


@app.on_message(filters.command("settings") & filters.private)
async def settings_command(client: Client, message: Message):
    if message.from_user.id not in Config.ADMINS:
        await message.reply_text(qline("⛔ Only Bot Admins can access Settings!"))
        return

    admin_interactive_state.pop(message.from_user.id, None)
    text = await build_settings_dashboard_text()
    await message.reply_text(text, reply_markup=get_settings_main_keyboard())


async def edit_or_reply_settings(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        if query.message.photo:
            await query.message.edit_caption(caption=text, reply_markup=markup)
        else:
            await query.message.edit_text(text=text, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception as e:
        print(f"Settings edit notice: {e}")


@app.on_callback_query(filters.regex("^stg_"))
async def handle_settings_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id not in Config.ADMINS:
        await query.answer("⛔ Admin Only!", show_alert=True)
        return

    data = query.data

    if data in ["stg_main", "stg_discard"]:
        admin_interactive_state.pop(user_id, None)
        text = await build_settings_dashboard_text()
        await edit_or_reply_settings(query, text, get_settings_main_keyboard())
        await query.answer()
        return

    if data == "stg_close":
        admin_interactive_state.pop(user_id, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data == "stg_sync_env":
        default_fsubs = [
            {"chat_id": cid, "title": f"Channel {i+1}", "mode": Config.DEFAULT_FSUB_MODE}
            for i, cid in enumerate(Config.DEFAULT_FSUB_CHANNELS)
        ]
        env_synced = {
            "shortener_url": Config.SHORTENER_URL,
            "shortener_api": Config.SHORTENER_API,
            "how_to_link": Config.HOW_TO_DOWNLOAD_LINK,
            "issue_link": Config.ISSUE_LINK,
            "share_link": Config.SHARE_LINK,
            "upi_id": Config.UPI_ID,
            "pay_amount": Config.PAYMENT_AMOUNT_INR,
            "upload_mode": Config.DEFAULT_UPLOAD_MODE,
            "preferred_audio": Config.DEFAULT_PREFERRED_AUDIO,
            "hardburn_wm": Config.DEFAULT_HARDBURN_WM,
            "rename_format": Config.DEFAULT_RENAME_FORMAT,
            "caption_format": Config.DEFAULT_CAPTION_FORMAT,
            "main_watermark": Config.DEFAULT_MAIN_WATERMARK,
            "sub_watermark": Config.DEFAULT_SUB_WATERMARK
        }
        if default_fsubs:
            env_synced["fsub_list"] = default_fsubs

        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": env_synced},
            upsert=True
        )
        await query.answer("✅ Synced all settings from .ENV!", show_alert=True)
        text = await build_settings_dashboard_text()
        await edit_or_reply_settings(query, text, get_settings_main_keyboard())
        return

    if data == "stg_upmode":
        cfg = await get_settings()
        curr = cfg.get("upload_mode") or Config.DEFAULT_UPLOAD_MODE
        curr_label = "📄 Document (File)" if curr == "document" else "🎬 Video (Stream)"
        doc_btn_txt = "✅ 📄 Document (File)" if curr == "document" else "📄 Document (File)"
        vid_btn_txt = "✅ 🎬 Video (Stream)" if curr == "video" else "🎬 Video (Stream)"
        await edit_or_reply_settings(
            query,
            qcard(
                "📄 Select Upload File Type",
                f"📦 Current Mode : {curr_label}",
                "ℹ️ Choose whether fetched files are stored and sent as a Document (File) or Video (Streamable)."
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(doc_btn_txt, callback_data="stg_setmode_document"),
                    InlineKeyboardButton(vid_btn_txt, callback_data="stg_setmode_video")
                ],
                [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
            ])
        )
        return

    if data.startswith("stg_setmode_"):
        new_mode = data.split("stg_setmode_", 1)[1]
        cfg = await get_settings()
        if (cfg.get("upload_mode") or Config.DEFAULT_UPLOAD_MODE) == new_mode:
            await query.answer(f"Already set to {new_mode.upper()}!", show_alert=False)
            return
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"upload_mode": new_mode}}, upsert=True)
        await query.answer(f"Upload mode set to {new_mode.upper()}!", show_alert=True)
        query.data = "stg_upmode"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_audio":
        cfg = await get_settings()
        curr_aud = cfg.get("preferred_audio") or Config.DEFAULT_PREFERRED_AUDIO

        def aud_label(lang_name: str, flag: str, display: str) -> str:
            prefix = "✅ " if curr_aud.lower() == lang_name.lower() else ""
            return f"{prefix}{flag} {display}"

        await edit_or_reply_settings(
            query,
            qcard(
                "🔊 Smart Multi-Audio Language Extractor",
                f"🔊 Current Selected Audio : {html.escape(curr_aud)}",
                "🎯 If a Google Drive video has multiple audio tracks, the bot extracts ONLY your selected language.",
                "⚡ If the video already has only 1 audio track (e.g. only Tamil), the bot uploads it directly."
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(aud_label("Tamil", "🇮🇳", "Tamil Only"), callback_data="stg_setaud_Tamil"),
                    InlineKeyboardButton(aud_label("English", "🇬🇧", "English Only"), callback_data="stg_setaud_English")
                ],
                [
                    InlineKeyboardButton(aud_label("Malayalam", "🇮🇳", "Malayalam Only"), callback_data="stg_setaud_Malayalam"),
                    InlineKeyboardButton(aud_label("Telugu", "🇮🇳", "Telugu Only"), callback_data="stg_setaud_Telugu")
                ],
                [
                    InlineKeyboardButton(aud_label("Hindi", "🇮🇳", "Hindi Only"), callback_data="stg_setaud_Hindi"),
                    InlineKeyboardButton(aud_label("Japanese", "🇯🇵", "Japanese Only"), callback_data="stg_setaud_Japanese")
                ],
                [
                    InlineKeyboardButton(aud_label("All", "🌐", "Keep All Audio Tracks"), callback_data="stg_setaud_All")
                ],
                [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
            ])
        )
        return

    if data.startswith("stg_setaud_"):
        chosen_aud = data.split("stg_setaud_", 1)[1]
        cfg = await get_settings()
        if (cfg.get("preferred_audio") or Config.DEFAULT_PREFERRED_AUDIO).lower() == chosen_aud.lower():
            await query.answer(f"Already set to {chosen_aud}!", show_alert=False)
            return
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"preferred_audio": chosen_aud}}, upsert=True)
        await query.answer(f"Target Audio set to {chosen_aud}!", show_alert=True)
        query.data = "stg_audio"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_thumb":
        cfg = await get_settings()
        has_thumb = bool(cfg.get("thumb_file_id"))
        status_txt = "✅ Custom Thumbnail Active" if has_thumb else "❌ No Thumbnail Set"
        btn_rows = [
            [InlineKeyboardButton("➕ Add / Change Thumbnail", callback_data="stg_thumb_add")]
        ]
        if has_thumb:
            btn_rows.append([InlineKeyboardButton("🗑️ Remove Thumbnail", callback_data="stg_thumb_del")])
        btn_rows.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")])

        await edit_or_reply_settings(
            query,
            qcard(
                "🖼️ Custom MKV File Thumbnail",
                f"🖼️ Status : {status_txt}",
                "📌 This thumbnail is attached to every renamed .mkv file uploaded to the DB Channel."
            ),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data == "stg_thumb_add":
        admin_interactive_state[user_id] = {"action": "wait_file_thumb"}
        await edit_or_reply_settings(
            query,
            qcard(
                "➕ Send File Thumbnail Photo",
                "📷 Send the image/photo you want to use as the thumbnail for all uploaded .mkv files."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_thumb_del":
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"thumb_file_id": ""}}, upsert=True)
        await query.answer("🗑️ Thumbnail Removed!", show_alert=True)
        query.data = "stg_thumb"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_rename":
        cfg = await get_settings()
        curr_fmt = html.escape(
            cfg.get("rename_format")
            or Config.DEFAULT_RENAME_FORMAT
            or "Default ({anime} - S{season}E{episode} [{audio}] [{quality}].mkv)"
        )
        btn_rows = [
            [InlineKeyboardButton("✏️ Set Rename Format", callback_data="stg_rename_set")],
            [InlineKeyboardButton("🔄 Reset to .ENV Default", callback_data="stg_rename_clr")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(
            query,
            qcard(
                "✏️ Custom MKV Rename Format",
                f"✏️ Current Format : {curr_fmt}",
                "🏷️ Supports custom tags: ({anime} - S{season}E{episode} [{audio}] [{quality}][{@aniflxTamil}].mkv)",
                "ℹ️ If a link has no Season, Season is automatically omitted from the file name!"
            ),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data == "stg_rename_set":
        admin_interactive_state[user_id] = {"action": "wait_rename_fmt"}
        await edit_or_reply_settings(
            query,
            qcard(
                "✏️ Send Your Rename Format",
                "📝 Send your desired filename template (automatically saved as .mkv).",
                "📌 Example : ({anime} - S{season}E{episode} [{audio}] [{quality}][{@aniflxTamil}].mkv)"
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_rename_clr":
        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"rename_format": Config.DEFAULT_RENAME_FORMAT}},
            upsert=True
        )
        await query.answer("Reset Rename Format to .ENV default!", show_alert=True)
        query.data = "stg_rename"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_caption":
        cfg = await get_settings()
        curr_cap = html.escape(
            (cfg.get("caption_format") or Config.DEFAULT_CAPTION_FORMAT or "Default Step-Down Blockquote Format")
            .replace("\n", " | ")
        )
        btn_rows = [
            [InlineKeyboardButton("📝 Set Custom Caption", callback_data="stg_caption_set")],
            [InlineKeyboardButton("🔄 Reset to .ENV Default", callback_data="stg_caption_clr")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(
            query,
            qcard(
                "📝 Custom File Caption Format",
                f"📝 Current Caption : {curr_cap}",
                "🏷️ Supports ({anime} - S{season}E{episode} [{audio}] [{quality}][{@aniflxTamil}].mkv) or multi-line captions!"
            ),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data == "stg_caption_set":
        admin_interactive_state[user_id] = {"action": "wait_caption_fmt"}
        await edit_or_reply_settings(
            query,
            qcard(
                "📝 Send Your Custom Caption Format",
                "📌 Every line will automatically be wrapped in its own step-down blockquote box.",
                "🏷️ Example : ({anime} - S{season}E{episode} [{audio}] [{quality}][{@aniflxTamil}].mkv)"
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_caption_clr":
        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"caption_format": Config.DEFAULT_CAPTION_FORMAT}},
            upsert=True
        )
        await query.answer("Reset Caption Format to .ENV default!", show_alert=True)
        query.data = "stg_caption"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_watermark":
        cfg = await get_settings()
        main_wm = html.escape(cfg.get("main_watermark") or Config.DEFAULT_MAIN_WATERMARK or "Not Set")
        sub_wm = html.escape(cfg.get("sub_watermark") or Config.DEFAULT_SUB_WATERMARK or "Not Set")
        hb_mode = (
            "🔥 Visible Video Burn + SRT (Active)"
            if cfg.get("hardburn_wm", Config.DEFAULT_HARDBURN_WM)
            else "⚡ SRT Sub-Track Only (Stream Copy)"
        )

        btn_rows = [
            [
                InlineKeyboardButton("💧 Set Top-Right Video WM", callback_data="stg_wm_main_set"),
                InlineKeyboardButton("🗑️ Clear Video WM", callback_data="stg_wm_main_clr")
            ],
            [
                InlineKeyboardButton("💬 Set First/Last 30s Sub WM", callback_data="stg_wm_sub_set"),
                InlineKeyboardButton("🗑️ Clear Sub WM", callback_data="stg_wm_sub_clr")
            ],
            [
                InlineKeyboardButton(f"⚙️ Mode: {hb_mode}", callback_data="stg_wm_toggle_hb")
            ],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(
            query,
            qcard(
                "💧 Permanent Top-Right & First/Last 30s Sub Watermark",
                f"💧 Permanent Top-Right Watermark : {main_wm}",
                f"💬 First & Last 30s Sub Watermark : {sub_wm}",
                f"⚡ Watermark Engine : {hb_mode}",
                "🎬 Burns both watermarks directly onto the video while keeping the exact Google Drive file size & quality!"
            ),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data == "stg_wm_toggle_hb":
        cfg = await get_settings()
        new_hb = not bool(cfg.get("hardburn_wm", Config.DEFAULT_HARDBURN_WM))
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"hardburn_wm": new_hb}}, upsert=True)
        await query.answer(
            "Switched to Visible Video Burn + SRT!" if new_hb else "Switched to SRT Sub-Track Only!",
            show_alert=True
        )
        query.data = "stg_watermark"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_wm_main_set":
        admin_interactive_state[user_id] = {"action": "wait_main_wm"}
        await edit_or_reply_settings(
            query,
            qcard(
                "💧 Set Permanent Top-Right Watermark Text",
                "📝 Send the watermark text to display at the top-right corner of the video from start to end (e.g. @aniflxTamil)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_wm_main_clr":
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"main_watermark": ""}}, upsert=True)
        await query.answer("Cleared Top-Right Video Watermark!", show_alert=True)
        query.data = "stg_watermark"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_wm_sub_set":
        admin_interactive_state[user_id] = {"action": "wait_sub_wm"}
        await edit_or_reply_settings(
            query,
            qcard(
                "💬 Set First & Last 30 Seconds Sub Watermark Text",
                "📝 Send the subtitle watermark text (e.g. Encoded By @aniflxTamil) to display at the bottom of the anime during the first 30s and last 30s."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_wm_sub_clr":
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"sub_watermark": ""}}, upsert=True)
        await query.answer("Cleared Sub Watermark!", show_alert=True)
        query.data = "stg_watermark"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_channels":
        channels = await channels_col.find().to_list(100)
        ch_items = []
        btn_rows = [[InlineKeyboardButton("➕ Add New Channel", callback_data="stg_ch_add")]]

        if channels:
            row = []
            for ch in channels:
                kw = ch["keyword"]
                ch_items.append(f"🎬 {html.escape(kw.title())} — {ch['channel_id']}")
                row.append(InlineKeyboardButton(f"🗑️ {kw.title()}", callback_data=f"stg_ch_del_{kw}"))
                if len(row) == 2:
                    btn_rows.append(row)
                    row = []
            if row:
                btn_rows.append(row)
        else:
            ch_items.append("ℹ️ No anime channels added yet.")

        btn_rows.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")])
        await edit_or_reply_settings(
            query,
            qcard("🎬 Manage Anime Channels", *ch_items),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data == "stg_ch_add":
        admin_interactive_state[user_id] = {"action": "ch_wait_keyword"}
        await edit_or_reply_settings(
            query,
            qcard(
                "➕ Add Anime Channel — Step 1/2",
                "🏷️ Send the Keyword Name for the channel button (e.g. naruto or bluelock)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data.startswith("stg_ch_del_"):
        kw = data.split("stg_ch_del_", 1)[1]
        await channels_col.delete_one({"keyword": kw})
        await query.answer(f"Deleted {kw.title()}!", show_alert=True)
        query.data = "stg_channels"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_posters":
        channels = await channels_col.find().to_list(100)
        if not channels:
            await query.answer("First add an Anime Channel in Manage Channels!", show_alert=True)
            return

        pos_items = []
        btn_rows = []
        for ch in channels:
            kw = ch["keyword"]
            status_icon = "✅ Saved" if ch.get("poster_file_id") else "❌ Not Set"
            pos_items.append(f"🖼️ {html.escape(kw.title())} : {status_icon}")
            row = [InlineKeyboardButton(f"🖼️ Set {kw.title()} Poster", callback_data=f"stg_pos_set_{kw}")]
            if ch.get("poster_file_id"):
                row.append(InlineKeyboardButton("🗑️ Clear", callback_data=f"stg_pos_clr_{kw}"))
            btn_rows.append(row)

        btn_rows.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")])
        await edit_or_reply_settings(
            query,
            qcard(
                "🖼️ Manage Permanent Channel Posters",
                *pos_items,
                "📐 Season and Episode numbers are stamped at the Top-Right corner automatically on every upload."
            ),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data.startswith("stg_pos_set_"):
        kw = data.split("stg_pos_set_", 1)[1]
        admin_interactive_state[user_id] = {"action": "pos_wait_photo", "keyword": kw}
        await edit_or_reply_settings(
            query,
            qcard(
                f"🖼️ Send Poster Image — {html.escape(kw.title())}",
                "📷 Send the photo now.",
                "📐 The bot will save it permanently and stamp the Season and Episode numbers at the Top-Right corner automatically."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data.startswith("stg_pos_clr_"):
        kw = data.split("stg_pos_clr_", 1)[1]
        await channels_col.update_one({"keyword": kw}, {"$unset": {"poster_file_id": ""}})
        await query.answer(f"Cleared poster for {kw.title()}!", show_alert=True)
        query.data = "stg_posters"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_fsub":
        cfg = await get_settings()
        fsub_list = cfg.get("fsub_list", [])
        fsub_items = []
        if fsub_list:
            for i, item in enumerate(fsub_list, start=1):
                role = "Base" if i <= Config.BASE_FSUB_COUNT else f"Step #{i}"
                fsub_items.append(
                    f"🛡️ {i}. {html.escape(item.get('title', str(item['chat_id'])))} [{item.get('mode', 'join').upper()}] ({role})"
                )
        else:
            fsub_items.append("ℹ️ No Force-Sub channels added yet.")

        btn_rows = [
            [InlineKeyboardButton("➕ Add Force-Sub Channel", callback_data="stg_fsub_add")],
            [
                InlineKeyboardButton("🗑️ Remove Last", callback_data="stg_fsub_pop"),
                InlineKeyboardButton("🧹 Clear All", callback_data="stg_fsub_clear")
            ],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(
            query,
            qcard(
                "🛡️ Force-Subscribe Manager",
                *fsub_items,
                f"📌 Channels 1–{Config.BASE_FSUB_COUNT} : Permanent Base Channels | Channels {Config.BASE_FSUB_COUNT + 1}+ : Progressive Step-Up"
            ),
            InlineKeyboardMarkup(btn_rows)
        )
        return

    if data == "stg_fsub_add":
        admin_interactive_state[user_id] = {"action": "fsub_wait_channel"}
        await edit_or_reply_settings(
            query,
            qcard(
                "➕ Add Force-Sub Channel",
                "📢 Forward any message from your Force-Sub channel here, or send its Channel ID (e.g. -100123456789)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_fsub_pop":
        cfg = await get_settings()
        fsub_list = cfg.get("fsub_list", [])
        if fsub_list:
            fsub_list.pop()
            await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"fsub_list": fsub_list}})
            await query.answer("Removed last F-Sub channel!", show_alert=True)
        else:
            await query.answer("F-Sub list is already empty!", show_alert=True)
        query.data = "stg_fsub"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_fsub_clear":
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"fsub_list": []}})
        await query.answer("Cleared all F-Sub channels!", show_alert=True)
        query.data = "stg_fsub"
        await handle_settings_callbacks(client, query)
        return

    if data.startswith("stg_fsub_mode_"):
        mode = data.split("stg_fsub_mode_", 1)[1]
        st = admin_interactive_state.pop(user_id, None)
        if not st or "chat_id" not in st:
            await query.answer("Session expired!", show_alert=True)
            return

        cfg = await get_settings()
        fsub_list = cfg.get("fsub_list", [])
        updated = False
        for item in fsub_list:
            if item["chat_id"] == st["chat_id"]:
                item["mode"] = mode
                item["title"] = st["title"]
                updated = True
                break
        if not updated:
            fsub_list.append({"chat_id": st["chat_id"], "title": st["title"], "mode": mode})

        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"fsub_list": fsub_list}})
        await query.answer(f"Saved in {mode.upper()} mode!", show_alert=True)
        query.data = "stg_fsub"
        await handle_settings_callbacks(client, query)
        return

    if data == "stg_shortener":
        cfg = await get_settings()
        text = qcard(
            "🔗 Shortener & Tutorial Settings",
            f"🌐 Shortener Domain : {html.escape(cfg.get('shortener_url') or Config.SHORTENER_URL)}",
            f"🔑 API Key : {html.escape((cfg.get('shortener_api') or Config.SHORTENER_API)[:12])}...",
            f"❓ How-To Link : {html.escape(cfg.get('how_to_link') or Config.HOW_TO_DOWNLOAD_LINK)}"
        )
        btn_rows = [
            [InlineKeyboardButton("✏️ Set Shortener URL & API", callback_data="stg_sh_set")],
            [InlineKeyboardButton("❓ Set 'How to Download' Link", callback_data="stg_howto_set")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(query, text, InlineKeyboardMarkup(btn_rows))
        return

    if data == "stg_sh_set":
        admin_interactive_state[user_id] = {"action": "wait_shortener"}
        await edit_or_reply_settings(
            query,
            qcard(
                "✏️ Update Shortener API",
                "🌐 Send your Shortener API URL and Key separated by a space.",
                "📌 Example : https://api.gplinks.com/api YOUR_API_KEY"
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_howto_set":
        admin_interactive_state[user_id] = {"action": "wait_howto"}
        await edit_or_reply_settings(
            query,
            qcard(
                "❓ Set 'How to Download' Tutorial Link",
                "🔗 Send the tutorial URL (e.g. https://t.me/your_channel/10)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_upi":
        cfg = await get_settings()
        text = qcard(
            "💳 UPI & Auto-₹1 QR Settings",
            f"🏦 UPI ID : {html.escape(cfg.get('upi_id') or Config.UPI_ID)}",
            f"💰 Fixed Amount : ₹{cfg.get('pay_amount') or Config.PAYMENT_AMOUNT_INR}.00"
        )
        btn_rows = [
            [InlineKeyboardButton("📷 Upload Normal QR -> Auto-₹1 QR", callback_data="stg_upi_qr")],
            [InlineKeyboardButton("🏦 Set UPI ID & Amount", callback_data="stg_upi_id")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(query, text, InlineKeyboardMarkup(btn_rows))
        return

    if data == "stg_upi_qr":
        admin_interactive_state[user_id] = {"action": "wait_upi_qr"}
        await edit_or_reply_settings(
            query,
            qcard(
                "📷 Upload Your Normal GPay / PhonePe QR Image",
                "📲 Send your QR code photo now.",
                "⚡ The bot will decode it and inject ₹1.00 automatically."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_upi_id":
        admin_interactive_state[user_id] = {"action": "wait_upi_text"}
        await edit_or_reply_settings(
            query,
            qcard(
                "🏦 Set UPI ID & Fixed Amount",
                "💳 Send your UPI ID and Amount (1 to 10) separated by a space.",
                "📌 Example : yourupi@okaxis 1"
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_mainlinks":
        cfg = await get_settings()
        text = qcard(
            "📢 Main Channel Button Links",
            f"⚠️ Any Issue Link : {html.escape(cfg.get('issue_link') or Config.ISSUE_LINK or 'Not Set')}",
            f"📤 Share Channel Link : {html.escape(cfg.get('share_link') or Config.SHARE_LINK or 'Not Set')}"
        )
        btn_rows = [
            [InlineKeyboardButton("⚠️ Set 'Any Issue' Link", callback_data="stg_set_issue")],
            [InlineKeyboardButton("📤 Set 'Share Channel' Link", callback_data="stg_set_share")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]
        ]
        await edit_or_reply_settings(query, text, InlineKeyboardMarkup(btn_rows))
        return

    if data == "stg_set_issue":
        admin_interactive_state[user_id] = {"action": "wait_issue_link"}
        await edit_or_reply_settings(
            query,
            qcard(
                "⚠️ Set 'Any Issue' Button Link",
                "🔗 Send the Support Group/Channel URL (or forward a message from that channel)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if data == "stg_set_share":
        admin_interactive_state[user_id] = {"action": "wait_share_link"}
        await edit_or_reply_settings(
            query,
            qcard(
                "📤 Set 'Share Channel' Button Link",
                "🔗 Send the Main Channel URL you want users to share (or forward a message from that channel)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return


async def process_admin_interactive_input(client: Client, message: Message):
    user_id = message.from_user.id
    st = admin_interactive_state.get(user_id)
    if not st:
        return

    action = st.get("action")
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Settings", callback_data="stg_main")]])

    if action == "wait_rename_fmt" and message.text:
        fmt = message.text.strip()
        admin_interactive_state.pop(user_id, None)
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"rename_format": fmt}}, upsert=True)
        await message.reply_text(
            qcard("✅ Rename Format Saved!", f"✏️ Template : {html.escape(fmt)}"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Rename", callback_data="stg_rename")]])
        )
        return

    if action == "wait_caption_fmt" and message.text:
        cap_fmt = message.text.strip()
        admin_interactive_state.pop(user_id, None)
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"caption_format": cap_fmt}}, upsert=True)
        await message.reply_text(
            qcard("✅ Custom Caption Format Saved!", f"📝 Template : {html.escape(cap_fmt)}"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Caption", callback_data="stg_caption")]])
        )
        return

    if action == "wait_sub_wm" and message.text:
        sub_txt = message.text.strip()
        admin_interactive_state.pop(user_id, None)
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {"sub_watermark": sub_txt}}, upsert=True)
        await message.reply_text(
            qcard("✅ First & Last 30s Sub Watermark Saved!", f"💬 Text : {html.escape(sub_txt)}"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Watermark", callback_data="stg_watermark")]])
        )
        return

    if action == "ch_wait_keyword":
        if not message.text:
            await message.reply_text(qline("⚠️ Please send a text keyword (e.g. naruto)."))
            return
        kw = message.text.strip().lower()
        admin_interactive_state[user_id] = {"action": "ch_wait_target", "keyword": kw}
        await message.reply_text(
            qcard(
                f"➕ Add Channel For '{html.escape(kw.title())}' — Step 2/2",
                "📢 Forward any message from that channel here, or send its Channel ID (e.g. -100123456789)."
            ),
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Discard / Cancel", callback_data="stg_discard")]])
        )
        return

    if action == "ch_wait_target":
        kw = st["keyword"]
        ch_id = None
        ch_title = kw.title()

        if message.forward_from_chat:
            ch_id = int(message.forward_from_chat.id)
            ch_title = message.forward_from_chat.title or kw.title()
        elif message.text and message.text.strip().lstrip("-").isdigit():
            ch_id = int(message.text.strip())
            try:
                chat = await client.get_chat(ch_id)
                ch_title = chat.title
            except Exception:
                pass

        if not ch_id:
            await message.reply_text(qline("⚠️ Invalid input! Forward a message from the channel or send a valid Channel ID."))
            return

        admin_interactive_state.pop(user_id, None)
        await channels_col.update_one(
            {"keyword": kw},
            {"$set": {"channel_id": ch_id, "title": ch_title}},
            upsert=True
        )
        await message.reply_text(
            qcard(
                "✅ Channel Saved Successfully!",
                f"🔘 Keyword : {html.escape(kw.title())}",
                f"📢 Channel : {html.escape(ch_title)} ({ch_id})"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🖼️ Set Poster for {kw.title()}", callback_data=f"stg_pos_set_{kw}")],
                [InlineKeyboardButton("⬅️ Back to Channels", callback_data="stg_channels")]
            ])
        )
        return

    if action == "fsub_wait_channel":
        ch_id = None
        ch_title = None
        if message.forward_from_chat:
            ch_id = int(message.forward_from_chat.id)
            ch_title = message.forward_from_chat.title
        elif message.text and message.text.strip().lstrip("-").isdigit():
            ch_id = int(message.text.strip())
            try:
                chat = await client.get_chat(ch_id)
                ch_title = chat.title
            except Exception:
                ch_title = f"Channel {ch_id}"

        if not ch_id:
            await message.reply_text(qline("⚠️ Please forward a message from the channel or send its Channel ID!"))
            return

        admin_interactive_state[user_id] = {"action": "fsub_select_mode", "chat_id": ch_id, "title": ch_title}
        await message.reply_text(
            qcard(
                "⚙️ Select Force-Sub Mode",
                f"📢 Channel : {html.escape(ch_title)} ({ch_id})",
                "🛡️ Choose whether users must Join directly or send a Join Request."
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📢 Join Mode", callback_data="stg_fsub_mode_join"),
                    InlineKeyboardButton("🔒 Request Mode", callback_data="stg_fsub_mode_request")
                ],
                [InlineKeyboardButton("🚫 Discard", callback_data="stg_discard")]
            ])
        )
        return

    if action == "wait_shortener" and message.text:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await message.reply_text(qline("⚠️ Send both URL and API Key separated by a space!"))
            return
        admin_interactive_state.pop(user_id, None)
        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"shortener_url": parts[0], "shortener_api": parts[1]}},
            upsert=True
        )
        await message.reply_text(qline("✅ Shortener URL & API Key Updated!"), reply_markup=back_btn)
        return

    if action == "wait_howto" and message.text:
        admin_interactive_state.pop(user_id, None)
        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"how_to_link": message.text.strip()}},
            upsert=True
        )
        await message.reply_text(qline("✅ 'How to Download' Link Updated!"), reply_markup=back_btn)
        return

    if action == "wait_upi_text" and message.text:
        parts = message.text.strip().split()
        new_upi = parts[0]
        amt = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        amt = max(1, min(10, amt))
        raw_uri = f"upi://pay?pa={new_upi}&cu=INR"
        admin_interactive_state.pop(user_id, None)

        await settings_col.update_one(
            {"_id": "bot_settings"},
            {"$set": {"upi_id": new_upi, "raw_upi_uri": raw_uri, "pay_amount": amt}},
            upsert=True
        )
        await message.reply_text(
            qcard("✅ UPI Settings Updated!", f"🏦 UPI ID : {html.escape(new_upi)}", f"💰 Amount : ₹{amt}.00"),
            reply_markup=back_btn
        )
        return

    if action in ["wait_issue_link", "wait_share_link"]:
        link = None
        if message.forward_from_chat:
            chat = message.forward_from_chat
            if chat.username:
                link = f"https://t.me/{chat.username}"
            else:
                try:
                    c_info = await client.get_chat(chat.id)
                    link = c_info.invite_link or (await client.export_chat_invite_link(chat.id))
                except Exception:
                    pass
        elif message.text:
            link = message.text.strip()
            if link.startswith("@"):
                link = f"https://t.me/{link[1:]}"

        if not link:
            await message.reply_text(qline("⚠️ Could not extract link. Send a valid https://t.me/... URL!"))
            return

        admin_interactive_state.pop(user_id, None)
        field = "issue_link" if action == "wait_issue_link" else "share_link"
        label = "Any Issue" if action == "wait_issue_link" else "Share Channel"
        await settings_col.update_one({"_id": "bot_settings"}, {"$set": {field: link}}, upsert=True)
        await message.reply_text(
            qcard(f"✅ {label} Button Link Saved!", f"🔗 Link : {html.escape(link)}"),
            reply_markup=back_btn
        )
        return
