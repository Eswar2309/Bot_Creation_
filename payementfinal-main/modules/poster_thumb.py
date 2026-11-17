import io
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client
from database import release_memory


def load_font(size: int):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


async def stamp_season_episode_on_poster(
    client: Client,
    poster_file_id: str,
    season: str | None,
    episode: str
) -> io.BytesIO:
    photo_bytes_io = await client.download_media(poster_file_id, in_memory=True)
    img = Image.open(photo_bytes_io).convert("RGBA")
    width, height = img.size

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    s_clean = str(season).strip() if (season and str(season).strip().lower() != "none") else ""
    e_clean = str(episode).strip() if episode else "1"
    e_text = f"EPISODE {e_clean}" if len(e_clean) <= 3 else f"EP {e_clean}"

    badge_h = max(48, int(height * 0.095))
    font_size = max(16, int(badge_h * 0.42))
    font = load_font(font_size)

    e_bbox = draw.textbbox((0, 0), e_text, font=font)
    e_w, e_h = e_bbox[2] - e_bbox[0], e_bbox[3] - e_bbox[1]
    pad_x = max(18, int(width * 0.022))

    margin_x = int(width * 0.028)
    margin_y = int(height * 0.028)
    y1 = margin_y
    y2 = margin_y + badge_h
    radius = max(10, int(badge_h * 0.25))

    if s_clean:
        s_text = f"S{s_clean}" if len(s_clean) > 2 else f"SEASON {s_clean}"
        s_bbox = draw.textbbox((0, 0), s_text, font=font)
        s_w, s_h = s_bbox[2] - s_bbox[0], s_bbox[3] - s_bbox[1]

        left_w = s_w + (pad_x * 2)
        right_w = e_w + (pad_x * 2)
        badge_w = left_w + right_w

        x2 = width - margin_x
        x1 = max(margin_x, x2 - badge_w)

        draw.rounded_rectangle(
            [x1, y1, x2, y2],
            radius=radius,
            fill=(10, 15, 28, 245),
            outline=(0, 210, 255, 255),
            width=max(2, int(width * 0.0035))
        )

        split_x = x1 + left_w
        draw.rounded_rectangle(
            [x1, y1, split_x, y2],
            radius=radius,
            fill=(0, 180, 245, 255)
        )

        s_x = x1 + (left_w - s_w) / 2
        s_y = y1 + (badge_h - s_h) / 2 - (s_bbox[1] / 2)
        draw.text((s_x, s_y), s_text, font=font, fill=(10, 15, 28, 255))

        e_x = split_x + ((x2 - split_x) - e_w) / 2
        e_y = y1 + (badge_h - e_h) / 2 - (e_bbox[1] / 2)
        draw.text((e_x, e_y), e_text, font=font, fill=(255, 255, 255, 255))
    else:
        badge_w = e_w + (pad_x * 2)
        x2 = width - margin_x
        x1 = max(margin_x, x2 - badge_w)

        draw.rounded_rectangle(
            [x1, y1, x2, y2],
            radius=radius,
            fill=(10, 15, 28, 245),
            outline=(0, 210, 255, 255),
            width=max(2, int(width * 0.0035))
        )
        e_x = x1 + (badge_w - e_w) / 2
        e_y = y1 + (badge_h - e_h) / 2 - (e_bbox[1] / 2)
        draw.text((e_x, e_y), e_text, font=font, fill=(255, 255, 255, 255))

    final_img = Image.alpha_composite(img, overlay).convert("RGB")
    out_bio = io.BytesIO()
    out_bio.name = f"poster_E{e_clean}.jpg"
    final_img.save(out_bio, format="JPEG", quality=92)
    out_bio.seek(0)

    img.close()
    overlay.close()
    final_img.close()
    photo_bytes_io.close()
    release_memory()
    return out_bio
