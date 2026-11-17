import re
import html
from config import Config


def _clean_single_line(line: str) -> str:
    s = str(line).strip()
    while True:
        prev = s
        s = re.sub(r"^<\s*blockquote\s*>", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"<\s*/\s*blockquote\s*>$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"^<\s*b\s*>", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"<\s*/\s*b\s*>$", "", s, flags=re.IGNORECASE).strip()
        if s == prev:
            break
    return s


def _wrap_patch(line: str) -> str:
    clean = _clean_single_line(line)
    if not clean:
        return ""
    return f"<blockquote><b>{clean}</b></blockquote>"


def qline(text: str) -> str:
    if not text:
        return ""
    out = []
    for sub_line in str(text).replace("\\n", "\n").splitlines():
        wrapped = _wrap_patch(sub_line)
        if wrapped:
            out.append(wrapped)
    return "\n".join(out)


def qblocks(*lines: str) -> str:
    out = []
    for line in lines:
        if not line:
            continue
        for sub_line in str(line).replace("\\n", "\n").splitlines():
            wrapped = _wrap_patch(sub_line)
            if wrapped:
                out.append(wrapped)
    return "\n".join(out)


def qcard(header: str, *items: str, sub_sections: list = None, top_title: str = None) -> str:
    all_lines = []
    if top_title:
        all_lines.append(top_title)
    if header:
        all_lines.append(header)

    for item in items:
        if item:
            all_lines.append(item)

    if sub_sections:
        for sec_title, sec_items in sub_sections:
            if sec_title:
                all_lines.append(sec_title)
            for s_item in sec_items:
                if s_item:
                    all_lines.append(s_item)

    return qblocks(*all_lines)


def render_env_template(template_str: str, **kwargs) -> str:
    """
    Renders any multi-line template from .env into line-by-line <blockquote><b>...</b></blockquote>.
    Automatically skips any line containing {season} if season is None/empty!
    """
    raw_lines = template_str.replace("\\n", "\n").splitlines()
    season_val = kwargs.get("season")
    has_season = bool(season_val and str(season_val).strip() and str(season_val).strip().lower() != "none")

    rendered_lines = []
    for line in raw_lines:
        if not line.strip():
            continue
        if "{season}" in line.lower() and not has_season:
            continue

        cur = line
        for k, v in kwargs.items():
            val_str = "" if v is None else str(v)
            cur = re.sub(rf"\{{{re.escape(k)}\}}", val_str, cur, flags=re.IGNORECASE)

        if cur.strip():
            rendered_lines.append(cur.strip())

    return qblocks(*rendered_lines)


def parse_anime_filename(filename: str) -> dict:
    name_no_ext = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", filename).strip()
    clean = re.sub(r"[._]+", " ", name_no_ext).strip()

    q_match = re.search(r"\b(360p|420p|480p|720p|1080p)\b", clean, re.IGNORECASE)
    raw_q = q_match.group(1).lower() if q_match else None
    quality = "480p" if raw_q in ("360p", "420p") else raw_q

    season = None
    s_match = re.search(
        r"(?:\bSeason[\s\-:]*(\d{1,4})\b|\bS(\d{1,4})(?=\s*E(?:P)?\s*\d|\b|[\s\-_]))",
        clean,
        re.IGNORECASE
    )
    if s_match:
        s_val = s_match.group(1) or s_match.group(2)
        season = str(int(s_val))

    episode = None
    ep_match = re.search(
        r"(?:\bS\d{1,4}[\s\-_]*E(?:P)?[\s\-_]*(\d{1,4})\b|\b(?:EP|Episode|E)[\s\-_:]*(\d{1,4})\b)",
        clean,
        re.IGNORECASE
    )
    if ep_match:
        e_val = ep_match.group(1) or ep_match.group(2)
        episode = str(int(e_val))

    audio_langs = [
        "Tamil", "English", "Malayalam", "Telugu", "Hindi",
        "Japanese", "Kannada", "Dual Audio", "Multi Audio",
        "Tam", "Eng", "Mal", "Tel", "Hin", "Jap", "Jpn"
    ]
    lang_map = {
        "tam": "Tamil", "eng": "English", "mal": "Malayalam",
        "tel": "Telugu", "hin": "Hindi", "jap": "Japanese", "jpn": "Japanese"
    }

    found_audios = []
    bracket_contents = re.findall(r"[\[\(\{]([^\]\)\}]+)[\]\)\}]", name_no_ext)

    for b_text in bracket_contents:
        for lang in audio_langs:
            if re.search(rf"\b{lang}\b", b_text, re.IGNORECASE):
                mapped = lang_map.get(lang.lower(), lang.title())
                if mapped not in found_audios:
                    found_audios.append(mapped)

    if not found_audios:
        for lang in audio_langs:
            if re.search(rf"\b{lang}\b", clean, re.IGNORECASE):
                mapped = lang_map.get(lang.lower(), lang.title())
                if mapped not in found_audios:
                    found_audios.append(mapped)

    audio_str = ", ".join(found_audios) if found_audios else None

    clean_no_leading_tag = re.sub(r"^(?:\[[^\]]+\]|@[a-zA-Z0-9_]+)\s*[-–—]?\s*", "", clean).strip()
    title_cut = re.split(
        r"(?:\bS\d{1,4}[\s\-_]*E(?:P)?[\s\-_]*\d{1,4}\b|\bS\d{1,4}\b|\bSeason[\s\-:]*\d{1,4}\b|\b(?:EP|Episode|E)[\s\-_:]*\d{1,4}\b|\b(?:360p|420p|480p|720p|1080p)\b|\b(?:Tamil|English|Malayalam|Telugu|Hindi|Japanese|Multi\s*Audio|Dual\s*Audio)\b|[\[\(\{])",
        clean_no_leading_tag,
        maxsplit=1,
        flags=re.IGNORECASE
    )[0]
    anime_title = title_cut.strip(" -–—_:|")
    if not anime_title or anime_title.lower().startswith("gdrive ") or anime_title.lower() in ["download", "file", "episode file", "uc"]:
        anime_title = None

    return {
        "anime": anime_title,
        "season": season,
        "episode": episode,
        "audio": audio_str,
        "quality": quality
    }


def safe_template_replace(template_str: str, anime: str, season: str | None, episode: str, audio: str, quality: str) -> str:
    has_season = bool(season and str(season).strip() and str(season).strip().lower() != "none")
    try:
        s_num = f"{int(season):02d}" if has_season else ""
    except Exception:
        s_num = str(season).strip() if has_season else ""

    try:
        e_num = f"{int(episode):02d}"
    except Exception:
        e_num = str(episode or "1")

    res = template_str.strip()
    if res.startswith("(") and res.endswith(")"):
        res = res[1:-1].strip()

    if not has_season:
        res = re.sub(r"(?i)\bSeason[\s._\-:]*\{season\}[\s._\-]*", "", res)
        res = re.sub(r"(?i)\bS\{season\}(?=\s*E|\b|[\s._\-])", "", res)
        res = res.replace("{season}", "")

    replacements = {
        "{anime}": str(anime),
        "{season}": s_num,
        "{episode}": e_num,
        "{audio}": str(audio),
        "{quality}": str(quality.upper())
    }
    for k, v in replacements.items():
        res = re.sub(re.escape(k), v, res, flags=re.IGNORECASE)

    res = re.sub(r"\{([^{}]+)\}", r"\1", res)
    return res


def apply_custom_rename(rename_template: str, anime: str, season: str | None, episode: str, audio: str, quality: str) -> str:
    active_tpl = rename_template or Config.DEFAULT_RENAME_FORMAT
    has_season = bool(season and str(season).strip() and str(season).strip().lower() != "none")
    try:
        s_num = f"{int(season):02d}" if has_season else ""
    except Exception:
        s_num = str(season).strip() if has_season else ""

    try:
        e_num = f"{int(episode):02d}"
    except Exception:
        e_num = str(episode or "1")

    if not active_tpl:
        if has_season:
            out_name = f"{anime} - S{s_num}E{e_num} [{audio}] [{quality.upper()}].mkv"
        else:
            out_name = f"{anime} - E{e_num} [{audio}] [{quality.upper()}].mkv"
    else:
        out_name = safe_template_replace(active_tpl, anime, season, episode, audio, quality)

    out_name = re.sub(r"\s{2,}", " ", out_name)
    out_name = re.sub(r'[\\/*?:"<>|]', "", out_name).strip()
    out_name = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", out_name) + ".mkv"
    return out_name


def build_channel_post_caption(
    anime_esc: str,
    season_esc: str | None,
    ep_esc: str,
    audio_esc: str,
    qualities_list: list = None
) -> str:
    if qualities_list:
        q_str = " | ".join(q for q in ["480p", "720p", "1080p"] if q in qualities_list) or "480p | 720p | 1080p"
    else:
        q_str = "480p | 720p | 1080p"

    return render_env_template(
        Config.MSG_CHANNEL_POST,
        anime=anime_esc,
        season=season_esc,
        episode=ep_esc,
        audio=audio_esc,
        qualities=q_str
    )


def build_bold_file_caption(
    anime: str,
    season: str | None,
    episode: str,
    audio: str,
    quality: str,
    custom_template: str = ""
) -> str:
    has_season = bool(season and str(season).strip() and str(season).strip().lower() != "none")
    active_tpl = custom_template or Config.DEFAULT_CAPTION_FORMAT

    if active_tpl:
        rendered = safe_template_replace(
            active_tpl,
            str(anime),
            str(season) if has_season else None,
            str(episode),
            str(audio),
            str(quality.upper())
        )
        lines = []
        for line in rendered.replace("\\n", "\n").splitlines():
            ls = line.strip()
            if not ls:
                continue
            if not has_season and re.search(r"(?i)^\W*season\s*:", ls):
                continue
            lines.append(html.escape(ls))
        if lines:
            return qblocks(*lines)

    return qblocks(
        f"🎬 Anime : {html.escape(str(anime))}",
        f"📅 Season : {html.escape(str(season))}" if has_season else None,
        f"🔢 Episode : {html.escape(str(episode))}",
        f"🔊 Audio : {html.escape(str(audio))}",
        f"🎞️ Quality : {html.escape(str(quality.upper()))}"
    )
