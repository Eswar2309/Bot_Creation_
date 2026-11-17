import asyncio
from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant
from config import Config
from database import fsub_reqs_col, users_col, AUTO_DELETE_SECONDS, get_settings
from modules.formatting import render_env_template, build_bold_file_caption
from modules.uploader import safe_resolve_chat


async def get_user_required_fsub_count(user_id: int, total_fsub_channels: int) -> int:
    user_doc = await users_col.find_one({"_id": user_id})
    downloads_done = user_doc.get("downloads_count", 0) if user_doc else 0
    required = Config.BASE_FSUB_COUNT + downloads_done
    return min(required, total_fsub_channels)


async def increment_user_download_count(user_id: int):
    await users_col.update_one(
        {"_id": user_id},
        {"$inc": {"downloads_count": 1}},
        upsert=True
    )


async def check_user_fsub(client: Client, user_id: int):
    if user_id in Config.ADMINS:
        return []

    cfg = await get_settings()
    fsub_list = cfg.get("fsub_list", [])
    if not fsub_list:
        return []

    req_count = await get_user_required_fsub_count(user_id, len(fsub_list))
    active_fsubs = fsub_list[:req_count]

    missing_buttons = []
    for idx, item in enumerate(active_fsubs, start=1):
        ch_id = int(item["chat_id"])
        mode = item.get("mode", "join")
        is_verified = False

        try:
            await safe_resolve_chat(client, client, ch_id)
            member = await client.get_chat_member(ch_id, user_id)
            if member.status.value in ["member", "administrator", "owner"]:
                is_verified = True
        except UserNotParticipant:
            if mode == "request":
                req = await fsub_reqs_col.find_one({"user_id": user_id, "chat_id": ch_id})
                if req:
                    is_verified = True
        except Exception:
            is_verified = True

        if not is_verified:
            try:
                if mode == "request":
                    inv = await client.create_chat_invite_link(ch_id, creates_join_request=True)
                    invite_url = inv.invite_link
                    btn_label = f"🔒 Request to Join Channel {idx}"
                else:
                    chat = await client.get_chat(ch_id)
                    invite_url = chat.invite_link or (await client.export_chat_invite_link(ch_id))
                    btn_label = f"📢 Join Channel {idx}"
                missing_buttons.append([InlineKeyboardButton(btn_label, url=invite_url)])
            except Exception as e:
                print(f"FSub Invite Error for {ch_id}: {e}")

    return missing_buttons


async def schedule_auto_delete_and_get_file(
    client: Client,
    chat_id: int,
    msg_ids_to_delete: list,
    retry_payload: str
):
    await asyncio.sleep(AUTO_DELETE_SECONDS)

    for mid in msg_ids_to_delete:
        try:
            await client.delete_messages(chat_id, mid)
        except Exception:
            pass

    bot_uname = (await client.get_me()).username
    get_file_url = f"https://t.me/{bot_uname}?start={retry_payload}"
    mins = max(1, AUTO_DELETE_SECONDS // 60)

    try:
        await client.send_message(
            chat_id=chat_id,
            text=render_env_template(Config.MSG_AUTO_DELETED, minutes=mins),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(Config.BTN_GET_FILE, url=get_file_url)]
            ])
        )
    except Exception as e:
        print(f"Auto-delete prompt error: {e}")


async def deliver_files_with_5min_timer(
    client: Client,
    chat_id: int,
    user_id: int,
    batch: dict,
    qualities: list,
    retry_payload: str
):
    cfg = await get_settings()
    cap_tpl = cfg.get("caption_format", Config.DEFAULT_CAPTION_FORMAT)
    db_ch = await safe_resolve_chat(client, client, Config.DB_CHANNEL_ID)

    sent_ids = []
    for q in qualities:
        if q in batch.get("files", {}):
            custom_cap = build_bold_file_caption(
                batch["anime"],
                batch.get("season"),
                batch["episode"],
                batch.get("audio", "Tamil"),
                q,
                custom_template=cap_tpl
            )
            copied = await client.copy_message(
                chat_id=chat_id,
                from_chat_id=db_ch,
                message_id=batch["files"][q]["msg_id"],
                caption=custom_cap
            )
            sent_ids.append(copied.id)

    mins = max(1, AUTO_DELETE_SECONDS // 60)
    warn_msg = await client.send_message(
        chat_id=chat_id,
        text=render_env_template(Config.MSG_AUTO_DELETE_WARN, minutes=mins)
    )
    sent_ids.append(warn_msg.id)

    await increment_user_download_count(user_id)

    asyncio.create_task(
        schedule_auto_delete_and_get_file(client, chat_id, sent_ids, retry_payload)
    )
