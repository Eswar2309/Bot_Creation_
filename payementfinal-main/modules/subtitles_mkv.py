import os
import re
import shutil
import asyncio
from database import FFMPEG_BIN, AUDIO_LANG_ALIASES, release_memory


def format_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis -= 1000
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


async def get_media_info(input_video: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_BIN, "-hide_banner", "-i", input_video,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        out = stderr.decode("utf-8", errors="ignore")

        dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
        duration = 0.0
        if dur_match:
            h, m, s = dur_match.groups()
            duration = int(h) * 3600 + int(m) * 60 + float(s)

        total_bitrate_kbps = 0
        br_match = re.search(r"Duration:[^\n]*bitrate:\s*(\d+)\s*kb/s", out)
        if br_match:
            total_bitrate_kbps = int(br_match.group(1))

        width, height = 1280, 720
        res_match = re.search(r"Stream #0:\d+[^\n]*Video:[^\n]*?\b(\d{3,4})x(\d{3,4})\b", out)
        if res_match:
            width, height = int(res_match.group(1)), int(res_match.group(2))

        streams = []
        audio_streams = []

        stream_blocks = re.split(r"(?=Stream #0:\d+)", out)
        for blk in stream_blocks:
            m_sub = re.match(r"Stream #0:(\d+)(?:\(([^)]+)\))?:\s*Subtitle:\s*([a-zA-Z0-9_]+)", blk)
            if m_sub:
                streams.append({
                    "index": int(m_sub.group(1)),
                    "lang": (m_sub.group(2) or "").lower(),
                    "codec_type": "subtitle",
                    "codec_name": m_sub.group(3).lower()
                })
                continue

            m_aud = re.match(r"Stream #0:(\d+)(?:\(([^)]+)\))?:\s*Audio:\s*([a-zA-Z0-9_]+)", blk)
            if m_aud:
                title_m = re.search(r"title\s*:\s*([^\r\n]+)", blk, re.IGNORECASE)
                aud_br_m = re.search(r"(\d+)\s*kb/s", blk)
                audio_streams.append({
                    "index": int(m_aud.group(1)),
                    "audio_pos": len(audio_streams),
                    "lang": (m_aud.group(2) or "und").strip().lower(),
                    "codec_name": m_aud.group(3).lower(),
                    "title": (title_m.group(1).strip().lower() if title_m else ""),
                    "kbps": int(aud_br_m.group(1)) if aud_br_m else 128
                })

        return {
            "format": {"duration": str(duration), "bitrate_kbps": total_bitrate_kbps},
            "width": width,
            "height": height,
            "streams": streams,
            "audio_streams": audio_streams
        }
    except Exception as e:
        print(f"Media info probe error: {e}")
        return {"format": {"duration": "0", "bitrate_kbps": 0}, "width": 1280, "height": 720, "streams": [], "audio_streams": []}


def select_matching_audio_stream(audio_streams: list, preferred_lang: str) -> tuple[dict | None, str | None, bool]:
    if not preferred_lang or preferred_lang.lower() in ("all", "default", "keep_all", "none"):
        return None, None, False

    target_key = preferred_lang.strip().lower()
    aliases = AUDIO_LANG_ALIASES.get(target_key, [target_key])
    canonical_name = target_key.title()

    if len(audio_streams) <= 1:
        return (audio_streams[0] if audio_streams else None), canonical_name, False

    for a_stream in audio_streams:
        lang_code = a_stream.get("lang", "").lower()
        track_title = a_stream.get("title", "").lower()
        for alias in aliases:
            if lang_code == alias or re.search(rf"\b{re.escape(alias)}\b", track_title):
                return a_stream, canonical_name, True

    for a_stream in audio_streams:
        lang_code = a_stream.get("lang", "").lower()
        track_title = a_stream.get("title", "").lower()
        for alias in aliases:
            if alias in lang_code or alias in track_title:
                return a_stream, canonical_name, True

    return None, canonical_name, False


async def extract_and_watermark_subs(input_video: str, watermark_text: str, temp_dir: str, duration_sec: float = 0.0) -> list:
    try:
        info = await get_media_info(input_video)
        if duration_sec <= 0:
            duration_sec = float(info.get("format", {}).get("duration", 0) or 0)
        sub_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]

        tail_start = max(31.0, duration_sec - 30.0) if duration_sec > 45.0 else 0.0
        tail_end = max(tail_start + 1.0, duration_sec) if tail_start > 0 else 0.0

        if not sub_streams:
            standalone_srt = os.path.join(temp_dir, "sub_0.srt")
            with open(standalone_srt, "w", encoding="utf-8") as f:
                f.write(f"1\n00:00:00,000 --> 00:00:30,000\n{watermark_text}\n\n")
                if tail_start > 0:
                    f.write(f"2\n{format_srt_timestamp(tail_start)} --> {format_srt_timestamp(tail_end)}\n{watermark_text}\n\n")
            return [standalone_srt]

        extracted_subs = []
        for idx, s in enumerate(sub_streams):
            codec = s.get("codec_name", "srt")
            if codec in ["hdmv_pgs_subtitle", "dvd_subtitle"]:
                continue

            extract_ext = ".ass" if codec in ["ass", "ssa"] else ".srt"
            temp_extract_file = os.path.join(temp_dir, f"temp_raw_sub_{idx}{extract_ext}")
            final_srt_file = os.path.join(temp_dir, f"sub_{idx}.srt")
            raw_index = s.get("index")

            process = await asyncio.create_subprocess_exec(
                FFMPEG_BIN, "-y", "-i", input_video, "-map", f"0:{raw_index}", "-c", "copy", temp_extract_file,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()

            if os.path.exists(temp_extract_file):
                try:
                    with open(temp_extract_file, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    first_30_block = f"1\n00:00:00,000 --> 00:00:30,000\n{watermark_text}"

                    if extract_ext == ".ass":
                        events_section = False
                        format_cols = []
                        srt_lines = [first_30_block + "\n"]
                        srt_index = 2

                        for line in content.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("[Events]"):
                                events_section = True
                                continue
                            elif line.startswith("[") and events_section:
                                events_section = False

                            if events_section:
                                if line.startswith("Format:"):
                                    format_cols = [c.strip() for c in line.replace("Format:", "").split(",")]
                                elif line.startswith("Dialogue:") and format_cols:
                                    c_part = line.split("Dialogue:", 1)[1].strip()
                                    vals = c_part.split(",", len(format_cols) - 1)
                                    if len(vals) == len(format_cols):
                                        d_map = dict(zip(format_cols, vals))
                                        start_ass = d_map.get("Start", "0:00:00.00")
                                        end_ass = d_map.get("End", "0:00:00.00")

                                        def to_sec(t):
                                            parts = t.split(":")
                                            if len(parts) != 3:
                                                return 0
                                            try:
                                                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2].split(".")[0])
                                            except Exception:
                                                return 0

                                        s_sec = to_sec(start_ass)
                                        if s_sec < 30 or (tail_start > 0 and s_sec >= tail_start):
                                            continue

                                        def to_srt_time(t):
                                            parts = t.split(":")
                                            if len(parts) != 3:
                                                return "00:00:00,000"
                                            h, m, s_c = parts[0], parts[1], parts[2]
                                            s_parts = s_c.split(".")
                                            s_v, cs = s_parts[0], s_parts[1] if len(s_parts) > 1 else "00"
                                            ms = cs + "0" if len(cs) == 2 else cs.ljust(3, "0")
                                            return f"{int(h):02d}:{int(m):02d}:{int(s_v):02d},{ms}"

                                        start_srt = to_srt_time(start_ass)
                                        end_srt = to_srt_time(end_ass)
                                        text_srt = d_map.get("Text", "").replace("\\N", "\n").replace("\\n", "\n")
                                        text_srt = re.sub(r"\{.*?\}", "", text_srt)

                                        srt_lines.append(f"{srt_index}\n{start_srt} --> {end_srt}\n{text_srt}\n")
                                        srt_index += 1

                        if tail_start > 0:
                            srt_lines.append(
                                f"{srt_index}\n{format_srt_timestamp(tail_start)} --> {format_srt_timestamp(tail_end)}\n{watermark_text}\n"
                            )
                        content = "\n".join(srt_lines)
                    else:
                        srt_out_lines = [first_30_block + "\n"]
                        srt_idx = 2
                        current_block = []
                        keep_block = True

                        for line in content.splitlines():
                            line = line.strip()
                            if not line:
                                if current_block:
                                    if keep_block and len(current_block) > 2:
                                        current_block[0] = str(srt_idx)
                                        srt_out_lines.append("\n".join(current_block) + "\n")
                                        srt_idx += 1
                                    current_block = []
                                    keep_block = True
                                continue

                            if len(current_block) == 1 and "-->" in line:
                                st = line.split("-->")[0].strip()
                                parts = st.split(":")
                                if len(parts) >= 3:
                                    try:
                                        h, m, s_ms = parts[0][-2:], parts[1], parts[2]
                                        s_v = s_ms.split(",")[0].split(".")[0]
                                        secs = int(h) * 3600 + int(m) * 60 + int(s_v)
                                        if secs < 30 or (tail_start > 0 and secs >= tail_start):
                                            keep_block = False
                                    except Exception:
                                        pass
                            current_block.append(line)

                        if current_block and keep_block and len(current_block) > 2:
                            current_block[0] = str(srt_idx)
                            srt_out_lines.append("\n".join(current_block) + "\n")
                            srt_idx += 1

                        if tail_start > 0:
                            srt_out_lines.append(
                                f"{srt_idx}\n{format_srt_timestamp(tail_start)} --> {format_srt_timestamp(tail_end)}\n{watermark_text}\n"
                            )
                        content = "\n".join(srt_out_lines)

                    with open(final_srt_file, "w", encoding="utf-8") as f:
                        f.write(content)
                    try:
                        os.remove(temp_extract_file)
                    except Exception:
                        pass
                    extracted_subs.append(final_srt_file)
                    break
                except Exception as e:
                    print(f"Failed to patch sub {idx}: {e}")
        return extracted_subs
    except Exception as e:
        print(f"Error extracting subs: {e}")
        return []


def escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


async def process_video_to_mkv(
    input_path: str,
    output_mkv_path: str,
    main_watermark: str,
    sub_watermark: str,
    preferred_audio_lang: str,
    hardburn_video_wm: bool,
    temp_dir: str
) -> tuple[str, int, int, int, str | None]:
    info = await get_media_info(input_path)
    duration_float = float(info.get("format", {}).get("duration", 0) or 0)
    duration_sec = int(duration_float)
    width = int(info.get("width", 1280) or 1280)
    height = int(info.get("height", 720) or 720)
    audio_streams = info.get("audio_streams", [])
    orig_file_bytes = os.path.getsize(input_path)

    matched_audio_stream, detected_lang_label, needs_audio_strip = select_matching_audio_stream(
        audio_streams, preferred_audio_lang
    )

    if not main_watermark and not sub_watermark and not needs_audio_strip and input_path.lower().endswith(".mkv"):
        shutil.move(input_path, output_mkv_path)
        return output_mkv_path, duration_sec, width, height, detected_lang_label

    patched_subs = []
    if sub_watermark:
        patched_subs = await extract_and_watermark_subs(
            input_path,
            sub_watermark,
            temp_dir,
            duration_sec=duration_float
        )

    cmd = [FFMPEG_BIN, "-y", "-fflags", "+genpts", "-i", input_path]
    for sub_path in patched_subs:
        if sub_path and os.path.exists(sub_path):
            cmd.extend(["-i", sub_path])

    cmd.extend(["-map", "0:v:0"])

    if needs_audio_strip and matched_audio_stream is not None:
        cmd.extend(["-map", f"0:{matched_audio_stream['index']}"])
        if preferred_audio_lang and preferred_audio_lang.lower() in AUDIO_LANG_ALIASES:
            iso_code = AUDIO_LANG_ALIASES[preferred_audio_lang.lower()][0]
            cmd.extend([
                "-metadata:s:a:0", f"language={iso_code}",
                "-metadata:s:a:0", f"title={detected_lang_label}",
                "-disposition:a:0", "default"
            ])
    elif matched_audio_stream is not None and len(audio_streams) == 1:
        cmd.extend(["-map", "0:a:0", "-disposition:a:0", "default"])
    else:
        cmd.extend(["-map", "0:a?"])

    if patched_subs:
        for i in range(len(patched_subs)):
            cmd.extend(["-map", f"{i + 1}:0"])
        cmd.extend(["-c:s", "srt", "-disposition:s:0", "default+forced"])
    else:
        cmd.extend(["-map", "0:s?", "-c:s", "copy"])

    if (main_watermark or sub_watermark) and hardburn_video_wm:
        font_file = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font_opt = f"fontfile={font_file}:" if os.path.exists(font_file) else ""
        vf_parts = []

        if main_watermark:
            wm_escaped = escape_drawtext(main_watermark)
            vf_parts.append(
                f"drawtext={font_opt}text='{wm_escaped}':"
                f"fontcolor=white@0.92:fontsize=h/28:"
                f"borderw=2:bordercolor=black@0.85:"
                f"shadowx=2:shadowy=2:shadowcolor=black@0.75:"
                f"x=w-tw-24:y=24"
            )

        if sub_watermark:
            sub_escaped = escape_drawtext(sub_watermark)
            tail_start = max(31.0, duration_float - 30.0) if duration_float > 45.0 else 0.0
            if tail_start > 0:
                enable_expr = f"between(t,0,30)+between(t,{tail_start:.1f},{duration_float + 5:.1f})"
            else:
                enable_expr = "between(t,0,30)"
            vf_parts.append(
                f"drawtext={font_opt}text='{sub_escaped}':"
                f"fontcolor=white@0.96:fontsize=h/25:"
                f"borderw=2:bordercolor=black@0.90:"
                f"shadowx=2:shadowy=2:shadowcolor=black@0.80:"
                f"x=(w-tw)/2:y=h-th-42:enable='{enable_expr}'"
            )

        aud_kbps = matched_audio_stream.get("kbps", 128) if matched_audio_stream else 128
        if duration_float > 10:
            total_file_kbps = int((orig_file_bytes * 8) / (duration_float * 1000))
            target_v_kbps = max(350, total_file_kbps - aud_kbps)
        else:
            target_v_kbps = 1200

        cmd.extend([
            "-vf", ",".join(vf_parts),
            "-c:v", "libx264",
            "-preset", "superfast",
            "-b:v", f"{target_v_kbps}k",
            "-x264-params", "rc-lookahead=10:ref=1:bframes=2:threads=2",
            "-threads", "2",
            "-pix_fmt", "yuv420p"
        ])
    else:
        cmd.extend(["-c:v", "copy"])

    if main_watermark:
        cmd.extend(["-metadata", f"title={main_watermark}"])

    cmd.extend(["-c:a", "copy", "-max_muxing_queue_size", "2048", output_mkv_path])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    if not os.path.exists(output_mkv_path) or os.path.getsize(output_mkv_path) == 0:
        err_msg = stderr.decode("utf-8", errors="ignore")[-400:]
        raise RuntimeError(f"FFmpeg MKV processing failed: {err_msg}")

    try:
        if os.path.exists(input_path):
            os.remove(input_path)
    except Exception:
        pass

    release_memory()
    return output_mkv_path, duration_sec, width, height, detected_lang_label
