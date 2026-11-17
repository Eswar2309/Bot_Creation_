import os
import re
import html
import time
import shutil
import asyncio
import aiohttp
from urllib.parse import urlparse, parse_qsl, unquote, urljoin
from database import release_memory

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    CurlAsyncSession = None
    HAS_CURL_CFFI = False

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    cloudscraper = None
    HAS_CLOUDSCRAPER = False

TELEGRAM_PREMIUM_MAX_BYTES = 4 * 1024 * 1024 * 1024

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def validate_downloaded_media(file_path: str) -> None:
    """
    Verifies that the downloaded file is a real binary video/archive and NOT
    an HTML error page, expired token response, or 0-byte stub before FFmpeg runs.
    """
    if not os.path.exists(file_path):
        raise RuntimeError("Downloaded file does not exist on disk.")

    size = os.path.getsize(file_path)
    if size < 100 * 1024:  # Less than 100 KB cannot be a valid video episode
        with open(file_path, "rb") as f:
            head_bytes = f.read(512)
        snippet = head_bytes.decode("utf-8", errors="ignore").strip()
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet)[:140]
        os.remove(file_path)
        raise RuntimeError(
            f"Download returned an invalid/HTML file ({size} bytes) instead of video: {snippet or 'Empty response'}"
        )

    with open(file_path, "rb") as f:
        header = f.read(64)

    low_head = header.lstrip().lower()
    if low_head.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"{")):
        text_preview = header.decode("utf-8", errors="ignore").strip()[:100]
        os.remove(file_path)
        raise RuntimeError(
            f"CDN served an HTML/JSON error page instead of the video stream ({text_preview})."
        )


def get_premium_session_client():
    for mod_name in ("database", "bot", "client", "app", "config"):
        try:
            mod = __import__(mod_name, fromlist=["*"])
            for attr in ("user_client", "user_app", "userbot", "premium_client", "ubot"):
                client = getattr(mod, attr, None)
                if client is not None and hasattr(client, "download_media"):
                    return client
        except Exception:
            continue
    return None


def clean_extracted_filename(raw_name: str, default_ext: str = ".mkv") -> str:
    if not raw_name:
        return f"video_{int(time.time())}{default_ext}"

    name = html.unescape(unquote(unquote(raw_name))).strip()

    name = re.sub(
        r"\s*[-|•]\s*(?:Google Drive|GDFlix|HubCloud|TGFiles|Download|Stream|Watch Online|GamerXYT|FSL).*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    name = re.sub(
        r"^(?:gdrive_|hubcloud_|gdflix_|tgfiles_)+",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()

    base, ext = os.path.splitext(name)
    if not ext or len(ext) > 6:
        name = f"{name}{default_ext}"

    return name


def extract_gdrive_file_id(url: str) -> str:
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def extract_filename_from_url_or_headers(url: str, headers: dict) -> str:
    ci_headers = {k.lower(): v for k, v in (headers or {}).items()}
    cd = ci_headers.get("content-disposition", "")
    if cd:
        m_star = re.search(
            r"filename\*\s*=\s*(?:UTF-8'')?([^;\r\n]+)", cd, re.IGNORECASE
        )
        if m_star:
            return clean_extracted_filename(m_star.group(1).strip().strip('"').strip("'"))

        m_std = re.search(
            r'filename\s*=\s*["\']?([^"\';\r\n]+)["\']?', cd, re.IGNORECASE
        )
        if m_std:
            return clean_extracted_filename(m_std.group(1))

    parsed = urlparse(url)
    qs = dict(parse_qsl(parsed.query))
    rcd = qs.get("response-content-disposition", "")
    if rcd:
        m = re.search(
            r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', rcd, re.IGNORECASE
        )
        if m:
            return clean_extracted_filename(m.group(1))

    path_name = unquote(unquote(os.path.basename(parsed.path))).strip()
    if (
        path_name
        and "." in path_name
        and path_name.lower()
        not in ["uc", "download", "view", "index.php", "drive", "hubcloud.php", "file"]
    ):
        return clean_extracted_filename(path_name)

    return ""


async def browser_fetch(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    referer: str = "",
    cookies: dict | None = None,
    timeout: int = 25,
) -> tuple[int, str, dict, str, dict]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    req_headers = dict(DEFAULT_HEADERS)
    req_headers["Referer"] = referer or f"{origin}/"

    if HAS_CURL_CFFI:
        try:
            async with CurlAsyncSession(impersonate="chrome") as s:
                if cookies:
                    s.cookies.update(cookies)
                if method.upper() == "POST":
                    resp = await s.post(
                        url,
                        data=data,
                        headers=req_headers,
                        allow_redirects=True,
                        timeout=timeout,
                    )
                else:
                    resp = await s.get(
                        url,
                        headers=req_headers,
                        allow_redirects=True,
                        timeout=timeout,
                    )
                hdrs = dict(resp.headers)
                ctype = hdrs.get("Content-Type", hdrs.get("content-type", "")).lower()
                body = (
                    ""
                    if "text/html" not in ctype and "json" not in ctype and "javascript" not in ctype
                    else resp.text
                )
                merged_cookies = dict(cookies or {})
                merged_cookies.update(dict(resp.cookies))
                return resp.status_code, str(resp.url), hdrs, body, merged_cookies
        except Exception:
            pass

    if HAS_CLOUDSCRAPER:
        def _sync_scrape():
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
            if cookies:
                scraper.cookies.update(cookies)
            if method.upper() == "POST":
                r = scraper.post(
                    url,
                    data=data,
                    headers=req_headers,
                    allow_redirects=True,
                    timeout=timeout,
                )
            else:
                r = scraper.get(
                    url,
                    headers=req_headers,
                    allow_redirects=True,
                    timeout=timeout,
                )
            hdrs = dict(r.headers)
            ctype = hdrs.get("Content-Type", hdrs.get("content-type", "")).lower()
            body = "" if "text/html" not in ctype and "json" not in ctype else r.text
            merged_cookies = dict(cookies or {})
            merged_cookies.update(r.cookies.get_dict())
            return r.status_code, str(r.url), hdrs, body, merged_cookies

        try:
            return await asyncio.to_thread(_sync_scrape)
        except Exception:
            pass

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(headers=req_headers, timeout=client_timeout, cookies=cookies) as session:
        if method.upper() == "POST":
            async with session.post(url, data=data, allow_redirects=True) as resp:
                hdrs = dict(resp.headers)
                ctype = hdrs.get("Content-Type", "").lower()
                body = await resp.text(errors="ignore") if ("text/html" in ctype or "json" in ctype) else ""
                jar = {k: v.value for k, v in session.cookie_jar.filter_cookies(resp.url).items()}
                return resp.status, str(resp.url), hdrs, body, jar
        else:
            async with session.get(url, allow_redirects=True) as resp:
                hdrs = dict(resp.headers)
                ctype = hdrs.get("Content-Type", "").lower()
                body = await resp.text(errors="ignore") if ("text/html" in ctype or "json" in ctype) else ""
                jar = {k: v.value for k, v in session.cookie_jar.filter_cookies(resp.url).items()}
                return resp.status, str(resp.url), hdrs, body, jar


async def fetch_gdrive_title_fallback(session: aiohttp.ClientSession, file_id: str) -> str:
    try:
        view_url = f"https://drive.google.com/file/d/{file_id}/view"
        async with session.get(view_url, allow_redirects=True, timeout=12) as resp:
            page = await resp.text(errors="ignore")
            for pat in [
                r'<meta\s+property="og:title"\s+content="([^"]+)"',
                r"<title>(.*?)</title>",
            ]:
                m = re.search(pat, page, re.IGNORECASE | re.DOTALL)
                if m:
                    title = clean_extracted_filename(m.group(1))
                    if title and not title.lower().startswith("google drive"):
                        return title
    except Exception:
        pass
    return ""


async def resolve_gdrive_direct_url_and_meta(
    session: aiohttp.ClientSession, gdrive_url: str
) -> tuple[str, str, int, str, str]:
    file_id = extract_gdrive_file_id(gdrive_url)
    if not file_id:
        raise RuntimeError("Could not extract File ID from Google Drive link.")

    direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    resp = await session.get(direct_url, allow_redirects=True)
    content_type = resp.headers.get("Content-Type", "")

    if "text/html" in content_type:
        html_body = await resp.text(errors="ignore")
        await resp.release()

        if "Quota exceeded" in html_body or "Too many users have viewed" in html_body:
            raise RuntimeError("Google Drive download quota exceeded for this file.")

        confirm_match = re.search(r'name="confirm"\s+value="([^"]+)"', html_body)
        uuid_match = re.search(r'name="uuid"\s+value="([^"]+)"', html_body)
        at_match = re.search(r'name="at"\s+value="([^"]+)"', html_body)

        params = {
            "id": file_id,
            "export": "download",
            "confirm": confirm_match.group(1) if confirm_match else "t",
        }
        if uuid_match:
            params["uuid"] = uuid_match.group(1)
        if at_match:
            params["at"] = at_match.group(1)

        resp = await session.get(
            "https://drive.usercontent.google.com/download",
            params=params,
            allow_redirects=True,
        )
        if "text/html" in resp.headers.get("Content-Type", ""):
            await resp.release()
            raise RuntimeError(
                "Google Drive blocked access. Make sure the file is shared as 'Anyone with the link'."
            )

    if resp.status >= 400:
        await resp.release()
        raise RuntimeError(f"Google Drive HTTP Error {resp.status}")

    final_url = str(resp.url)
    orig_filename = extract_filename_from_url_or_headers(final_url, resp.headers)
    total_bytes = int(resp.headers.get("Content-Length", 0) or 0)

    cookies = session.cookie_jar.filter_cookies(resp.url)
    cookie_header = "; ".join(f"{k}={v.value}" for k, v in cookies.items())
    await resp.release()

    if not orig_filename:
        orig_filename = await fetch_gdrive_title_fallback(session, file_id)
    if not orig_filename:
        orig_filename = f"media_{int(time.time())}.mkv"

    return (
        final_url,
        clean_extracted_filename(orig_filename),
        total_bytes,
        cookie_header,
        "https://drive.google.com/",
    )


def _extract_title_from_html(html_body: str) -> str:
    for pat in [
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        r'<div[^>]*class=["\'][^"\']*(?:card-header|file-name|filename|title)[^"\']*["\'][^>]*>(.*?)</div>',
        r"<h[1-5][^>]*>(.*?)</h[1-5]>",
        r"<title>(.*?)</title>",
    ]:
        m = re.search(pat, html_body, re.IGNORECASE | re.DOTALL)
        if m:
            raw_t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if raw_t and not any(
                k in raw_t.lower()
                for k in [
                    "hubcloud",
                    "gdflix",
                    "just a moment",
                    "attention required",
                    "404",
                    "not found",
                    "generate direct",
                    "cloudflare",
                ]
            ):
                return clean_extracted_filename(raw_t)
    return ""


def _find_all_links(html_body: str, base_url: str) -> list[str]:
    found = []
    patterns = [
        r'(?:href|data-href|data-url|src)\s*=\s*["\']([^"\']+)["\']',
        r'(?:window\.open|window\.location(?:\.href)?|location\.replace)\s*(?:=|\()\s*["\']([^"\']+)["\']',
        r'(?:var|let|const)\s+(?:url|dl_url|download_url|link|file_url|direct_url)\s*=\s*["\']([^"\']+)["\']',
        r'["\'](https?://[^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html_body, re.IGNORECASE):
            val = html.unescape(m.group(1)).strip()
            if not val or val.startswith(("#", "javascript:", "mailto:", "data:")):
                continue
            abs_url = urljoin(base_url, val)
            if abs_url.startswith("http") and abs_url not in found:
                found.append(abs_url)
    return found


def _filter_cdn_candidates(all_urls: list[str]) -> list[str]:
    priority_keywords = (
        "workers.dev",
        "r2.dev",
        "googleusercontent.com",
        "googleapis.com",
        "hubcdn",
        "gpdl",
        "fsl",
        "fastdl",
        "zfile",
        "pixeldrain.com/api/file",
        "cloudfront.net",
        "cdn.",
        ".mkv",
        ".mp4",
        ".avi",
        ".zip",
    )
    skip_keywords = (
        "google-analytics",
        "googletagmanager",
        "doubleclick",
        "facebook.com",
        "twitter.com",
        "t.me/",
        "telegram.me",
        ".css",
        ".js",
        ".png",
        ".jpg",
        ".svg",
        ".ico",
        "fonts.googleapis",
        "cloudflare.com",
    )
    ranked = []
    for u in all_urls:
        low = u.lower()
        if any(s in low for s in skip_keywords):
            continue
        if any(k in low for k in priority_keywords) and u not in ranked:
            ranked.append(u)
    return ranked


async def resolve_hubcloud_and_gdflix(
    page_url: str,
) -> tuple[str, str, int, str, str]:
    status, current_url, hdrs, html_body, jar = await browser_fetch(page_url)
    if status >= 400:
        raise RuntimeError(f"Host HTTP Error {status}")

    ctype = hdrs.get("Content-Type", hdrs.get("content-type", "")).lower()
    if ctype and "text/html" not in ctype:
        fname = extract_filename_from_url_or_headers(current_url, hdrs)
        total_bytes = int(hdrs.get("Content-Length", hdrs.get("content-length", 0)) or 0)
        cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
        return current_url, clean_extracted_filename(fname), total_bytes, cookie_str, page_url

    page_title = _extract_title_from_html(html_body)

    for _ in range(4):
        all_links = _find_all_links(html_body, current_url)
        cdn_cands = _filter_cdn_candidates(all_links)
        if cdn_cands:
            break

        hop_url = ""
        for u in all_links:
            low = u.lower()
            if u != current_url and any(
                k in low
                for k in (
                    "gamerxyt",
                    "hubcloud.php",
                    "/drive/",
                    "/dl/",
                    "/fastdl/",
                    "/zfile/",
                    "/ddl/",
                    "token=",
                    "key=",
                )
            ):
                hop_url = u
                break

        if hop_url:
            st, next_curr, next_hdrs, next_body, jar = await browser_fetch(
                hop_url, referer=current_url, cookies=jar
            )
            if st < 400:
                current_url = next_curr
                n_ctype = next_hdrs.get("Content-Type", next_hdrs.get("content-type", "")).lower()
                if n_ctype and "text/html" not in n_ctype:
                    fname = extract_filename_from_url_or_headers(current_url, next_hdrs)
                    total_bytes = int(
                        next_hdrs.get("Content-Length", next_hdrs.get("content-length", 0)) or 0
                    )
                    cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
                    return (
                        current_url,
                        clean_extracted_filename(fname or page_title),
                        total_bytes,
                        cookie_str,
                        hop_url,
                    )
                html_body = next_body
                if not page_title:
                    page_title = _extract_title_from_html(html_body)
                continue

        form_match = re.search(
            r'<form[^>]*method=["\']post["\'][^>]*>(.*?)</form>',
            html_body,
            re.IGNORECASE | re.DOTALL,
        )
        if form_match:
            action_m = re.search(r'action=["\']([^"\']*)["\']', form_match.group(0), re.IGNORECASE)
            post_url = (
                urljoin(current_url, html.unescape(action_m.group(1)))
                if action_m and action_m.group(1)
                else current_url
            )
            form_data = {}
            for inp in re.finditer(r"<input[^>]+>", form_match.group(1), re.IGNORECASE):
                tag = inp.group(0)
                n_m = re.search(r'name=["\']([^"\']+)["\']', tag, re.IGNORECASE)
                v_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.IGNORECASE)
                if n_m:
                    form_data[n_m.group(1)] = html.unescape(v_m.group(1)) if v_m else ""
            if form_data:
                st, next_curr, next_hdrs, next_body, jar = await browser_fetch(
                    post_url, method="POST", data=form_data, referer=current_url, cookies=jar
                )
                if st < 400:
                    current_url = next_curr
                    html_body = next_body
                    if not page_title:
                        page_title = _extract_title_from_html(html_body)
                    continue
        break

    cdn_cands = _filter_cdn_candidates(_find_all_links(html_body, current_url))

    for cand_url in cdn_cands:
        st, final_cdn, f_hdrs, _, f_jar = await browser_fetch(
            cand_url, referer=current_url, cookies=jar, timeout=20
        )
        f_ctype = f_hdrs.get("Content-Type", f_hdrs.get("content-type", "")).lower()
        if st < 400 and "text/html" not in f_ctype:
            header_name = extract_filename_from_url_or_headers(final_cdn, f_hdrs)
            total_bytes = int(f_hdrs.get("Content-Length", f_hdrs.get("content-length", 0)) or 0)
            cookie_str = "; ".join(f"{k}={v}" for k, v in f_jar.items())
            final_name = clean_extracted_filename(header_name or page_title)
            return final_cdn, final_name, total_bytes, cookie_str, current_url

    title_dbg = re.search(r"<title>(.*?)</title>", html_body, re.IGNORECASE | re.DOTALL)
    dbg_str = re.sub(r"\s+", " ", title_dbg.group(1)).strip() if title_dbg else "No Title"
    raise RuntimeError(
        f"Could not extract direct download link from HubCloud/GDFlix page (Page: {dbg_str[:55]})."
    )


async def download_from_tgfiles_via_premium(
    tgfiles_url: str,
    dest_dir: str,
    progress_cb=None,
    user_client=None,
) -> tuple[str, str] | None:
    status, final_page_url, hdrs, html_body, jar = await browser_fetch(tgfiles_url)
    if status >= 400:
        raise RuntimeError(f"TGFiles HTTP Error {status}")

    tg_match = re.search(
        r'(https?://(?:t\.me|telegram\.me|telegram\.dog)/([a-zA-Z0-9_]+)\?start=([a-zA-Z0-9_-]+))',
        final_page_url + "\n" + html_body,
    )
    if not tg_match:
        return None

    bot_username = tg_match.group(2)
    start_payload = tg_match.group(3)

    premium_client = user_client or get_premium_session_client()
    if premium_client is None or not getattr(premium_client, "is_connected", False):
        raise RuntimeError(
            "TGFiles link points to a Telegram bot, but no active Telegram Premium string session client is connected."
        )

    sent = await premium_client.send_message(bot_username, f"/start {start_payload}")

    media_msg = None
    for _ in range(30):
        await asyncio.sleep(1.0)
        async for msg in premium_client.get_chat_history(bot_username, limit=5):
            if msg.id > sent.id and (msg.document or msg.video):
                media_msg = msg
                break
        if media_msg:
            break

    if media_msg is None:
        raise RuntimeError(f"Timed out waiting for @{bot_username} to send the file.")

    media_obj = media_msg.document or media_msg.video
    file_size = getattr(media_obj, "file_size", 0) or 0
    if file_size > TELEGRAM_PREMIUM_MAX_BYTES:
        raise RuntimeError(
            f"File size ({file_size / (1024**3):.2f} GB) exceeds the 4.00 GB Telegram Premium limit."
        )

    raw_tg_name = getattr(media_obj, "file_name", "") or f"tg_media_{int(time.time())}.mkv"
    clean_name = clean_extracted_filename(raw_tg_name)
    dest_path = os.path.join(dest_dir, f"raw_{int(time.time())}_{clean_name}")

    start_ts = time.time()
    last_update = [0.0]

    async def _pyro_progress(current: int, total: int):
        if not progress_cb:
            return
        now = time.time()
        if now - last_update[0] >= 4.0 or current >= total:
            last_update[0] = now
            spd = (current / (1024 * 1024)) / max(0.1, now - start_ts)
            try:
                await progress_cb(current, total or file_size, spd)
            except Exception:
                pass

    await premium_client.download_media(
        media_msg,
        file_name=dest_path,
        progress=_pyro_progress,
    )
    validate_downloaded_media(dest_path)
    return dest_path, clean_name


async def resolve_tgfiles_http(tgfiles_url: str) -> tuple[str, str, int, str, str]:
    status, current_url, hdrs, html_body, jar = await browser_fetch(tgfiles_url)
    if status >= 400:
        raise RuntimeError(f"TGFiles HTTP Error {status}")

    ctype = hdrs.get("Content-Type", hdrs.get("content-type", "")).lower()
    if ctype and "text/html" not in ctype:
        fname = extract_filename_from_url_or_headers(current_url, hdrs)
        total_bytes = int(hdrs.get("Content-Length", hdrs.get("content-length", 0)) or 0)
        cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
        return current_url, clean_extracted_filename(fname), total_bytes, cookie_str, tgfiles_url

    candidates = _filter_cdn_candidates(_find_all_links(html_body, current_url))
    for cand_url in candidates:
        st, final_url, f_hdrs, _, f_jar = await browser_fetch(
            cand_url, referer=current_url, cookies=jar, timeout=20
        )
        f_ctype = f_hdrs.get("Content-Type", f_hdrs.get("content-type", "")).lower()
        if st < 400 and "text/html" not in f_ctype:
            fname = extract_filename_from_url_or_headers(final_url, f_hdrs)
            total_bytes = int(f_hdrs.get("Content-Length", f_hdrs.get("content-length", 0)) or 0)
            cookie_str = "; ".join(f"{k}={v}" for k, v in f_jar.items())
            return final_url, clean_extracted_filename(fname), total_bytes, cookie_str, current_url

    raise RuntimeError("Could not resolve direct stream or bot link from TGFiles page.")


async def resolve_direct_http(url: str) -> tuple[str, str, int, str, str]:
    parsed = urlparse(url)
    origin_ref = f"{parsed.scheme}://{parsed.netloc}/"
    status, final_url, hdrs, _, jar = await browser_fetch(url, referer=origin_ref)
    if status >= 400:
        raise RuntimeError(f"HTTP Download Error {status}")

    orig_filename = (
        extract_filename_from_url_or_headers(final_url, hdrs)
        or f"download_{int(time.time())}.mkv"
    )
    total_bytes = int(hdrs.get("Content-Length", hdrs.get("content-length", 0)) or 0)
    cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
    return final_url, clean_extracted_filename(orig_filename), total_bytes, cookie_str, origin_ref


async def curl_cffi_stream_download(
    direct_url: str,
    dest_path: str,
    total_bytes: int,
    referer: str = "",
    cookie_header: str = "",
    progress_cb=None,
) -> bool:
    """Fallback downloader using Chrome TLS fingerprint when aria2c/aiohttp gets blocked by CDN WAF."""
    if not HAS_CURL_CFFI:
        return False

    req_headers = dict(DEFAULT_HEADERS)
    if referer:
        req_headers["Referer"] = referer
    if cookie_header:
        req_headers["Cookie"] = cookie_header

    start_ts = time.time()
    last_rep = [0.0]
    done = 0

    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            async with s.stream(
                "GET", direct_url, headers=req_headers, allow_redirects=True, timeout=7200
            ) as resp:
                if resp.status_code >= 400:
                    return False
                ctype = resp.headers.get("Content-Type", "").lower()
                if "text/html" in ctype:
                    return False

                expected = total_bytes or int(resp.headers.get("Content-Length", 0) or 0)
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_content(chunk_size=512 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if progress_cb and (now - last_rep[0] >= 4.0):
                            last_rep[0] = now
                            spd = (done / (1024 * 1024)) / max(0.1, now - start_ts)
                            try:
                                await progress_cb(done, expected or done, spd)
                            except Exception:
                                pass

        if progress_cb and done > 0:
            spd = (done / (1024 * 1024)) / max(0.1, time.time() - start_ts)
            await progress_cb(done, total_bytes or done, spd)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except Exception:
        return False


async def aria2_fast_download(
    direct_url: str,
    dest_path: str,
    total_bytes: int,
    cookie_header: str = "",
    referer: str = "",
    progress_cb=None,
) -> bool:
    if not shutil.which("aria2c"):
        return False

    out_dir = os.path.dirname(dest_path)
    out_name = os.path.basename(dest_path)

    cmd = [
        "aria2c",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--disk-cache=16M",
        "--file-allocation=none",
        "--max-tries=5",
        "--retry-wait=1",
        "--connect-timeout=15",
        "--summary-interval=0",
        "--console-log-level=error",
        f"--user-agent={DEFAULT_HEADERS['User-Agent']}",
        "-d",
        out_dir,
        "-o",
        out_name,
    ]
    if referer:
        cmd.extend(["--referer", referer])
    if cookie_header:
        cmd.extend(["--header", f"Cookie: {cookie_header}"])
    cmd.append(direct_url)

    start_ts = time.time()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    while proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            if progress_cb and os.path.exists(dest_path):
                cur_bytes = os.path.getsize(dest_path)
                elapsed = max(0.1, time.time() - start_ts)
                speed_mbps = (cur_bytes / (1024 * 1024)) / elapsed
                try:
                    await progress_cb(cur_bytes, total_bytes, speed_mbps)
                except Exception:
                    pass

    if (
        proc.returncode != 0
        or not os.path.exists(dest_path)
        or os.path.getsize(dest_path) == 0
    ):
        return False

    # Check if aria2c downloaded an HTML block page instead of actual video
    try:
        validate_downloaded_media(dest_path)
    except Exception:
        return False

    if progress_cb:
        final_sz = os.path.getsize(dest_path)
        elapsed = max(0.1, time.time() - start_ts)
        speed_mbps = (final_sz / (1024 * 1024)) / elapsed
        try:
            await progress_cb(final_sz, total_bytes or final_sz, speed_mbps)
        except Exception:
            pass
    return True


async def parallel_range_http_download(
    session: aiohttp.ClientSession,
    direct_url: str,
    dest_path: str,
    total_bytes: int,
    referer: str = "",
    cookie_header: str = "",
    progress_cb=None,
    num_parts: int = 16,
):
    start_ts = time.time()
    chunk_size = 512 * 1024
    base_hdrs = {}
    if referer:
        base_hdrs["Referer"] = referer
    if cookie_header:
        base_hdrs["Cookie"] = cookie_header

    if total_bytes < 5 * 1024 * 1024:
        async with session.get(direct_url, headers=base_hdrs, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP Error {resp.status}")
            done = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
            if progress_cb:
                spd = (done / (1024 * 1024)) / max(0.1, time.time() - start_ts)
                await progress_cb(done, done, spd)
            return

    supports_range = False
    try:
        test_hdrs = dict(base_hdrs)
        test_hdrs["Range"] = "bytes=0-1"
        async with session.get(
            direct_url, headers=test_hdrs, allow_redirects=True
        ) as test_resp:
            if test_resp.status == 206:
                supports_range = True
    except Exception:
        supports_range = False

    if not supports_range:
        async with session.get(direct_url, headers=base_hdrs, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP Error {resp.status}")
            done = [0]
            dl_done_ev = asyncio.Event()

            async def single_reporter():
                while not dl_done_ev.is_set():
                    await asyncio.sleep(4.0)
                    if progress_cb:
                        spd = (done[0] / (1024 * 1024)) / max(0.1, time.time() - start_ts)
                        try:
                            await progress_cb(done[0], total_bytes or done[0], spd)
                        except Exception:
                            pass

            rep_t = asyncio.create_task(single_reporter())
            try:
                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if chunk:
                            f.write(chunk)
                            done[0] += len(chunk)
            finally:
                dl_done_ev.set()
                rep_t.cancel()
            if progress_cb:
                spd = (done[0] / (1024 * 1024)) / max(0.1, time.time() - start_ts)
                await progress_cb(done[0], total_bytes or done[0], spd)
            return

    with open(dest_path, "wb") as f:
        f.truncate(total_bytes)

    part_size = total_bytes // num_parts
    done_bytes = [0]
    dl_done_ev = asyncio.Event()

    async def range_reporter():
        if not progress_cb:
            return
        while not dl_done_ev.is_set():
            await asyncio.sleep(4.0)
            spd = (done_bytes[0] / (1024 * 1024)) / max(0.1, time.time() - start_ts)
            try:
                await progress_cb(min(done_bytes[0], total_bytes), total_bytes, spd)
            except Exception:
                pass

    rep_task = asyncio.create_task(range_reporter())
    fd = os.open(dest_path, os.O_WRONLY)

    async def fetch_part(start: int, end: int):
        range_headers = dict(base_hdrs)
        range_headers["Range"] = f"bytes={start}-{end}"
        async with session.get(
            direct_url, headers=range_headers, allow_redirects=True
        ) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"Range HTTP Error {resp.status}")
            offset = start
            async for chunk in resp.content.iter_chunked(chunk_size):
                if not chunk:
                    continue
                os.pwrite(fd, chunk, offset)
                offset += len(chunk)
                done_bytes[0] += len(chunk)

    try:
        tasks = []
        for i in range(num_parts):
            s = i * part_size
            e = (total_bytes - 1) if i == num_parts - 1 else ((i + 1) * part_size - 1)
            tasks.append(asyncio.create_task(fetch_part(s, e)))
        await asyncio.gather(*tasks)
    finally:
        os.close(fd)
        dl_done_ev.set()
        rep_task.cancel()
        release_memory()

    if progress_cb:
        spd = (total_bytes / (1024 * 1024)) / max(0.1, time.time() - start_ts)
        await progress_cb(total_bytes, total_bytes, spd)


async def download_from_url_with_progress(
    url: str,
    dest_dir: str,
    progress_cb=None,
    user_client=None,
) -> tuple[str, str]:
    os.makedirs(dest_dir, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=7200, connect=20)
    connector = aiohttp.TCPConnector(limit=32, force_close=False)
    domain = urlparse(url).netloc.lower()

    async with aiohttp.ClientSession(
        headers=DEFAULT_HEADERS, timeout=timeout, connector=connector
    ) as session:
        if any(
            d in domain
            for d in ["drive.google.com", "docs.google.com", "drive.usercontent.google.com"]
        ):
            direct_url, orig_filename, total_bytes, cookie_header, referer = (
                await resolve_gdrive_direct_url_and_meta(session, url)
            )

        elif "hubcloud" in domain or "gdflix" in domain:
            direct_url, orig_filename, total_bytes, cookie_header, referer = (
                await resolve_hubcloud_and_gdflix(url)
            )

        elif "tgfiles" in domain:
            tg_result = await download_from_tgfiles_via_premium(
                tgfiles_url=url,
                dest_dir=dest_dir,
                progress_cb=progress_cb,
                user_client=user_client,
            )
            if tg_result is not None:
                return tg_result

            direct_url, orig_filename, total_bytes, cookie_header, referer = (
                await resolve_tgfiles_http(url)
            )

        else:
            direct_url, orig_filename, total_bytes, cookie_header, referer = (
                await resolve_direct_http(url)
            )

        if total_bytes > TELEGRAM_PREMIUM_MAX_BYTES:
            raise RuntimeError(
                f"File size ({total_bytes / (1024**3):.2f} GB) exceeds the 4.00 GB Telegram Premium limit."
            )

        clean_filename = clean_extracted_filename(orig_filename)
        dest_path = os.path.join(dest_dir, f"raw_{int(time.time())}_{clean_filename}")

        # 1. Try 16-connection aria2c first (validates that result isn't HTML)
        ok = await aria2_fast_download(
            direct_url=direct_url,
            dest_path=dest_path,
            total_bytes=total_bytes,
            cookie_header=cookie_header,
            referer=referer,
            progress_cb=progress_cb,
        )

        # 2. If aria2c failed or got an HTML WAF block page, use Chrome-TLS curl_cffi stream
        if not ok:
            ok = await curl_cffi_stream_download(
                direct_url=direct_url,
                dest_path=dest_path,
                total_bytes=total_bytes,
                referer=referer,
                cookie_header=cookie_header,
                progress_cb=progress_cb,
            )

        # 3. Final fallback: 16-part async aiohttp range downloader
        if not ok:
            await parallel_range_http_download(
                session=session,
                direct_url=direct_url,
                dest_path=dest_path,
                total_bytes=total_bytes,
                referer=referer,
                cookie_header=cookie_header,
                progress_cb=progress_cb,
                num_parts=16,
            )

        # Verify file is a genuine binary media file before FFmpeg touches it
        validate_downloaded_media(dest_path)

        if os.path.getsize(dest_path) > TELEGRAM_PREMIUM_MAX_BYTES:
            os.remove(dest_path)
            raise RuntimeError("Downloaded file exceeds the 4.00 GB Telegram Premium limit.")

        return dest_path, clean_filename
