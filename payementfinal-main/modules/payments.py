import io
import re
import html
import time
import hashlib
import secrets
import qrcode
import aiohttp
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from PIL import Image, ImageOps, ImageEnhance
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import Config
from database import batches_col, payments_col, safe_edit_message, get_settings
from modules.formatting import qline, qcard, render_env_template
from modules.fsub_delivery import deliver_files_with_5min_timer


def compute_image_fingerprints(file_bytes: bytes) -> tuple[str, str]:
    sha256_hex = hashlib.sha256(file_bytes).hexdigest()
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())
        bits = []
        for row in range(8):
            for col in range(8):
                left = pixels[row * 9 + col]
                right = pixels[row * 9 + col + 1]
                bits.append("1" if left > right else "0")
        dhash_hex = f"{int(''.join(bits), 2):016x}"
        img.close()
    except Exception:
        dhash_hex = sha256_hex[:16]
    return sha256_hex, dhash_hex


def preprocess_receipt_for_ocr(file_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        w, h = img.size
        if max(w, h) > 1600:
            scale = 1600.0 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(img)
        gray = ImageOps.autocontrast(gray, cutoff=1)
        gray = ImageEnhance.Sharpness(gray).enhance(1.8)
        out = io.BytesIO()
        gray.save(out, format="JPEG", quality=88)
        img.close()
        gray.close()
        return out.getvalue()
    except Exception:
        return file_bytes


def prepare_qr_variants_for_decoding(file_bytes: bytes) -> list[bytes]:
    """
    Prepares lightweight (<250KB) high-contrast JPEG variants of GPay/PhonePe
    screenshots so api.qrserver.com never rejects them with 400 Bad Request.
    """
    variants = []
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        w, h = img.size

        # 1. Center-cropped square (strips GPay/PhonePe header, status bar & buttons)
        if h > w * 1.15:
            crop_size = int(w * 0.92)
            left = (w - crop_size) // 2
            top = (h - crop_size) // 2
            center_crop = img.crop((left, top, left + crop_size, top + crop_size))
            center_crop = ImageOps.autocontrast(ImageOps.grayscale(center_crop), cutoff=2)
            if max(center_crop.size) > 900:
                center_crop.thumbnail((900, 900), Image.Resampling.LANCZOS)
            buf_crop = io.BytesIO()
            center_crop.save(buf_crop, format="JPEG", quality=85)
            variants.append(buf_crop.getvalue())
            center_crop.close()

        # 2. Full image resized safely under 1000px (well under 1MB API limit)
        full_img = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
        if max(full_img.size) > 1000:
            full_img.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
        buf_full = io.BytesIO()
        full_img.save(buf_full, format="JPEG", quality=85)
        variants.append(buf_full.getvalue())

        full_img.close()
        img.close()
    except Exception as e:
        print(f"QR Preprocess notice: {e}")
        variants.append(file_bytes)

    return variants


def is_valid_upi_transaction_id(tx_id: str) -> bool:
    if not tx_id:
        return False
    clean = re.sub(r"[\s\-]+", "", tx_id.strip()).upper()

    if len(set(clean)) <= 3 or "123456" in clean or "654321" in clean or "000000" in clean:
        return False

    if clean.isdigit() and len(clean) == 12:
        return True

    if clean.startswith("T") and clean[1:].isdigit() and 16 <= len(clean) <= 26:
        return True

    digit_count = sum(c.isdigit() for c in clean)
    if clean.isalnum() and 10 <= len(clean) <= 24 and digit_count >= 8:
        return True

    return False


def extract_valid_tx_id_from_text(raw_text: str) -> str:
    if not raw_text:
        return ""

    cleaned = raw_text.replace("\r", "\n")

    labeled_patterns = [
        r"(?:UTR|UPI\s*Ref(?:\.\s*|\s+)No|Ref(?:\.\s*|\s+)No|Transaction\s*ID|Google\s*transaction\s*ID|Order\s*ID)[\s:.\-#]*([A-Za-z0-9]{10,28})",
        r"\b(T\d{18,25})\b",
        r"\b(\d{12})\b"
    ]
    for pat in labeled_patterns:
        for m in re.finditer(pat, cleaned, re.IGNORECASE):
            cand = m.group(1).strip()
            if len(cand) == 12:
                cand_num = cand.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
                if is_valid_upi_transaction_id(cand_num):
                    return cand_num
            if is_valid_upi_transaction_id(cand):
                return cand.upper()

    spaced_12 = re.findall(r"\b(\d{4}\s?\d{4}\s?\d{4}|\d{6}\s?\d{6}|\d{12})\b", cleaned)
    for item in spaced_12:
        cand = re.sub(r"\s+", "", item)
        if is_valid_upi_transaction_id(cand):
            return cand

    phonepe_ids = re.findall(r"\b(T[0-9O]{16,25})\b", cleaned, re.IGNORECASE)
    for pid in phonepe_ids:
        norm_pid = "T" + pid[1:].replace("O", "0").replace("o", "0")
        if is_valid_upi_transaction_id(norm_pid):
            return norm_pid

    return ""


async def extract_utr_from_payment_screenshot(file_bytes: bytes) -> tuple[str, str]:
    url = "https://api.ocr.space/parse/image"
    api_key = getattr(Config, "OCR_API_KEY", "helloworld")
    enhanced_bytes = preprocess_receipt_for_ocr(file_bytes)

    for engine in ("2", "1"):
        for img_payload in (enhanced_bytes, file_bytes):
            form = aiohttp.FormData()
            form.add_field("apikey", api_key)
            form.add_field("language", "eng")
            form.add_field("OCREngine", engine)
            form.add_field("scale", "true")
            form.add_field("file", img_payload, filename="receipt.jpg", content_type="image/jpeg")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, data=form, timeout=22) as resp:
                        data = await resp.json(content_type=None)
                        parsed_list = data.get("ParsedResults", [])
                        if parsed_list:
                            raw_text = parsed_list[0].get("ParsedText", "")
                            tx_id = extract_valid_tx_id_from_text(raw_text)
                            if tx_id:
                                return tx_id, raw_text
            except Exception as e:
                print(f"Screenshot OCR Engine {engine} notice: {e}")

    return "", ""


def inject_amount_into_upi_uri(base_upi_uri: str, amount: int, tr_ref: str = "") -> str:
    """
    Safely injects amount and note into GPay / PhonePe / Paytm UPI QR links.
    Removes cryptographic merchant signature params (sign, orgid, tid, tr) that
    would otherwise invalidate the QR when 'am' is modified.
    """
    amt_str = f"{float(amount):.2f}"
    if base_upi_uri and base_upi_uri.lower().startswith("upi://pay"):
        parsed = urlparse(base_upi_uri)
        raw_params = dict(parse_qsl(parsed.query, keep_blank_values=False))

        # Keep safe payee details; strip locked merchant signature/order fields
        blocked_keys = {"sign", "orgid", "tid", "tr", "url", "mam", "mode"}
        params = {k: v for k, v in raw_params.items() if k.lower() not in blocked_keys}

        params["am"] = amt_str
        params["cu"] = "INR"
        if tr_ref:
            params["tn"] = f"VIP_{tr_ref}"

        new_query = urlencode(params, safe="@.-_")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    note = f"&tn=VIP_{tr_ref}" if tr_ref else ""
    return f"upi://pay?pa={base_upi_uri.strip()}&am={amt_str}&cu=INR{note}"


def generate_qr_image_bytes(upi_string: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    bio.name = "upi_1rs_qr.png"
    img.save(bio, "PNG")
    bio.seek(0)
    return bio


async def decode_qr_from_file_bytes(file_bytes: bytes) -> str:
    """
    Decodes QR codes from GPay, PhonePe, Paytm, or standard gallery images
    without triggering 400 Bad Request or aiohttp mimetype errors.
    """
    url = "https://api.qrserver.com/v1/read-qr-code/"
    image_variants = prepare_qr_variants_for_decoding(file_bytes)

    async with aiohttp.ClientSession() as session:
        for idx, img_bytes in enumerate(image_variants):
            form = aiohttp.FormData()
            form.add_field("file", img_bytes, filename="qr.jpg", content_type="image/jpeg")
            try:
                async with session.post(url, data=form, timeout=15) as resp:
                    if resp.status != 200:
                        err_body = await resp.text()
                        print(f"QR API attempt {idx + 1} HTTP {resp.status}: {err_body[:150]}")
                        continue

                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and data:
                        symbols = data[0].get("symbol", [])
                        if symbols and symbols[0].get("data"):
                            return symbols[0]["data"].strip()
                        elif symbols and symbols[0].get("error"):
                            print(f"QR API attempt {idx + 1} symbol notice: {symbols[0]['error']}")
            except Exception as e:
                print(f"QR Decode Error (attempt {idx + 1}): {e}")

    return ""


async def send_auto_1rs_qr_payment(client: Client, message: Message, batch_id: str):
    batch = await batches_col.find_one({"_id": batch_id})
    if not batch:
        await message.reply_text(qline("❌ Invalid episode batch."))
        return

    cfg = await get_settings()
    upi_id = cfg.get("upi_id", Config.UPI_ID)
    amount = cfg.get("pay_amount", Config.PAYMENT_AMOUNT_INR)
    raw_upi_uri = cfg.get("raw_upi_uri") or upi_id

    session_ref = f"{batch_id[-4:]}{message.from_user.id % 10000:04d}"
    final_upi_uri = inject_amount_into_upi_uri(raw_upi_uri, amount, tr_ref=session_ref)
    qr_image_bio = generate_qr_image_bytes(final_upi_uri)

    avail_q = " | ".join(q for q in ["480p", "720p", "1080p"] if q in batch.get("files", {})) or "480p | 720p | 1080p"

    await payments_col.update_one(
        {"user_id": message.from_user.id, "status": "waiting_utr"},
        {
            "$set": {
                "batch_id": batch_id,
                "amount": amount,
                "upi_id": upi_id,
                "session_ref": session_ref,
                "qr_sent_at": int(time.time())
            }
        },
        upsert=True
    )

    season_val = batch.get("season")
    has_season = bool(season_val and str(season_val).strip() and str(season_val).strip().lower() != "none")

    caption = render_env_template(
        Config.MSG_VIP_PAYMENT,
        anime=html.escape(batch["anime"]),
        season=html.escape(str(season_val)) if has_season else None,
        episode=html.escape(str(batch["episode"])),
        audio=html.escape(batch.get("audio", "Tamil")),
        qualities=avail_q,
        amount=amount,
        upi_id=html.escape(upi_id)
    )

    cancel_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(Config.BTN_CANCEL_PAY, callback_data="pay_cancel")]
    ])

    await message.reply_photo(photo=qr_image_bio, caption=caption, reply_markup=cancel_markup)


async def verify_and_approve_utr(
    client: Client,
    message: Message,
    user_id: int,
    utr: str,
    img_sha256: str = "",
    img_dhash: str = ""
):
    clean_utr = re.sub(r"[\s\-]+", "", utr.strip()).upper()

    existing_utr = await payments_col.find_one({"utr": clean_utr, "status": "verified"})
    if existing_utr:
        await message.reply_text(
            qcard(
                "❌ Duplicate Transaction ID Detected!",
                f"🚫 Transaction ID {html.escape(clean_utr)} has already been used.",
                "⚠️ Reusing old Transaction IDs or screenshots is strictly blocked."
            )
        )
        return

    if img_sha256 or img_dhash:
        hash_query = []
        if img_sha256:
            hash_query.append({"img_sha256": img_sha256, "status": "verified"})
        if img_dhash:
            hash_query.append({"img_dhash": img_dhash, "status": "verified"})
        if hash_query:
            dup_img = await payments_col.find_one({"$or": hash_query})
            if dup_img:
                await message.reply_text(
                    qcard(
                        "❌ Duplicate Payment Screenshot Detected!",
                        "🚫 This payment screenshot has already been used to unlock a file.",
                        "📲 Please make a fresh payment and send the new receipt."
                    )
                )
                return

    pending_pay = await payments_col.find_one({"user_id": user_id, "status": "waiting_utr"})
    if not pending_pay:
        await message.reply_text(
            qcard(
                "⚠️ No Active Payment Session!",
                "💎 Click the Direct Files (VIP Payment) button in the channel post first."
            )
        )
        return

    cfg = await get_settings()
    expected_amt = int(float(pending_pay.get("amount") or cfg.get("pay_amount", Config.PAYMENT_AMOUNT_INR)))
    checking_msg = await message.reply_text(qline(f"🔄 Verifying Transaction ID {html.escape(clean_utr)}..."))

    if not is_valid_upi_transaction_id(clean_utr):
        await safe_edit_message(
            checking_msg,
            qcard(
                "❌ Invalid Transaction ID / UTR!",
                "📲 Please send a valid 12-digit UTR or PhonePe / GPay Transaction ID or upload your Payment Screenshot."
            )
        )
        return

    batch_id = pending_pay["batch_id"]
    batch = await batches_col.find_one({"_id": batch_id})
    if not batch:
        await safe_edit_message(checking_msg, qline("❌ Associated episode files were not found."))
        return

    verify_code = f"VERIFY-{secrets.token_hex(3).upper()}"
    update_fields = {
        "utr": clean_utr,
        "status": "verified",
        "verify_code": verify_code,
        "redeemed": True,
        "verified_at": int(time.time())
    }
    if img_sha256:
        update_fields["img_sha256"] = img_sha256
    if img_dhash:
        update_fields["img_dhash"] = img_dhash

    await payments_col.update_one(
        {"_id": pending_pay["_id"]},
        {"$set": update_fields}
    )

    avail_qualities = [q for q in ["480p", "720p", "1080p"] if q in batch.get("files", {})]

    await safe_edit_message(
        checking_msg,
        render_env_template(
            Config.MSG_PAYMENT_VERIFIED,
            utr=html.escape(clean_utr),
            amount=expected_amt,
            verify_code=verify_code
        )
    )

    await deliver_files_with_5min_timer(
        client=client,
        chat_id=message.chat.id,
        user_id=user_id,
        batch=batch,
        qualities=avail_qualities,
        retry_payload=f"all_{batch_id}"
    )
    
