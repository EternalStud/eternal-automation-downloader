import os
import re
import time
import html
import json
import shutil
import socket
import threading
import ipaddress
import urllib.parse
import requests
import yt_dlp

import http.cookiejar
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from config import (
    DOWNLOADS_DIR, COOKIES_FILE, MAX_TELEGRAM_SIZE_BYTES,
    TRUSTED_DOMAINS, URL_REGEX
)

def get_auth_session():
    """Returns a requests.Session() automatically authenticated with cookies.txt if available."""
    session = requests.Session()
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        try:
            cj = http.cookiejar.MozillaCookieJar(COOKIES_FILE)
            cj.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(cj)
        except Exception as e:
            print(f"⚠️ Error loading cookies for scraper: {e}")
    return session

# -----------------------
# TITLE & CAPTION FORMATTING
# -----------------------

def is_ugly_stats_string(s):
    """Detects if a string is pure view/like/reaction noise (e.g. '179K views · 213K reactions')."""
    if not s or not str(s).strip():
        return False
    s_clean = str(s).strip()
    return bool(re.match(
        r'^\s*\d+[\d.,]*\s*[KkMmBb]?\s*(?:views?|reactions?|likes?|plays?|reels?|shares?|comments?)'
        r'(?:\s*[·•\|\-,\s/]+\s*\d+[\d.,]*\s*[KkMmBb]?\s*(?:views?|reactions?|likes?|plays?|reels?|shares?|comments?))*\s*$',
        s_clean, re.IGNORECASE
    ))

def clean_media_title(title, default="Audio Track"):
    """Creates a clean, human-readable title by removing video noise, ugly view/reaction counts, and numeric IDs."""
    if not title or not str(title).strip() or str(title).strip().lower() in ("media", "none", "video", "null", "image", "audio", "track"):
        return default
    cleaned = str(title).strip()
    if cleaned.isdigit() and len(cleaned) > 6:
        return default

    # If title has stats prefix separated by delimiter (e.g. "179K views · 213K reactions | Actual Caption"):
    cleaned = re.sub(
        r'^\s*\d+[\d.,]*\s*[KkMmBb]?\s*(?:views?|reactions?|likes?|plays?|reels?|shares?|comments?)'
        r'(?:\s*[·•\|\-,\s/]+\s*\d+[\d.,]*\s*[KkMmBb]?\s*(?:views?|reactions?|likes?|plays?|reels?|shares?|comments?))*\s*[·•\|\-:]\s*',
        '', cleaned, flags=re.IGNORECASE
    ).strip()

    # If title has stats suffix separated by delimiter:
    cleaned = re.sub(
        r'\s*[·•\|\-:]\s*\d+[\d.,]*\s*[KkMmBb]?\s*(?:views?|reactions?|likes?|plays?|reels?|shares?|comments?)'
        r'(?:\s*[·•\|\-,\s/]+\s*\d+[\d.,]*\s*[KkMmBb]?\s*(?:views?|reactions?|likes?|plays?|reels?|shares?|comments?))*\s*$',
        '', cleaned, flags=re.IGNORECASE
    ).strip()

    if is_ugly_stats_string(cleaned):
        return default

    cleaned = re.sub(r"\s*\|\s*(Official.*|Music Video|Audio|Full Video|Lyrics|HD|4K|1080p|Full Song).*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[\(\[]\s*.*?(Official|Music Video|Audio|Lyrics|Visualizer|HD|4K|Full Song).*?[\)\]]", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned if len(cleaned) > 2 else default

def format_clean_audio_filename(clean_name, ext=".mp3"):
    """Produces a clean, filesystem-safe filename with proper audio extension."""
    s = clean_name.replace("|", " - ").replace("/", " - ").replace("\\", " - ")
    s = re.sub(r"[\*?:\"<>#%&\{\}\$!@+=`~]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    if not s or s.isdigit() or is_ugly_stats_string(s):
        s = "Audio_Track"
    return f"{s[:60]}{ext}"

def format_clean_video_filename(title, ext=".mp4"):
    """Produces a clean, human-readable filename for videos without ugly hashes or view stats."""
    if not title or not str(title).strip():
        return f"Video{ext}"
    s = clean_media_title(title, default="Video")
    s = re.sub(r'[\*?:\"<>|/\\#%&\{\}\$!@+=`~]', '', s)
    s = re.sub(r'\s*\[[a-zA-Z0-9_-]+\]$', '', s).strip()
    s = re.sub(r'\s+', ' ', s).strip(" -_.")
    if not s or s.isdigit() or s.lower() in ("media", "video", "none", "null") or is_ugly_stats_string(s):
        s = "Video"
    return f"{s[:64]}{ext}"


def generate_video_thumbnail(video_path, thumb_path=None):
    """Extracts a crisp, vibrant preview frame at 1.0s to eliminate black video boxes in Telegram."""
    if not video_path or not os.path.exists(video_path):
        return None
    if not thumb_path:
        thumb_path = os.path.splitext(video_path)[0] + "_thumb.jpg"
    try:
        import subprocess
        cmd = [
            "ffmpeg", "-y", "-ss", "00:00:01", "-i", str(video_path),
            "-vframes", "1", "-q:v", "2", str(thumb_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if res.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None

def get_video_metadata(file_path):
    """Extracts width, height, and duration using ffprobe for seamless Telegram in-app streaming."""
    if not file_path or not os.path.exists(file_path):
        return {}
    try:
        import subprocess
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "json", str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, text=True)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            if streams:
                w = int(streams[0].get("width") or 0)
                h = int(streams[0].get("height") or 0)
                d = float(streams[0].get("duration") or 0)
                return {"width": w, "height": h, "duration": int(d)}
    except Exception:
        pass
    return {}


class DownloadCancelledException(Exception):
    """Raised when an active download job is cancelled by the user."""
    pass

_CANCELLED_JOBS = set()
_CANCELLED_LOCK = threading.Lock()

def mark_job_cancelled(job_id):
    with _CANCELLED_LOCK:
        _CANCELLED_JOBS.add(str(job_id))

def is_job_cancelled(job_id):
    with _CANCELLED_LOCK:
        return str(job_id) in _CANCELLED_JOBS

def clear_job_cancelled(job_id):
    with _CANCELLED_LOCK:
        _CANCELLED_JOBS.discard(str(job_id))


def format_caption(title, file_path=None, max_len=140, is_audio_fallback=False, is_explicit_audio=False):
    text = ""
    generic_titles = (
        "media", "none", "video", "null", "image", "facebook post",
        "facebook", "photo", "photos", "post", "instagram post", "x post", "tweet"
    )
    if title and str(title).strip():
        cleaned = str(title).strip()
        if cleaned.isdigit() or re.match(r'^(?:verified account\s*)?\[?[0-9]+\]?$', cleaned, re.IGNORECASE):
            cleaned = ""
        elif cleaned.lower() not in generic_titles:
            # Handle "179K views · 213K reactions | Real Caption" or "Real Caption | 179K views"
            if " | " in cleaned:
                parts = cleaned.split(" | ")
                valid_parts = [p.strip() for p in parts if not is_ugly_stats_string(p.strip()) and p.strip().lower() not in generic_titles]
                cleaned = " - ".join(valid_parts) if valid_parts else ""
            elif is_ugly_stats_string(cleaned):
                cleaned = ""
            text = cleaned

    if not text and file_path:
        base = os.path.splitext(os.path.basename(file_path))[0]
        base = re.sub(r'\s*\[[0-9]+\]$', '', base).strip()
        if not re.match(r'^(?:fb|ig|pin|tw|yt|job)_[0-9]+', base, re.IGNORECASE) and not base.isdigit():
            if not re.match(r'^(?:verified account\s*)?\[?[0-9]+\]?$', base, re.IGNORECASE) and base.lower() not in generic_titles and not is_ugly_stats_string(base):
                text = base.replace("_", " ").replace("-", " ").strip()

    if not text or text.lower() in generic_titles or text.isdigit() or is_ugly_stats_string(text):
        text = ""


    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."

    safe_text = html.escape(text)
    if is_explicit_audio:
        return f"🎵 <b>{safe_text}</b>" if safe_text else None

    if is_audio_fallback:
        return f"🎵 <b>{safe_text}</b>\n\n⚠️ <i>Note: Audio format delivered because video exceeded 49MB limit.</i>" if safe_text else "⚠️ <i>Note: Audio format delivered because video exceeded 49MB limit.</i>"

    if not safe_text:
        return None

    ext = os.path.splitext(file_path)[1].lower() if file_path else ""
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return f"📸 <b>{safe_text}</b>"
    return f"🎬 <b>{safe_text}</b>"

def detect_platform(url):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "instagram.com" in u:
        return "Instagram"
    if "tiktok.com" in u:
        return "TikTok"
    if "threads.net" in u or "threads.com" in u:
        return "Threads"
    if "twitter.com" in u or "x.com" in u:
        return "X (Twitter)"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "pinterest.com" in u or "pin.it" in u or "pinimg.com" in u:
        return "Pinterest"
    if "snapchat.com" in u:
        return "Snapchat"
    if "reddit.com" in u or "redd.it" in u:
        return "Reddit"
    if "soundcloud.com" in u:
        return "SoundCloud"
    return "Media"

# -----------------------
# URL SAFETY & EXTRACTION (FAIL-CLOSED)
# -----------------------

def is_safe_url(url_str):
    """
    Validates URL safety against SSRF and private networks.
    Fails closed: Any unresolvable or private IP returns False.
    """
    try:
        parsed = urllib.parse.urlparse(url_str)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False

        host_lower = hostname.lower()
        # Fast-path for trusted platforms
        if host_lower in TRUSTED_DOMAINS or any(host_lower.endswith("." + d) for d in TRUSTED_DOMAINS):
            return True

        if host_lower in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False

        # Thread-safe DNS resolution without mutating global socket defaults
        addr_info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)

        if not addr_info:
            return False

        for item in addr_info:
            ip_str = item[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (ip_obj.is_private or ip_obj.is_loopback or
                ip_obj.is_link_local or ip_obj.is_reserved or
                ip_obj.is_multicast or ip_obj.is_unspecified):
                return False
        return True
    except Exception:
        # Strict Fail Closed on DNS failures, timeouts, or resolution errors
        return False


def sanitize_url(u):
    """Strips autoplay mix queues (list=RD...), tracking, and ephemeral query parameters."""
    try:
        parsed = urllib.parse.urlparse(u)
        host = parsed.netloc.lower()
        if "youtube.com" in host or "youtu.be" in host:
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            clean_qs = {}
            for k, v in qs.items():
                if k.lower() == "list":
                    list_val = v[0] if v else ""
                    # Strip radio mix (RD...), Liked (LL), Watch Later (WL)
                    if list_val.startswith("RD") or list_val.startswith("LL") or list_val.startswith("WL"):
                        continue
                    if parsed.path.rstrip("/") in ("/playlist", "/playlist/"):
                        clean_qs[k] = v
                    elif list_val.startswith("PL") or list_val.startswith("OL") or list_val.startswith("UU"):
                        clean_qs[k] = v
                elif k.lower() in ("si", "feature", "pp", "index", "start_radio"):
                    continue
                else:
                    clean_qs[k] = v
            new_query = urllib.parse.urlencode(clean_qs, doseq=True)
            return urllib.parse.urlunparse(parsed._replace(query=new_query))
        elif any(d in host for d in ("facebook.com", "instagram.com", "twitter.com", "x.com")):
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            clean_qs = {k: v for k, v in qs.items() if not (k.startswith("utm_") or k in ("fbclid", "igsh", "s", "t", "ref"))}
            new_query = urllib.parse.urlencode(clean_qs, doseq=True)
            return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        pass
    return u

def extract_urls(text):
    """Extract and clean all valid URLs from any message (including shared text and intent:// links)."""
    if not text:
        return []
    raw_urls = URL_REGEX.findall(text)
    cleaned = []
    for u in raw_urls:
        u = u.rstrip(".,;!?:'\"()[]{}<>")
        if u.startswith("intent://"):
            u = "https://" + u[9:]
        elif u.startswith("www."):
            u = "https://" + u
        elif not u.startswith(("http://", "https://")):
            u = "https://" + u

        u = sanitize_url(u)
        if u not in cleaned:
            cleaned.append(u)
    return cleaned


def extract_threads_media(url, dest_dir=None):
    """
    Direct scraper for Meta Threads (threads.net and threads.com) posts.
    Fetches full post text, high-res photos, and/or MP4 videos via SSR OpenGraph tags.
    """
    target_dir = dest_dir or DOWNLOADS_DIR
    try:
        # Googlebot UA triggers Threads SSR to include direct MP4 video streams in JSON hydration payload
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = get_auth_session().get(url, headers=headers, allow_redirects=True, timeout=15)
        if resp.status_code != 200 or len(resp.text) < 500:
            # Fallback to facebookexternalhit
            headers["User-Agent"] = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
            resp = get_auth_session().get(url, headers=headers, allow_redirects=True, timeout=15)
            if resp.status_code != 200 or len(resp.text) < 500:
                return None
        text = resp.text

        caption = ""
        desc_match = re.search(r"<meta[^>]+(?:property|name)=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)[\"']", text, re.I)
        if not desc_match:
            desc_match = re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:description[\"']", text, re.I)
        if desc_match:
            caption = html.unescape(desc_match.group(1)).strip()

        video_urls = []
        # 1. Parse video_versions from JSON script tags
        scripts = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', text)
        for s in scripts:
            for m in re.finditer(r'"video_versions"\s*:\s*(\[[^\]]+\])', s):
                try:
                    v_arr = json.loads(m.group(1))
                    for v in v_arr:
                        vu = v.get("url")
                        if vu and vu not in video_urls and not vu.endswith("/i8Ek3A1myhR.mp4") and "rsrc.php" not in vu:
                            video_urls.append(vu)
                except Exception:
                    pass

        # 2. OpenGraph og:video tags
        for vm in re.finditer(r"<meta[^>]+(?:property|name)=[\"']og:video(?::secure_url)?[\"'][^>]+content=[\"']([^\"']+)[\"']", text, re.I):
            vu = html.unescape(vm.group(1))
            if vu and vu not in video_urls and "rsrc.php" not in vu:
                video_urls.append(vu)

        # 3. Regex fallback for direct MP4 URLs
        if not video_urls:
            for mp4 in re.findall(r'https:[^\"\'\s<>]+?\.mp4[^\"\'\s<>]*', text):
                clean_mp4 = mp4.replace("\\u0026", "&").replace("\\/", "/")
                if "rsrc.php" not in clean_mp4 and "static.cdninstagram.com" not in clean_mp4 and clean_mp4 not in video_urls:
                    video_urls.append(clean_mp4)

        image_urls = []
        for im in re.finditer(r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)[\"']", text, re.I):
            iu = html.unescape(im.group(1))
            if iu and iu not in image_urls and not iu.endswith(("/logo.png", "/favicon.ico")):
                image_urls.append(iu)

        if not video_urls and not image_urls and not caption:
            return None

        os.makedirs(target_dir, exist_ok=True)
        results = []
        if caption:
            for chunk in split_telegram_text(caption):
                results.append(("text_only", html.escape(chunk, quote=False), False))

        short_cap = caption.split("\n")[0][:120] if caption else "Threads Post"

        dl_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }

        # Deliver video(s) when available; only fallback to static preview images if no video exists
        if video_urls:
            for idx, v_url in enumerate(video_urls[:4]):
                out_file = os.path.join(target_dir, f"threads_{int(time.time()*1000)}_v{idx}.mp4")
                if _download_binary(v_url, out_file, min_size=5000, headers=dl_headers):
                    results.append((out_file, short_cap, False))
        else:
            for idx, img_url in enumerate(image_urls[:10]):
                out_file = os.path.join(target_dir, f"threads_{int(time.time()*1000)}_{idx}.jpg")
                if _download_binary(img_url, out_file, min_size=1000, headers=dl_headers):
                    results.append((out_file, short_cap, False))

        return results if results else None
    except Exception as e:
        print(f"⚠️ Threads media extraction failed: {e}")
        return None

def extract_pinterest_media(url, dest_dir=None):
    """Direct HD video & original image scraper for Pinterest pins."""
    target_dir = dest_dir or DOWNLOADS_DIR
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = get_auth_session().get(url, headers=headers, allow_redirects=True, timeout=12)
        html_text = resp.text

        title_match = re.search(r'<title>(.*?)</title>', html_text, re.IGNORECASE)
        title = title_match.group(1).replace(" | Pinterest", "").replace(" - Pinterest", "").strip() if title_match else "Pinterest Media"

        # 1. Search for direct pre-muxed MP4 video URLs
        vid_matches = re.findall(r'https://v1\.pinimg\.com/videos/[^\s"\'<>]+\.mp4', html_text) + re.findall(r'https://[^\s"\'<>]+\.pinimg\.com/[^\s"\'<>]+\.mp4', html_text)
        if vid_matches:
            sorted_vids = sorted(list(set(vid_matches)), key=lambda v: (1 if '720w' in v else (2 if '1080w' in v else 0)), reverse=True)
            best_vid = sorted_vids[0]
            vid_resp = get_auth_session().get(best_vid, stream=True, headers=headers, timeout=25)
            if vid_resp.status_code == 200:
                os.makedirs(target_dir, exist_ok=True)
                out_path = os.path.join(target_dir, f"pin_{int(time.time()*1000)}.mp4")
                with open(out_path, "wb") as f:
                    for chunk in vid_resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                return [(out_path, title, False)]

        # 2. Search for original HD image
        img_matches = re.findall(r'https://i\.pinimg\.com/(?:originals|736x|564x)/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)', html_text)
        if img_matches:
            best_img_url = img_matches[0]
            hd_url = re.sub(r'/(?:736x|564x)/', '/originals/', best_img_url)
            img_resp = get_auth_session().get(hd_url, headers=headers, timeout=15)
            if img_resp.status_code != 200:
                img_resp = get_auth_session().get(best_img_url, headers=headers, timeout=15)

            if img_resp.status_code == 200 and len(img_resp.content) > 0:
                os.makedirs(target_dir, exist_ok=True)
                ext = ".jpg" if "jpeg" in img_resp.headers.get("content-type", "") or ".jpg" in hd_url else ".png"
                out_path = os.path.join(target_dir, f"pin_{int(time.time()*1000)}{ext}")
                with open(out_path, "wb") as f:
                    f.write(img_resp.content)
                return [(out_path, title, False)]

    except Exception as e:
        print(f"⚠️ Pinterest scraper error: {e}")
    return None

def extract_instagram_media(url, dest_dir=None):
    """
    Direct HD scraper for Instagram photo posts, video posts, and mixed carousels.
    Extracts all media items (photos and videos) and delivers full post caption text,
    strictly excluding advertising banners, profile icons, and placeholder media.
    """
    target_dir = dest_dir or DOWNLOADS_DIR
    files = []
    downloaded_media = []
    caption = None

    # Stage 1: Use yt-dlp info extractor to inspect all carousel items (photos & videos)
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
        }
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            ydl_opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ie = ydl.get_info_extractor("Instagram")
            res = ie.extract(url)
            if res:
                entries = res.get("entries") if res.get("_type") == "playlist" else [res]
                caption = res.get("description") or res.get("title") or ""
                if not caption:
                    for entry in entries:
                        if entry and (entry.get("description") or entry.get("title")):
                            caption = entry.get("description") or entry.get("title")
                            break
                if caption:
                    caption = html.unescape(str(caption)).strip()

                short_title = caption.split("\n")[0][:120] if caption else "Instagram Post"
                os.makedirs(target_dir, exist_ok=True)
                session = get_auth_session()

                bad_ig_markers = (
                    "s150x150", "s320x320", "profile_pic", "/static/", "favicon",
                    "app_store", "play_store", "rsrc.php", "emoji.php", "sponsored", "ad_creative"
                )

                for i, entry in enumerate(entries):
                    if not entry:
                        continue

                    # Check if entry is a video
                    is_video = bool(
                        entry.get("vcodec") and entry.get("vcodec") != "none"
                        or entry.get("ext") in ("mp4", "mov", "mkv", "webm")
                        or entry.get("_type") == "video"
                        or any(f.get("vcodec") and f.get("vcodec") != "none" for f in entry.get("formats", []))
                        or (entry.get("url") and ".mp4" in str(entry.get("url")).lower())
                    )

                    if is_video:
                        video_url = None
                        formats = entry.get("formats", [])
                        video_formats = [
                            f for f in formats
                            if f.get("vcodec") != "none" and f.get("url")
                        ]
                        if video_formats:
                            video_formats.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
                            video_url = video_formats[0].get("url")
                        if not video_url:
                            video_url = entry.get("url")

                        if video_url and str(video_url).startswith("http"):
                            out_path = os.path.join(target_dir, f"ig_{int(time.time()*1000)}_{i}.mp4")
                            if _download_binary(video_url, out_path, min_size=5000):
                                downloaded_media.append((out_path, short_title, False))
                                continue

                    # If not video or video download failed, process as photo
                    thumbs = entry.get("thumbnails", [])
                    valid_thumbs = [
                        t for t in thumbs
                        if t.get("url") and not any(m in t["url"].lower() for m in bad_ig_markers)
                    ]
                    best_img_url = valid_thumbs[-1]["url"] if valid_thumbs else (entry.get("url") if entry.get("url") and not is_video else None)

                    if best_img_url and str(best_img_url).startswith("http"):
                        try:
                            r = session.get(best_img_url, timeout=20)
                            if r.status_code == 200 and len(r.content) >= 5000:
                                if r.content.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF")):
                                    ext = ".jpg" if r.content.startswith(b"\xff\xd8\xff") else (".png" if r.content.startswith(b"\x89PNG") else ".webp")
                                    out_path = os.path.join(target_dir, f"ig_{int(time.time()*1000)}_{i}{ext}")
                                    with open(out_path, "wb") as f:
                                        f.write(r.content)
                                    downloaded_media.append((out_path, short_title, False))
                        except Exception as e:
                            print(f"⚠️ Error downloading Instagram image {i}: {e}")

                if downloaded_media:
                    if caption:
                        for chunk in split_telegram_text(caption):
                            files.append(("text_only", html.escape(chunk, quote=False), False))
                    files.extend(downloaded_media)
                    return files
    except Exception as e:
        print(f"⚠️ Instagram info extractor error: {e}")

    # Stage 2: Fallback to direct web scraper
    try:
        clean_url = url.split("?")[0].rstrip("/")
        headers = {
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = get_auth_session().get(clean_url + "/", headers=headers, timeout=15)
        text = r.text

        desc_match = re.findall(r"property=[\"\']og:description[\"\'] content=[\"\']([^\"\']+)[\"\']", text)
        tw_desc = re.findall(r"name=[\"\']twitter:description[\"\'] content=[\"\']([^\"\']+)[\"\']", text)
        title_match = re.findall(r"property=[\"\']og:title[\"\'] content=[\"\']([^\"\']+)[\"\']", text)

        caption = (desc_match[0] if desc_match else (tw_desc[0] if tw_desc else None))
        if caption:
            caption = html.unescape(caption).strip()
        short_title = caption.split("\n")[0][:120] if caption else (html.unescape(title_match[0]) if title_match else "Instagram Post")

        # Extract videos
        og_vids = re.findall(r"property=[\"\']og:video(?::secure_url)?[\"\'] content=[\"\']([^\"\']+)[\"\']", text)
        json_vids = re.findall(r'"video_url"\s*:\s*"([^"]+)"', text)
        video_candidates = []
        for v in og_vids + json_vids:
            v_clean = html.unescape(v).replace("\\/", "/").strip()
            if v_clean.startswith("http") and v_clean not in video_candidates:
                video_candidates.append(v_clean)

        # Extract photos
        og_imgs = re.findall(r"property=[\"\']og:image[\"\'] content=[\"\']([^\"\']+)[\"\']", text)
        tw_imgs = re.findall(r"name=[\"\']twitter:image[\"\'] content=[\"\']([^\"\']+)[\"\']", text)
        json_imgs = re.findall(r'"display_url"\s*:\s*"([^"]+)"', text)

        bad_ig_markers = (
            "s150x150", "s320x320", "profile_pic", "/static/", "favicon",
            "app_store", "play_store", "rsrc.php", "emoji.php", "sponsored", "ad_creative"
        )

        photo_candidates = []
        for img in og_imgs + tw_imgs + json_imgs:
            u = html.unescape(img).replace("\\/", "/").strip()
            if u and u.startswith("http") and u not in photo_candidates and not any(m in u.lower() for m in bad_ig_markers):
                photo_candidates.append(u)

        os.makedirs(target_dir, exist_ok=True)
        session = get_auth_session()

        for i, v_url in enumerate(video_candidates):
            out_path = os.path.join(target_dir, f"ig_{int(time.time()*1000)}_v{i}.mp4")
            if _download_binary(v_url, out_path, min_size=5000, headers=headers):
                downloaded_media.append((out_path, short_title, False))

        for i, img_url in enumerate(photo_candidates):
            try:
                img_resp = session.get(img_url, headers=headers, timeout=15)
                if img_resp.status_code == 200 and len(img_resp.content) >= 5000:
                    if img_resp.content.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF")):
                        ext = ".jpg" if img_resp.content.startswith(b"\xff\xd8\xff") else (".png" if img_resp.content.startswith(b"\x89PNG") else ".webp")
                        out_path = os.path.join(target_dir, f"ig_{int(time.time()*1000)}_p{i}{ext}")
                        with open(out_path, "wb") as f:
                            f.write(img_resp.content)
                        downloaded_media.append((out_path, short_title, False))
            except Exception as e:
                print(f"⚠️ Error downloading fallback Instagram image {i}: {e}")

        if downloaded_media:
            if caption:
                for chunk in split_telegram_text(caption):
                    files.append(("text_only", html.escape(chunk, quote=False), False))
            files.extend(downloaded_media)
            return files
    except Exception as e:
        print(f"⚠️ Instagram web scraper error: {e}")

    return None

def decode_facebook_json_text(page_text):
    """Unescapes Facebook HTML/JSON blobs into a searchable plain string."""
    if not page_text:
        return ""
    decoded = html.unescape(page_text).replace("\\/", "/")
    decoded = decoded.replace("\\u0025", "%").replace("\\u0026", "&").replace("\\u003d", "=")
    decoded = decoded.replace("\\u003F", "?").replace("\\u003f", "?")
    return decoded

def normalize_facebook_image_url(raw_url):
    """Normalizes Facebook's heavily escaped image URLs into direct downloadable URLs."""
    if not raw_url:
        return None

    try:
        url = html.unescape(str(raw_url)).strip()
        url = url.replace("\\/", "/")
        url = url.replace("\\u0025", "%").replace("\\u0026", "&").replace("\\u003d", "=")
        url = url.replace("\\u003F", "?").replace("\\u003f", "?")
        url = url.strip(" \t\r\n'\"<>")

        # Some Facebook JSON blobs keep URLs percent-encoded inside another URL-ish value.
        for _ in range(2):
            decoded = urllib.parse.unquote(url)
            if decoded == url:
                break
            url = decoded

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None

        host = parsed.hostname.lower()
        if not (host.endswith("fbcdn.net") or host.endswith("fbsbx.com")):
            return None

        bad_markers = (
            "safe_image.php", "emoji.php", "rsrc.php", "profile_images",
            "static.xx.fbcdn.net", "external.xx.fbcdn.net",
            # Ad creative / suggested-video thumbnail CDN paths
            "/t45.1600-", "/t45.5328-", "/t15.5256-", "/t15.13418-",
            "/t39.1997-", "/t1.30497-",
        )
        lower = url.lower()
        if any(marker in lower for marker in bad_markers):
            return None

        # Reject tiny UI thumbnails when Facebook exposes dimensions in the URL.
        dim_matches = re.findall(r"(?:p|s)(\d{2,4})x(\d{2,4})", url)
        if dim_matches:
            largest_side = max(max(int(w), int(h)) for w, h in dim_matches)
            if largest_side < 180:
                return None

        image_like_path = re.search(r"\.(?:jpg|jpeg|png|webp)(?:[?&/#]|$)", parsed.path + "?" + parsed.query, re.IGNORECASE)
        lookaside_media = host.endswith("fbsbx.com") and "media" in parsed.path.lower()
        if not image_like_path and not lookaside_media:
            return None

        return urllib.parse.urlunparse(parsed)
    except Exception:
        return None

def _facebook_media_dedupe_key(url):
    """Stable key so resized variants of the same FB photo collapse to one item."""
    if not url:
        return None
    m_id = re.search(r'media_id=(\d+)', url)
    if m_id:
        return f"media_{m_id.group(1)}"
    m = re.search(r"/(\d+_\d+_\d+_[no])\.(?:jpg|jpeg|png|webp)", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return url.split("?", 1)[0].split("#", 1)[0]

def extract_facebook_attachment_media(page_text, is_reel=False, target_post_id=None):
    """
    Extracts Facebook post attachments directly from Comet GraphQL / HTML chunks.
    Handles all_subattachments (albums/carousels), progressive_urls (reels/videos),
    and canonical og:image/og:video for single media posts.
    """
    if not page_text:
        return {"items": [], "mediaset_token": None, "expected_count": 0}

    decoded = decode_facebook_json_text(page_text)
    results = []
    seen = set()
    mediaset_token = None
    expected_count = None

    # Scope extraction to target post when ID is available to isolate from feed ads/recommendations
    search_scope = decoded
    if target_post_id:
        t_match = re.search(rf'"post_id"\s*:\s*"{target_post_id}"', decoded) or \
                  re.search(rf'"{target_post_id}"', decoded)
        if t_match:
            search_scope = decoded[max(0, t_match.start() - 1000): t_match.start() + 250000]

    token_match = re.search(r'"mediaset_token"\s*:\s*"(pcb\.\d+)"', search_scope)
    if token_match:
        cand_token = token_match.group(1)
        # Only accept mediaset token if it belongs to the target post (avoids feed recommendations)
        if not target_post_id or cand_token.replace("pcb.", "") == target_post_id:
            mediaset_token = cand_token

    for match in re.finditer(
        r'"all_subattachments"\s*:\s*\{[^{}]*?"nodes"\s*:\s*\[',
        search_scope,
    ):
        after_bracket = search_scope[match.end(): match.end() + 10].strip()
        if after_bracket.startswith("]"):
            continue

        count_m = re.search(r'"count"\s*:\s*(\d+)', search_scope[max(0, match.start() - 200): match.end()])
        expected = int(count_m.group(1)) if count_m else 10
        if expected_count is None or expected > expected_count:
            expected_count = expected

        # Strictly bound the scan window to the nodes array so we never parse subsequent feed items/ads
        close_idx = search_scope.find("]", match.end())
        if close_idx != -1 and close_idx > match.end():
            chunk = search_scope[match.end(): close_idx]
        else:
            chunk = search_scope[match.end(): match.end() + max(expected, 1) * 20000 + 8000]
            next_block = re.search(r'"all_subattachments"\s*:\s*\{', chunk)
            if next_block:
                chunk = chunk[: next_block.start()]

        local_token = re.search(r'"mediaset_token"\s*:\s*"(pcb\.\d+)"', search_scope[max(0, match.start() - 500): match.start() + 200])
        if local_token:
            mediaset_token = local_token.group(1)

        node_parts = re.split(r'"deduplication_key"\s*:\s*"', chunk)
        for part in node_parts[1:]:
            typename = re.search(r'"__typename"\s*:\s*"(\w+)"', part)
            kind = typename.group(1) if typename else ""
            media_id_match = re.search(r'"id"\s*:\s*"(\d+)"', part)
            media_id = media_id_match.group(1) if media_id_match else None



            if kind == "Photo" or '"__isMedia":"Photo"' in part or '"__isMedia": "Photo"' in part:
                viewer = re.search(
                    r'"viewer_image"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"',
                    part,
                )
                image = re.search(
                    r'"image"\s*:\s*\{\s*"uri"\s*:\s*"([^"]+)"',
                    part,
                )
                raw = (viewer.group(1) if viewer else None) or (image.group(1) if image else None)
                normalized = normalize_facebook_image_url(raw)
                if not normalized:
                    continue
                key = _facebook_media_dedupe_key(normalized) or media_id
                if not key or key in seen:
                    continue
                seen.add(key)
                if media_id:
                    seen.add(media_id)
                results.append({"kind": "photo", "url": normalized, "id": media_id})

            elif kind == "Video" or '"__isMedia":"Video"' in part or '"__isMedia": "Video"' in part:
                video_url = None
                for pm in re.finditer(r'"progressive_urls"\s*:\s*(\[[^\]]+\])', part):
                    try:
                        pdata = json.loads(pm.group(1))
                        hd = [x['progressive_url'] for x in pdata if x.get('metadata', {}).get('quality') == 'HD' and x.get('progressive_url')]
                        if hd:
                            video_url = hd[0]
                            break
                        sd = [x['progressive_url'] for x in pdata if x.get('progressive_url')]
                        if sd:
                            video_url = sd[0]
                            break
                    except Exception:
                        pass
                if not video_url:
                    for pat in (
                        r'"browser_native_hd_url"\s*:\s*"([^"]+)"',
                        r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
                        r'"browser_native_sd_url"\s*:\s*"([^"]+)"',
                        r'"playable_url"\s*:\s*"([^"]+)"',
                    ):
                        vm = re.search(pat, part)
                        if vm:
                            video_url = html.unescape(vm.group(1)).replace("\\/", "/")
                            break
                if not video_url or not video_url.startswith("http"):
                    continue
                key = video_url.split("?", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
                results.append({"kind": "video", "url": video_url, "id": media_id})

        if results:
            break

    # If no carousel items were found:
    if not results:
        # 1. Target post single photo from Comet attachments in search_scope
        for sp_m in re.finditer(r'"attachments"\s*:\s*\[\s*\{\s*"media"\s*:\s*\{[^{}]*?"__typename"\s*:\s*"Photo"[^{}]*?"id"\s*:\s*"(\d+)"', search_scope):
            photo_id = sp_m.group(1)
            p_chunk = search_scope[sp_m.start(): sp_m.start() + 15000]
            uri_m = re.search(r'"photo_image"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"', p_chunk) or \
                    re.search(r'"viewer_image"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"', p_chunk) or \
                    re.search(r'"image"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"', p_chunk)
            if uri_m:
                norm = normalize_facebook_image_url(uri_m.group(1))
                if norm and not any(bad in norm.lower() for bad in ["fb_icon", "logo", "silhouette"]):
                    results.append({"kind": "photo", "url": norm, "id": photo_id})
                    break

        og_img_m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', page_text, re.IGNORECASE) or \
                   re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', page_text, re.IGNORECASE)
        has_video_tag = bool(re.search(r'<meta[^>]+(?:property|name)=["\']og:video["\']', page_text, re.IGNORECASE))

        # 2. If it's explicitly a reel or has video tag, look for standalone video stream strictly in search_scope
        if not results and (is_reel or has_video_tag):
            standalone_video = None
            for pm in re.finditer(r'"progressive_urls"\s*:\s*(\[[^\]]+\])', search_scope):
                try:
                    pdata = json.loads(pm.group(1))
                    hd = [x['progressive_url'] for x in pdata if x.get('metadata', {}).get('quality') == 'HD' and x.get('progressive_url')]
                    if hd:
                        standalone_video = hd[0]
                        break
                    sd = [x['progressive_url'] for x in pdata if x.get('progressive_url')]
                    if sd:
                        standalone_video = sd[0]
                        break
                except Exception:
                    pass
            if not standalone_video:
                for pat in (
                    r'"browser_native_hd_url"\s*:\s*"([^"]+)"',
                    r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
                    r'"browser_native_sd_url"\s*:\s*"([^"]+)"',
                    r'"playable_url"\s*:\s*"([^"]+)"',
                ):
                    vm = re.search(pat, search_scope)
                    if vm:
                        standalone_video = html.unescape(vm.group(1)).replace("\\/", "/")
                        break
            if standalone_video:
                results.append({"kind": "video", "url": standalone_video, "id": None})

        # 3. If no media was extracted, check for single photo via canonical og:image
        if not results and og_img_m:
            norm = normalize_facebook_image_url(og_img_m.group(1))
            if norm and not any(bad in norm.lower() for bad in ["fb_icon", "logo", "silhouette"]):
                results.append({"kind": "photo", "url": norm, "id": None})

    return {
        "items": results,
        "mediaset_token": mediaset_token,
        "expected_count": expected_count,
    }

def extract_facebook_mediaset_photo_ids(page_text, mediaset_token=None, known_ids=None):
    """Collect ordered photo IDs belonging to a Facebook album/mediaset."""
    if not page_text:
        return list(known_ids or [])
    decoded = decode_facebook_json_text(page_text)
    token_num = mediaset_token.replace("pcb.", "") if mediaset_token else None
    known_ids = [k for k in (known_ids or []) if k]

    prefix = None
    if known_ids:
        prefix = known_ids[0][:8]
    elif token_num:
        prefix = token_num[:8]
    if not prefix:
        return list(known_ids)

    ids = []
    seen = set()
    # Keep known IDs first (preserves carousel order from all_subattachments).
    for pid in known_ids:
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)

    for pid in re.findall(rf'"({re.escape(prefix)}\d+)"', decoded):
        if token_num and pid == token_num:
            continue
        if pid in seen:
            continue
        # Album photo IDs are typically 16 digits; skip short/unrelated matches.
        if len(pid) < 15:
            continue
        seen.add(pid)
        ids.append(pid)
    return ids

def extract_facebook_photo_url_from_page(page_text, photo_id=None):
    """Best image URL from an individual Facebook /photo/ page."""
    if not page_text:
        return None
    decoded = decode_facebook_json_text(page_text)

    candidates = []
    if photo_id:
        for m in re.finditer(rf'"{re.escape(photo_id)}"', decoded):
            snippet = decoded[max(0, m.start() - 400): m.start() + 4500]
            for pat in (
                r'"viewer_image"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"',
                r'"(https://[^"]*t39\.99422[^"]+)"',
                r'"(https://[^"]*t39\.30808[^"]+)"',
                r'"uri"\s*:\s*"(https://[^"]+fbcdn\.net[^"]+\.(?:jpg|png|webp)\?[^"]+)"',
            ):
                for raw in re.findall(pat, snippet):
                    url = normalize_facebook_image_url(raw)
                    if url:
                        candidates.append(url)

    if not candidates:
        for pat in (
            r'"viewer_image"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"',
            r'"(https://[^"]*t39\.99422[^"]+)"',
        ):
            for raw in re.findall(pat, decoded):
                url = normalize_facebook_image_url(raw)
                if url:
                    candidates.append(url)

    def score(u):
        m = re.search(r"ctp=s(\d+)x(\d+)", u)
        if m:
            return int(m.group(1)) * int(m.group(2))
        m = re.search(r"cstp=mx(\d+)x(\d+)", u)
        if m:
            return int(m.group(1)) * int(m.group(2))
        return 0

    if not candidates:
        return None
    candidates.sort(key=score, reverse=True)
    return candidates[0]

def expand_facebook_album_attachments(attachment_data, referer_url=None, target_post_id=None):
    """
    Facebook post HTML often inlines only the first ~5 carousel photos.
    When mediaset_token + expected_count indicate more items, resolve the rest
    via the media set / individual photo pages.
    """
    items = list(attachment_data.get("items") or [])
    token = attachment_data.get("mediaset_token")
    expected = attachment_data.get("expected_count") or 0

    if not token or expected <= len(items):
        return items

    known_ids = [it.get("id") for it in items if it.get("id")]
    token_num = token.replace("pcb.", "") if token else ""
    post_boundary_id = target_post_id or (token_num if token and token.startswith("pcb.") else None)
    id_cluster_prefix = known_ids[0][:7] if known_ids else (token_num[:7] if token_num else None)

    photo_ids = []
    set_url = f"https://www.facebook.com/media/set/?set={token}&type=3"
    set_resp = fetch_facebook_post_html(set_url)
    if set_resp and set_resp.status_code == 200:
        photo_ids = extract_facebook_mediaset_photo_ids(set_resp.text, token, known_ids=known_ids)

    if len(photo_ids) < expected and attachment_data.get("_raw_html"):
        more = extract_facebook_mediaset_photo_ids(
            attachment_data["_raw_html"], token, known_ids=known_ids or photo_ids
        )
        for pid in more:
            if pid not in photo_ids:
                photo_ids.append(pid)

    # If media set page only rendered the initial chunk (e.g. 24 photos), traverse the photo viewer carousel
    if len(photo_ids) < expected and photo_ids:
        curr_id = photo_ids[-1]
        seen_pids = set(photo_ids)
        target_limit = min(expected, 100)
        while len(photo_ids) < target_limit:
            p_url = f"https://www.facebook.com/photo/?fbid={curr_id}&set={token}"
            p_resp = fetch_facebook_post_html(p_url)
            if not p_resp:
                break

            # Stop carousel traversal if we crossed the post boundary
            if post_boundary_id and post_boundary_id not in p_resp.text:
                break

            p_img = extract_facebook_photo_url_from_page(p_resp.text, photo_id=curr_id)
            if p_img:
                k = _facebook_media_dedupe_key(p_img)
                if k and not any(_facebook_media_dedupe_key(it["url"]) == k for it in items):
                    items.append({"kind": "photo", "url": p_img, "id": curr_id})

            p_decoded = decode_facebook_json_text(p_resp.text)
            next_m = re.search(r'"prevMedia"\s*:\s*\{\s*"edges"\s*:\s*\[\s*\{\s*"node"\s*:\s*\{\s*"__typename"\s*:\s*"Photo",\s*"id"\s*:\s*"(\d+)"', p_decoded)
            if not next_m:
                next_m = re.search(r'"nextMedia"\s*:\s*\{\s*"edges"\s*:\s*\[\s*\{\s*"node"\s*:\s*\{\s*"__typename"\s*:\s*"Photo",\s*"id"\s*:\s*"(\d+)"', p_decoded)
            if not next_m:
                break
            next_id = next_m.group(1)
            if next_id in seen_pids or next_id == token_num:
                break
            # Do not traverse beyond the current album/post ID cluster
            if id_cluster_prefix and not next_id.startswith(id_cluster_prefix):
                break
            seen_pids.add(next_id)
            photo_ids.append(next_id)
            curr_id = next_id

    have_ids = {it.get("id") for it in items if it.get("id")}
    missing_ids = [pid for pid in photo_ids if pid not in have_ids and (not id_cluster_prefix or pid.startswith(id_cluster_prefix))]

    if missing_ids:
        def _fetch_single_album_photo(pid):
            try:
                if id_cluster_prefix and not pid.startswith(id_cluster_prefix):
                    return None
                p_url = f"https://www.facebook.com/photo/?fbid={pid}&set={token}"
                p_resp = fetch_facebook_post_html(p_url)
                if p_resp:
                    p_img = extract_facebook_photo_url_from_page(p_resp.text, photo_id=pid)
                    if p_img:
                        return {"kind": "photo", "url": p_img, "id": pid}
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_fetch_single_album_photo, pid) for pid in missing_ids]
            for fut in futures:
                res = fut.result()
                if res:
                    key = _facebook_media_dedupe_key(res["url"])
                    if key and not any(_facebook_media_dedupe_key(it["url"]) == key for it in items):
                        items.append(res)

    return items

def extract_facebook_image_urls(page_text):
    """
    Fallback image extractor when all_subattachments is missing.
    Prefer og:image / twitter:image, then post-like fbcdn URLs while skipping ads.
    """
    if not page_text:
        return []

    variants = [page_text]
    decoded = decode_facebook_json_text(page_text)
    if decoded != page_text:
        variants.append(decoded)

    raw_candidates = []
    for text in variants:
        raw_candidates.extend(re.findall(
            r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
            text,
            flags=re.IGNORECASE
        ))
        raw_candidates.extend(re.findall(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
            text,
            flags=re.IGNORECASE
        ))
        # Prefer known post-photo CDN families over generic page scrapes.
        raw_candidates.extend(re.findall(
            r'https?:\\?/\\?/[^"\'<>\s]+(?:fbcdn\.net|fbsbx\.com)/v/(?:t39\.99422|t39\.30808)[^"\'<>\s]+',
            text,
            flags=re.IGNORECASE
        ))

    seen = set()
    urls = []
    for candidate in raw_candidates:
        normalized = normalize_facebook_image_url(candidate)
        if not normalized:
            continue
        key = _facebook_media_dedupe_key(normalized)
        if not key or key in seen:
            continue
        seen.add(key)
        urls.append(normalized)

    return urls

def extract_facebook_post_caption(page_text, target_post_id=None):
    """Best-effort caption from og:description, GraphQL message.text, or title."""
    if not page_text:
        return None

    # 1. Post-specific og:description (very reliable for shared posts & reels)
    og_desc = None
    m = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', page_text, re.IGNORECASE) or \
        re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:description["\']', page_text, re.IGNORECASE)
    if m:
        desc = html.unescape(m.group(1)).strip()
        # Drop page follower/like boilerplate in English or Hindi
        if not re.search(r'\d+[\d.,]*\s*(?:likes?|followers?|talking about this|पसंद)', desc, re.IGNORECASE):
            if desc and desc.lower() not in ("facebook", "log into facebook", "log in", "error"):
                og_desc = desc

    # 2. GraphQL story message.text (contains full untruncated post body)
    json_msg = None
    decoded = decode_facebook_json_text(page_text)
    for msg_m in re.finditer(r'"message"\s*:\s*\{[^{}]*?"text"\s*:\s*"((?:\\.|[^"\\])*)"', decoded):
        try:
            cand = bytes(msg_m.group(1), "utf-8").decode("unicode_escape", errors="ignore")
            cand = html.unescape(cand).strip()
            cand = cand.encode("utf-8", "ignore").decode("utf-8", "ignore").strip()
            if cand and cand.lower() not in ("facebook", "log into facebook", "log in", "error", "#"):
                json_msg = cand
                break
        except Exception:
            pass

    if json_msg:
        if not og_desc or len(json_msg) >= len(og_desc) or og_desc.endswith("..."):
            return json_msg
    if og_desc:
        return og_desc

    # 3. Clean fallback to og:title or <title>
    title_m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', page_text, re.IGNORECASE) or \
              re.search(r'<title>(.*?)</title>', page_text, re.IGNORECASE)
    if title_m:
        caption = html.unescape(title_m.group(1)).strip()
        for suffix in (" | Facebook", " - Facebook", " | Meta"):
            if caption.endswith(suffix):
                caption = caption[:-len(suffix)].strip()
        if caption.lower() not in ("facebook", "log into facebook", "log in", "error", ""):
            return caption

    return None

def fetch_facebook_post_html(url):
    """
    Fetch Facebook post HTML with UA fallbacks.
    Logged-in Chrome sessions are occasionally blocked (HTTP 400); rotate UAs.
    """
    session = get_auth_session()
    header_variants = [
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
        {
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ]

    best = None
    for headers in header_variants:
        try:
            resp = session.get(url, headers=headers, allow_redirects=True, timeout=20)
            if resp.status_code != 200 or len(resp.text) < 2000:
                continue
            if "all_subattachments" in resp.text or "og:image" in resp.text or "fbcdn.net" in resp.text or "currMedia" in resp.text:
                return resp
            if best is None or len(resp.text) > len(best.text):
                best = resp
        except Exception as e:
            print(f"⚠️ Facebook fetch attempt failed: {e}")
    return best

def fetch_facebook_ssr_metadata(url):
    """Fetches clean OpenGraph metadata as Facebook crawler without session cookies."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"},
            allow_redirects=True,
            timeout=10
        )
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None

def extract_facebook_media(url, dest_dir=None):
    """Direct HD image/video scraper for Facebook photo posts, albums, and mixed carousels."""
    target_dir = dest_dir or DOWNLOADS_DIR
    try:
        # Resolve target post ID from URL or redirects
        target_post_id = None
        pid_m = re.search(r'/(?:posts|videos|photos|reel)/(\d+)', url) or \
                re.search(r'(?:story_fbid|fbid)=(\d+)', url)
        if pid_m:
            target_post_id = pid_m.group(1)

        if ("/share/" in url.lower() or "fb.watch" in url.lower()):
            try:
                r = get_auth_session().head(url, allow_redirects=True, timeout=8)
                if r.url and r.url != url:
                    url = r.url
                    pid_m = re.search(r'/(?:posts|videos|photos|reel)/(\d+)', url) or \
                            re.search(r'(?:story_fbid|fbid)=(\d+)', url)
                    if pid_m:
                        target_post_id = pid_m.group(1)
            except Exception:
                pass

        u_lower = url.lower()
        is_reel = ("/reel/" in u_lower or "/reels/" in u_lower or "/videos/" in u_lower or "/watch" in u_lower) and not (
            "/posts/" in u_lower or "/photos/" in u_lower or "/p/" in u_lower
        )

        # 1. Fetch unauthenticated crawler SSR metadata (clean post caption & canonical single image, 0 ads/feed noise)
        ssr_text = fetch_facebook_ssr_metadata(url)
        caption = extract_facebook_post_caption(ssr_text, target_post_id=target_post_id) if ssr_text else None

        if not target_post_id and ssr_text:
            og_url_m = re.search(r'<meta\s+property=["\']og:url["\']\s+content=["\']([^"\']+)["\']', ssr_text, re.IGNORECASE)
            if og_url_m:
                pid_m = re.search(r'/(?:posts|videos|photos|reel)/(\d+)', og_url_m.group(1)) or \
                        re.search(r'(?:story_fbid|fbid)=(\d+)', og_url_m.group(1))
                if pid_m:
                    target_post_id = pid_m.group(1)

        # 2. Fetch full Comet HTML for album attachments / HD video
        resp = fetch_facebook_post_html(url)
        text = resp.text if resp else (ssr_text or "")
        comet_caption = extract_facebook_post_caption(text, target_post_id=target_post_id)
        if comet_caption:
            if not caption or len(comet_caption) > len(caption) or (caption and caption.endswith("...")):
                caption = comet_caption

        attachment_data = extract_facebook_attachment_media(text, is_reel=is_reel, target_post_id=target_post_id)
        attachment_data["_raw_html"] = text
        items = expand_facebook_album_attachments(attachment_data, referer_url=url, target_post_id=target_post_id)

        # 3. If items is empty, fallback to SSR canonical og:image/og:video
        if not items and ssr_text:
            ssr_att = extract_facebook_attachment_media(ssr_text, is_reel=is_reel, target_post_id=target_post_id)
            if ssr_att.get("items"):
                items = ssr_att["items"]

        download_plan = []
        if items:
            download_plan = [(it["kind"], it["url"]) for it in items if it.get("url")]
        elif ssr_text:
            og_img = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', ssr_text, re.IGNORECASE) or \
                     re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', ssr_text, re.IGNORECASE)
            if og_img:
                norm = normalize_facebook_image_url(og_img.group(1))
                if norm and not any(bad in norm.lower() for bad in ["fb_icon", "logo", "silhouette"]):
                    download_plan.append(("photo", norm))

        if not download_plan:
            return None

        os.makedirs(target_dir, exist_ok=True)
        files = []
        # Deliver full post text thread-wise when present (matches X/Twitter behavior).
        if caption:
            for chunk in split_telegram_text(caption):
                files.append(("text_only", html.escape(chunk, quote=False), False))

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.facebook.com/",
        }
        session = get_auth_session()

        short_cap = caption.split("\n")[0][:120] if caption else None

        def _download_fb_item(item_tuple):
            i, (kind, media_url) = item_tuple
            try:
                if kind == "video":
                    out_path = os.path.join(target_dir, f"fb_{int(time.time()*1000)}_{i}.mp4")
                    if _download_binary(media_url, out_path, min_size=1000, headers=headers):
                        return (i, (out_path, short_cap, False))
                    return None

                m_headers = dict(headers)
                if "lookaside.fbsbx.com" in media_url or "crawler" in media_url:
                    m_headers["User-Agent"] = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
                    media_resp = requests.get(media_url, headers=m_headers, timeout=20)
                else:
                    media_resp = session.get(media_url, headers=m_headers, timeout=20)
                content = media_resp.content
                content_type = media_resp.headers.get("content-type", "").lower()
                if media_resp.status_code == 200 and len(content) > 1000:
                    if content.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF")):
                        ext = ".jpg" if content.startswith(b"\xff\xd8\xff") or "jpeg" in content_type else (
                            ".png" if content.startswith(b"\x89PNG") else ".webp"
                        )
                        out_path = os.path.join(target_dir, f"fb_{int(time.time()*1000)}_{i}{ext}")
                        with open(out_path, "wb") as f:
                            f.write(content)
                        return (i, (out_path, short_cap, False))
            except Exception as e:
                print(f"⚠️ Error downloading FB {kind} {i}: {e}")
            return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            dl_results = list(pool.map(_download_fb_item, enumerate(download_plan)))
            for res in sorted([r for r in dl_results if r], key=lambda x: x[0]):
                files.append(res[1])

        # Only succeed when at least one real media file was saved (text alone is not enough).
        if any(f[0] != "text_only" for f in files):
            return files
    except Exception as e:
        print(f"⚠️ Facebook photo scraper error: {e}")
    return None

def parse_twitter_status_id(url):
    """Extract numeric status ID from an X/Twitter URL."""
    if not url:
        return None
    m = re.search(r'(?:twitter\.com|x\.com)/[^/]+/status(?:es)?/(\d+)', url, re.IGNORECASE)
    return m.group(1) if m else None

def split_telegram_text(text, limit=3500):
    """Split long post text into Telegram-safe chunks (thread-wise delivery)."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 3:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [c for c in chunks if c]

def _download_binary(url, out_path, min_size=1000, headers=None):
    """Download a remote binary to out_path. Returns True on success."""
    try:
        resp = get_auth_session().get(url, headers=headers or {}, timeout=45, stream=True)
        if resp.status_code != 200:
            return False
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        if os.path.getsize(out_path) < min_size:
            try:
                os.remove(out_path)
            except Exception:
                pass
            return False
        return True
    except Exception as e:
        print(f"⚠️ Binary download failed: {e}")
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return False

def _twitter_from_graphql(status_id):
    """Fetch live tweet and full conversation thread directly via Twitter GraphQL API using cookies.txt session."""
    session = get_auth_session()
    ct0 = None
    for c in session.cookies:
        if c.name == 'ct0':
            ct0 = c.value
            break

    BEARER = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'
    headers = {
        'authorization': f'Bearer {BEARER}',
        'x-csrf-token': ct0 or '',
        'x-twitter-active-user': 'yes',
        'x-twitter-auth-type': 'OAuth2Session',
        'x-twitter-client-language': 'en',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    query_ids = ['nBS-WpgA6ZG0CyNHD517JQ', '5G6eAKSlRA_rgsm78z052A', 'xOhJFmRytTk6yodNmSHQtA']

    for qid in query_ids:
        try:
            url = f'https://x.com/i/api/graphql/{qid}/TweetDetail'
            params = {
                'variables': json.dumps({
                    'focalTweetId': str(status_id),
                    'with_rux_injections': False,
                    'includePromotedContent': False,
                    'withCommunity': True,
                    'withQuickPromoteEligibilityTweetFields': False,
                    'withBirdwatchNotes': False,
                    'withVoice': True,
                    'withV2Timeline': True
                }),
                'features': json.dumps({
                    'rweb_lists_timeline_redesign_enabled': True,
                    'responsive_web_graphql_exclude_directive_enabled': True,
                    'verified_phone_label_enabled': False,
                    'creator_subscriptions_tweet_preview_api_enabled': True,
                    'responsive_web_graphql_timeline_navigation_enabled': True,
                    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
                    'tweetypie_unmention_optimization_enabled': True,
                    'responsive_web_edit_tweet_api_enabled': True,
                    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
                    'view_counts_everywhere_api_enabled': True,
                    'longform_notetweets_consumption_enabled': True,
                    'responsive_web_twitter_article_tweet_consumption_enabled': False,
                    'tweet_awards_web_tipping_enabled': False,
                    'freedom_of_speech_not_reach_fetch_enabled': True,
                    'standardized_nudges_misinfo': True,
                    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
                    'longform_notetweets_rich_text_read_enabled': True,
                    'longform_notetweets_inline_media_enabled': True,
                    'responsive_web_media_download_video_enabled': False,
                    'responsive_web_enhance_cards_enabled': False
                })
            }
            resp = session.get(url, params=params, headers=headers, timeout=12)
            if resp.status_code != 200:
                continue
            data = resp.json()
            instructions = data.get('data', {}).get('threaded_conversation_with_injections_v2', {}).get('instructions', [])
            if not instructions:
                continue

            def extract_tweet(res):
                if not isinstance(res, dict): return None
                if res.get('__typename') == 'TweetWithVisibilityResults': res = res.get('tweet', {})
                leg = res.get('legacy') or {}
                usr = (res.get('core') or {}).get('user_results', {}).get('result', {}).get('legacy', {})
                tid = leg.get('id_str') or res.get('rest_id')
                txt = leg.get('full_text') or ''
                note_txt = res.get('note_tweet', {}).get('note_tweet_results', {}).get('result', {}).get('text')
                if note_txt:
                    txt = note_txt
                if not tid or not txt: return None

                media = leg.get('extended_entities', {}).get('media', []) or leg.get('entities', {}).get('media', [])
                photos = [m['media_url_https'] + ':orig' for m in media if m.get('type') == 'photo']
                videos = []
                for m in media:
                    if m.get('type') in ('video', 'animated_gif'):
                        mp4s = [v for v in m.get('video_info', {}).get('variants', []) if v.get('content_type') == 'video/mp4']
                        mp4s.sort(key=lambda v: v.get('bitrate', 0), reverse=True)
                        if mp4s: videos.append(mp4s[0]['url'])

                return {
                    'id': str(tid),
                    'text': txt,
                    'author_name': usr.get('name', ''),
                    'author_handle': usr.get('screen_name', ''),
                    'in_reply_to': str(leg.get('in_reply_to_status_id_str') or ''),
                    'photos': photos,
                    'videos': videos
                }

            raw_tweets = []
            for inst in instructions:
                for ent in inst.get('entries', []):
                    tw = extract_tweet(ent.get('content', {}).get('itemContent', {}).get('tweet_results', {}).get('result'))
                    if tw: raw_tweets.append(tw)
                    for item in ent.get('content', {}).get('items', []):
                        tw2 = extract_tweet(item.get('item', {}).get('itemContent', {}).get('tweet_results', {}).get('result'))
                        if tw2: raw_tweets.append(tw2)

            if not raw_tweets:
                continue

            focal = raw_tweets[0]
            target_author = focal['author_handle'].lower()
            thread_ids = set([str(status_id)])

            author_thread = [focal]
            for t in raw_tweets[1:]:
                if t['id'] in thread_ids: continue
                # Must be by the same author and part of the post chain
                if t['author_handle'].lower() == target_author:
                    # Ignore comment replies starting with @other_user
                    if t['text'].startswith('@') and not any(f'@{target_author}' in t['text'].split()[0].lower() for _ in [1]):
                        continue
                    thread_ids.add(t['id'])
                    author_thread.append(t)

            all_photos = []
            all_videos = []
            thread_texts = []
            for t in author_thread:
                clean_text = re.sub(r'\shttps://t\.co/\w+$', '', t['text']).strip()
                if clean_text:
                    thread_texts.append(clean_text)
                all_photos.extend(t['photos'])
                all_videos.extend(t['videos'])

            return {
                'text': thread_texts[0] if thread_texts else '',
                'thread_texts': thread_texts,
                'photos': all_photos,
                'videos': all_videos,
                'author_name': focal['author_name'],
                'author_handle': focal['author_handle']
            }
        except Exception as e:
            print(f'⚠️ GraphQL Twitter fetch failed with qid {qid}: {e}')
    return None

def _twitter_from_fxtwitter(status_id):
    """Fetch tweet payload via FxTwitter (photos, videos, text)."""
    endpoints = (
        f"https://api.fxtwitter.com/status/{status_id}",
        f"https://api.vxtwitter.com/status/{status_id}",
    )
    session = get_auth_session()
    for endpoint in endpoints:
        try:
            resp = session.get(endpoint, timeout=15, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            data = resp.json()
            tweet = data.get("tweet") or data
            if not isinstance(tweet, dict):
                continue
            if not (tweet.get("text") or tweet.get("media") or data.get("media_extended")):
                # vxtwitter shape uses top-level fields
                if not data.get("text"):
                    continue
                tweet = data

            text = (tweet.get("text") or tweet.get("full_text") or "").strip()
            # Strip trailing t.co media link noise for cleaner captions.
            text = re.sub(r"\shttps://t\.co/\w+$", "", text).strip()

            media_items = []
            media_obj = tweet.get("media") or {}
            if isinstance(media_obj, dict):
                for item in media_obj.get("all") or []:
                    media_items.append(item)
            elif isinstance(media_obj, list):
                media_items.extend(media_obj)

            # vxtwitter compatibility
            for item in tweet.get("media_extended") or data.get("media_extended") or []:
                media_items.append(item)

            photos = []
            videos = []
            seen = set()
            for item in media_items:
                if not isinstance(item, dict):
                    continue
                mtype = (item.get("type") or item.get("media_type") or "").lower()
                murl = item.get("url") or item.get("media_url_https") or item.get("thumbnail_url")
                # Prefer video variants when present
                if mtype in ("video", "gif", "animated_gif"):
                    murl = (
                        item.get("url")
                        or item.get("video_url")
                        or (item.get("variants") or [{}])[-1].get("url")
                        or murl
                    )
                    if murl and murl not in seen:
                        seen.add(murl)
                        videos.append(murl)
                elif mtype in ("photo", "image", "") or (murl and "pbs.twimg.com/media" in str(murl)):
                    if murl and "profile_images" not in murl and murl not in seen:
                        # Prefer original size when FxTwitter already provides ?name=orig
                        if "pbs.twimg.com/media" in murl and "name=" not in murl:
                            murl = murl + ("&" if "?" in murl else "?") + "name=orig"
                        seen.add(murl)
                        photos.append(murl)

            # Explicit arrays from fxtwitter
            for item in (media_obj.get("photos") if isinstance(media_obj, dict) else None) or []:
                murl = item.get("url")
                if murl and murl not in seen and "profile_images" not in murl:
                    seen.add(murl)
                    photos.append(murl)
            for item in (media_obj.get("videos") if isinstance(media_obj, dict) else None) or []:
                murl = item.get("url")
                if murl and murl not in seen:
                    seen.add(murl)
                    videos.append(murl)

            author_info = tweet.get("author") or {}
            author_name = author_info.get("name") or ""
            author_handle = author_info.get("screen_name") or ""
            return {"text": text, "photos": photos, "videos": videos, "author_name": author_name, "author_handle": author_handle}
        except Exception as e:
            print(f"⚠️ FxTwitter fetch failed ({endpoint}): {e}")
    return None

def _twitter_from_syndication(status_id):
    """Fallback tweet payload via Twitter syndication CDN."""
    try:
        resp = get_auth_session().get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&token=0",
            timeout=15,
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = (data.get("text") or "").strip()
        text = re.sub(r"\shttps://t\.co/\w+$", "", text).strip()
        user_info = data.get("user") or {}
        author_name = user_info.get("name") or ""
        author_handle = user_info.get("screen_name") or ""

        photos = []
        videos = []
        seen = set()
        for item in data.get("mediaDetails") or data.get("photos") or []:
            mtype = (item.get("type") or "photo").lower()
            if mtype in ("video", "animated_gif"):
                variants = ((item.get("video_info") or {}).get("variants") or [])
                mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
                mp4s.sort(key=lambda v: v.get("bitrate") or 0, reverse=True)
                if mp4s and mp4s[0]["url"] not in seen:
                    seen.add(mp4s[0]["url"])
                    videos.append(mp4s[0]["url"])
            else:
                murl = item.get("media_url_https") or item.get("url")
                if murl and "profile_images" not in murl and murl not in seen:
                    if "pbs.twimg.com/media" in murl and "name=" not in murl:
                        murl = murl + ("&" if "?" in murl else "?") + "name=orig"
                    seen.add(murl)
                    photos.append(murl)
        return {"text": text, "photos": photos, "videos": videos, "author_name": author_name, "author_handle": author_handle}
    except Exception as e:
        print(f"⚠️ Twitter syndication fetch failed: {e}")
    return None

def _clean_thread_item(text):
    text = re.sub(r'thread#showTweet[^\n]*', '', text)
    text = re.sub(r'data-screenname=[^\n]*', '', text)
    text = re.sub(r'dir="auto">?', '', text)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n\n'.join(lines)

def _twitter_fetch_thread(status_id):
    """Fetches full trailing thread tweets from ThreadReader unroll engine."""
    url = f"https://threadreaderapp.com/thread/{status_id}.html"
    try:
        r = get_auth_session().get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=8
        )
        if r.status_code != 200:
            return []
        
        parts = re.split(r'data-tweet="(\d+)"', r.text)
        if len(parts) < 3:
            return []
        
        trailing_tweets = []
        seen_ids = set([str(status_id)])
        
        for idx in range(1, len(parts), 2):
            tid = parts[idx]
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            
            chunk = parts[idx+1].split('<div class="footer-tweet"')[0]
            tm = re.search(r'dir="auto">(.*?)(?:<div class="main-tweet|<div class="tweet-media|<div class="footer-tweet|$)', chunk, re.DOTALL)
            raw_html = tm.group(1) if tm else chunk
            
            raw_html = re.sub(r'<script.*?</script>', '', raw_html, flags=re.DOTALL)
            raw_html = re.sub(r'<style.*?</style>', '', raw_html, flags=re.DOTALL)
            raw_html = raw_html.replace('<br>', '\n').replace('<br/>', '\n').replace('</p>', '\n\n')
            text = re.sub(r'<[^>]+>', '', raw_html).strip()
            text = html.unescape(text)
            
            if 'Missing some Tweet in this thread?' in text:
                text = text.split('Missing some Tweet in this thread?')[0].strip()
            if '• • •' in text:
                text = text.split('• • •')[0].strip()
                
            clean = _clean_thread_item(text)
            if clean and len(clean) > 3:
                trailing_tweets.append(clean)
        
        return trailing_tweets
    except Exception as e:
        print(f"⚠️ Thread unroll fetch failed: {e}")
        return []

def extract_twitter_media(url, dest_dir=None):
    """
    X/Twitter extractor via Native GraphQL + FxTwitter + syndication + Thread unroller.
    Returns photos and/or videos, plus full unrolled thread text chunks when thread/caption exists.
    """
    target_dir = dest_dir or DOWNLOADS_DIR
    status_id = parse_twitter_status_id(url)
    if not status_id:
        return None

    p_gql = _twitter_from_graphql(status_id)
    p_fx = _twitter_from_fxtwitter(status_id)
    p_syn = _twitter_from_syndication(status_id)
    payload = p_gql or p_fx or p_syn
    if not payload:
        return None

    # Merge un-truncated text from FxTwitter if GraphQL had truncated preview text
    if p_fx and len(p_fx.get("text") or "") > len(payload.get("text") or ""):
        payload["text"] = p_fx["text"]

    if not payload.get("videos") and p_fx and p_fx.get("videos"):
        payload["videos"] = p_fx["videos"]
    if not payload.get("photos") and p_fx and p_fx.get("photos"):
        payload["photos"] = p_fx["photos"]

    caption = html.unescape(payload.get("text") or "").strip()
    thread_texts = payload.get("thread_texts") or []
    photos = payload.get("photos") or []
    videos = payload.get("videos") or []
    author_name = payload.get("author_name") or ""
    author_handle = payload.get("author_handle") or ""

    # If GraphQL didn't fetch thread_texts, check ThreadReader fallback
    if not thread_texts:
        extra_items = _twitter_fetch_thread(status_id)
        if extra_items:
            thread_texts = [caption] + extra_items if caption else extra_items

    results = []
    if thread_texts and len(thread_texts) > 1:
        full_thread_text = "\n\n───────────────\n\n".join(thread_texts)
        header_prefix = f"🧵 <b>Thread by {html.escape(author_name)} (@{html.escape(author_handle)})</b>\n\n" if author_name and author_handle else "🧵 <b>X Thread</b>\n\n"

        for i, chunk in enumerate(split_telegram_text(full_thread_text, limit=3500)):
            safe = html.escape(chunk, quote=False)
            prefix = header_prefix if i == 0 else ""
            results.append(("text_only", f"{prefix}{safe}", False))
    else:
        # Single tweet delivery
        for chunk in split_telegram_text(caption):
            safe = html.escape(chunk, quote=False)
            results.append(("text_only", safe, False))

    os.makedirs(target_dir, exist_ok=True)
    short_cap = caption.split("\n")[0][:120] if caption else "X Post"

    # Download all photos in thread (capped to 10 for Telegram album limit)
    for i, photo_url in enumerate(photos[:10]):
        out_path = os.path.join(target_dir, f"x_{int(time.time()*1000)}_{i}.jpg")
        if _download_binary(photo_url, out_path, min_size=500):
            results.append((out_path, short_cap, False))

    # Download any videos
    for i, video_url in enumerate(videos):
        out_path = os.path.join(target_dir, f"x_{int(time.time()*1000)}_v{i}.mp4")
        if _download_binary(video_url, out_path, min_size=5000):
            results.append((out_path, short_cap, False))

    if results:
        return results
    return None

# -----------------------
# CORE MEDIA DOWNLOAD ENGINE (ISOLATED JOB DIRECTORIES)
# -----------------------

def get_js_runtime_config():
    cfg = {}
    qjs_termux = "/data/data/com.termux/files/usr/bin/qjs"
    node_termux = "/data/data/com.termux/files/usr/bin/node"
    if os.path.exists(qjs_termux):
        cfg["quickjs"] = {"path": qjs_termux}
    elif shutil.which("qjs"):
        cfg["quickjs"] = {"path": shutil.which("qjs")}

    if os.path.exists(node_termux):
        cfg["node"] = {"path": node_termux}
    elif shutil.which("node"):
        cfg["node"] = {"path": shutil.which("node")}
    return cfg

def build_ydl_opts(fmt, job_dir=None, is_playlist=False, progress_cb=None, job_id=None):
    target_dir = job_dir or DOWNLOADS_DIR
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": f"{target_dir}/%(title).80B.%(ext)s",
        "restrictfilenames": False,
        "windowsfilenames": True,
        "format": fmt,
        "overwrites": True,
        "merge_output_format": "mp4",
        "postprocessor_args": {
            "merger": ["-movflags", "+faststart"],
            "ffmpeg": ["-movflags", "+faststart"],
        },
        "noplaylist": not is_playlist,
        "socket_timeout": 15,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 64,
        "nocheckcertificate": True,
        "remote_components": ["ejs:github"],
        "js_runtimes": get_js_runtime_config(),
    }
    if is_playlist:
        opts["playlist_items"] = "1-15"
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE

    if progress_cb:
        last_edit = [0]
        def ydl_hook(d):
            if job_id and is_job_cancelled(job_id):
                raise DownloadCancelledException("Download cancelled by user.")
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                now = time.time()
                if total > 0 and now - last_edit[0] > 2.5:
                    last_edit[0] = now
                    pct = int((downloaded / total) * 100)
                    mb_dl = downloaded / (1024 * 1024)
                    mb_tot = total / (1024 * 1024)
                    speed = d.get("speed") or 0
                    mb_spd = speed / (1024 * 1024) if speed else 0
                    bar = '█' * (pct // 10) + '░' * (10 - (pct // 10))
                    progress_cb(f"📥 <b>Downloading from Source:</b>\n<code>[{bar}]</code> <b>{pct}%</b> ({mb_dl:.1f} / {mb_tot:.1f} MB • {mb_spd:.1f} MB/s)")
            elif status == "finished":
                now = time.time()
                if now - last_edit[0] > 1.5:
                    last_edit[0] = now
                    progress_cb("⚙️ <b>Processing & Merging High-Definition streams...</b>")

        opts["progress_hooks"] = [ydl_hook]
    return opts


def find_file(ydl, info):
    path = ydl.prepare_filename(info)
    if os.path.exists(path):
        return path
    for ext in (".mp4", ".m4a", ".mp3", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp"):
        alt = os.path.splitext(path)[0] + ext
        if os.path.exists(alt):
            return alt
    return None

def probe_playlist_info(url):
    """
    Probes playlist metadata in < 2 seconds using flat extraction.
    Returns title, item count, and playable status.
    """
    u_clean = sanitize_url(url)
    u_lower = u_clean.lower()
    is_playlist_url = ("/playlist" in u_lower or "list=pl" in u_lower or "list=ol" in u_lower or "list=uu" in u_lower or "/sets/" in u_lower)
    if not is_playlist_url:
        return None
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 8,
            "remote_components": ["ejs:github"],
            "js_runtimes": get_js_runtime_config(),
        }
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(u_clean, download=False)
            if info and (info.get("_type") == "playlist" or info.get("entries")):
                entries = info.get("entries") or []
                valid_entries = [e for e in entries if e]
                title = str(info.get("title") or "YouTube Playlist")
                return {
                    "is_playlist": True,
                    "title": title[:80],
                    "item_count": min(len(valid_entries), 20),
                    "total_count": len(valid_entries)
                }
    except Exception:
        pass
    return None

def extract_playlist_entries(url):
    """Extracts flat list of valid item URLs and title for pipelined playlist execution."""
    u_clean = sanitize_url(url)
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 10,
            "remote_components": ["ejs:github"],
            "js_runtimes": get_js_runtime_config(),
        }
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(u_clean, download=False)
            if not info:
                return "YouTube Playlist", []
            raw_entries = info.get("entries") or []
            valid_entries = []
            for e in raw_entries:
                if not e:
                    continue
                item_url = e.get("url") or (f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else None)
                if item_url and not str(item_url).startswith("http"):
                    item_url = f"https://www.youtube.com/watch?v={item_url}"
                if item_url:
                    valid_entries.append({"url": item_url, "title": e.get("title") or "Video Item"})
            title = str(info.get("title") or "YouTube Playlist")
            return title, valid_entries
    except Exception as e:
        print(f"⚠️ extract_playlist_entries error: {e}")
        return "YouTube Playlist", []


def probe_media_info(url):
    """
    Quickly probes remote media metadata in < 1 second.
    Accurately computes size using bitrates and duration.
    """
    u_clean = sanitize_url(url)

    u_lower = u_clean.lower()
    if "instagram.com/p/" in u_lower or "pinterest." in u_lower:
        return None
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "socket_timeout": 8,
            "remote_components": ["ejs:github"],
            "js_runtimes": get_js_runtime_config(),
        }
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(u_clean, download=False)
            if not info:
                return None

            title = str(info.get("title") or "Video Media")
            duration = info.get("duration") or 0

            formats = info.get("formats") or []
            filesize = 0

            # Size estimation targeting H.264 (avc1) ≤1080p — the exact codec
            # that merges natively into MP4 and streams inside Telegram's player.
            # VP9/AV1 streams are larger but can't play inline in Telegram.
            if formats:
                v_fmts = [f for f in formats if f.get("vcodec") != "none" and (f.get("height") or f.get("width"))]

                # Priority 1: H.264 (avc1) ≤1080p — native MP4/Telegram codec
                avc_fhd = [f for f in v_fmts
                           if "avc" in (f.get("vcodec") or "").lower()
                           and (f.get("height") or 0) <= 1080]
                if avc_fhd:
                    best_v = max(avc_fhd, key=lambda f: (
                        f.get("height") or 0,
                        f.get("filesize") or f.get("filesize_approx") or 0,
                        f.get("tbr") or 0))
                # Fallback: any codec ≤1080p (rare, non-YouTube sources)
                elif v_fmts:
                    fhd_v = [f for f in v_fmts if (f.get("height") or 0) <= 1080]
                    pool = fhd_v if fhd_v else v_fmts
                    best_v = max(pool, key=lambda f: (
                        f.get("height") or 0,
                        f.get("filesize") or f.get("filesize_approx") or 0,
                        f.get("tbr") or 0))
                else:
                    best_v = {}

                # Audio: prefer AAC (mp4a) for native MP4 muxing
                a_fmts = [f for f in formats if f.get("acodec") != "none"]
                aac_a = [f for f in a_fmts if "mp4a" in (f.get("acodec") or "").lower()]
                best_a = max(aac_a, key=lambda f: f.get("abr") or f.get("tbr") or 0) if aac_a else (
                    max(a_fmts, key=lambda f: f.get("abr") or f.get("tbr") or 0) if a_fmts else {})

                v_size = best_v.get("filesize") or best_v.get("filesize_approx")
                a_size = best_a.get("filesize") or best_a.get("filesize_approx")

                if not v_size and duration > 0 and (best_v.get("tbr") or best_v.get("vbr")):
                    v_size = int(((best_v.get("tbr") or best_v.get("vbr")) * 1000 / 8) * duration)
                if not a_size and duration > 0 and (best_a.get("tbr") or best_a.get("abr")):
                    a_size = int(((best_a.get("tbr") or best_a.get("abr")) * 1000 / 8) * duration)

                if v_size and a_size:
                    filesize = v_size + a_size
                elif v_size:
                    filesize = v_size
                elif a_size:
                    filesize = a_size
                elif info.get("filesize") or info.get("filesize_approx"):
                    filesize = info.get("filesize") or info.get("filesize_approx")
            else:
                filesize = info.get("filesize") or info.get("filesize_approx") or 0

            best_mb = int(filesize / (1024 * 1024)) if filesize else 0
            is_large = bool(best_mb > 50 or (duration and duration > 360))




            dur_str = ""
            if duration and isinstance(duration, (int, float)) and duration > 0:
                m = int(duration // 60)
                s = int(duration % 60)
                dur_str = f"{m}:{s:02d}"

            return {
                "title": title[:80],
                "duration_str": dur_str,
                "best_mb": best_mb,
                "is_large": is_large,
            }
    except Exception:
        return None


def download_media(url, progress_cb, force_audio=False, job_dir=None, max_bytes=None, quality_choice="best", job_id=None):
    """
    Downloads media from supported platforms into an isolated job directory.
    Concurrency safe: isolated job_dir prevents any race conditions across threads!
    """
    target_dir = job_dir or DOWNLOADS_DIR
    os.makedirs(target_dir, exist_ok=True)
    quota_limit = max_bytes or MAX_TELEGRAM_SIZE_BYTES

    if force_audio or quality_choice == "audio":
        formats = [("audio", "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best")]
    elif quality_choice == "balanced":
        # Balanced: Compressed / Fast delivery under 49MB (capped at 720p/480p)
        formats = [
            ("video", "bestvideo[height<=720][filesize_approx<43M]+bestaudio[filesize_approx<6M]/bestvideo[height<=720]+bestaudio/best[height<=720][filesize<49M]/best[height<=720]/bestvideo[height<=480]+bestaudio/best[height<=480]/best[filesize<49M]/best"),
            ("best", "best[filesize<49M]/best"),
        ]
    else:
        # Default / "best": H.264 (avc1) Full HD 1080p + AAC audio
        # This is the exact codec combo that:
        #   1. Merges natively into MP4 without re-encoding
        #   2. Streams inline in Telegram's in-app video player
        #   3. Matches the size shown in probe_media_info 1:1
        formats = [
            ("best_video", "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/bv*[vcodec^=avc1][height<=1080]+ba/bv*[vcodec^=avc][height<=1080]+ba/bestvideo[height<=1080]+bestaudio/best"),
        ]






    last_error = None
    u_lower = url.lower()
    is_instagram = "instagram.com" in u_lower
    is_pinterest = "pinterest.com" in u_lower or "pin.it" in u_lower or "pinimg.com" in u_lower
    is_facebook = "facebook.com" in u_lower or "fb.watch" in u_lower
    is_youtube_playlist = ("list=" in u_lower or "/playlist" in u_lower or "/sets/" in u_lower) and ("youtube.com" in u_lower or "youtu.be" in u_lower)

    # Always resolve redirects for Facebook share links (including /share/r/, /share/p/, /share/v/)
    if is_facebook and ("/share/" in u_lower or "fb.watch" in u_lower):
        try:
            r = get_auth_session().head(url, allow_redirects=True, timeout=8)
            if r.url and r.url != url:
                url = r.url
                u_lower = url.lower()
        except Exception:
            try:
                r = requests.head(url, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True, timeout=8)
                if r.url and r.url != url:
                    url = r.url
                    u_lower = url.lower()
            except Exception:
                pass

    is_facebook_reel = is_facebook and ("/reel/" in u_lower or "/reels/" in u_lower or "/watch" in u_lower or "/videos/" in u_lower or "/video.php" in u_lower)
    is_facebook_photo = is_facebook and not is_facebook_reel and (
        "/p/" in u_lower or "/photo" in u_lower or "/posts/" in u_lower
        or "story_fbid=" in u_lower or "permalink.php" in u_lower or "/media/set" in u_lower
    )
    is_instagram_photo = is_instagram and not ("/reel/" in u_lower or "/reels/" in u_lower or "/share/r/" in u_lower) and (
        "/p/" in u_lower or "/photo" in u_lower or "/posts/" in u_lower or "/share/p/" in u_lower
    )
    is_twitter = "twitter.com" in u_lower or "x.com" in u_lower
    is_threads = "threads.net" in u_lower or "threads.com" in u_lower

    # High-Speed Direct Scraper for Threads
    if is_threads and not force_audio:
        progress_cb("🧵 Fetching Threads post content...")
        th_files = extract_threads_media(url, target_dir)
        if th_files:
            return th_files

    # High-Speed Direct Scraper for Pinterest
    if is_pinterest and not force_audio:
        progress_cb("📌 Fetching Pinterest HD media...")
        pin_files = extract_pinterest_media(url, target_dir)
        if pin_files:
            return pin_files

    # Direct route for Instagram Photo/Carousel posts
    if is_instagram_photo and not force_audio:
        progress_cb("📸 Fetching Instagram photo(s) & album...")
        ig_files = extract_instagram_media(url, target_dir)
        if ig_files:
            return ig_files

    # X/Twitter: prefer FxTwitter/syndication first (photo+text, video, mixed, text-only).
    if is_twitter and not force_audio:
        progress_cb("🐦 Fetching Twitter / X post content...")
        x_files = extract_twitter_media(url, target_dir)
        if x_files:
            return x_files

    # Direct route for Facebook Photo/Album/Mixed posts (NOT reels).
    if is_facebook_photo and not force_audio:
        progress_cb("📸 Fetching Facebook photo(s)...")
        fb_files = extract_facebook_media(url, target_dir)
        if fb_files:
            return fb_files
        raise Exception(
            "Could not extract this Facebook photo post. It may be private, deleted, or region-locked."
        )

    for mode, fmt in formats:
        try:
            if is_youtube_playlist:
                progress_cb(f"📋 Downloading YouTube Playlist items (Audio: {force_audio})...")
            elif force_audio or mode == "audio":
                progress_cb("🎵 Extracting high-quality Audio stream...")
            elif mode == "best_video":
                progress_cb("⚡ Fetching best original quality stream...")
            else:
                progress_cb("⚡ Adapting optimized stream to fit within 49MB...")

            with yt_dlp.YoutubeDL(build_ydl_opts(fmt, target_dir, is_playlist=is_youtube_playlist, progress_cb=progress_cb, job_id=job_id)) as ydl:


                info = ydl.extract_info(url, download=True)
                entries = info.get("entries") if (info and info.get("_type") == "playlist") else [info]
                files = []
                for entry in entries:
                    if not entry:
                        continue
                    fpath = find_file(ydl, entry)
                    if fpath and os.path.exists(fpath):
                        ent_title = entry.get("title") or entry.get("fulltitle") or ""
                        # If title is numeric, generic ID, or ugly view/reaction stats, try description or uploader
                        if not ent_title or str(ent_title).isdigit() or re.match(r'^(?:verified account\s*)?\[?[0-9]+\]?$', str(ent_title), re.IGNORECASE) or is_ugly_stats_string(ent_title):
                            desc = entry.get("description") or ""
                            clean_desc = None
                            if desc:
                                for line in desc.split("\n"):
                                    line_c = re.sub(r'#\S+', '', line).strip()
                                    if len(line_c) > 3 and not is_ugly_stats_string(line_c):
                                        clean_desc = line_c[:80].strip()
                                        break
                            uploader = entry.get("uploader") or entry.get("channel") or entry.get("uploader_id")
                            if clean_desc:
                                ent_title = clean_desc
                            elif uploader:
                                ent_title = f"Reel by @{uploader}"
                            else:
                                ent_title = "Video"
                        else:
                            # Clean any stats prefix if title has '179K views · 213K reactions | Actual title'
                            ent_title = clean_media_title(ent_title, default="Video")

                        is_aud = force_audio or mode == "audio"
                        files.append((fpath, ent_title, is_aud))

                if files:
                    ok_files = [(f, t, is_aud) for f, t, is_aud in files if os.path.getsize(f) <= MAX_TELEGRAM_SIZE_BYTES]
                    if ok_files:
                        return ok_files
                    progress_cb("⚠️ File exceeded size limit, adapting format...")
                    continue

        except DownloadCancelledException:
            raise
        except Exception as e:
            if (job_id and is_job_cancelled(job_id)) or "cancelled" in str(e).lower():
                raise DownloadCancelledException("Download cancelled by user.")
            last_error = str(e)
            if "no video" in last_error.lower() or any(x in last_error for x in ["Sign in", "LOGIN_REQUIRED", "Private video", "Cannot parse data", "members only", "registered users", "only available"]):
                break
            continue


    if not force_audio:
        if is_twitter:
            progress_cb("🐦 Fetching Twitter / X post content...")
            x_files = extract_twitter_media(url, target_dir)
            if x_files:
                return x_files

        if is_instagram:
            progress_cb("📸 Fetching high-resolution Instagram photo(s)...")
            ig_files = extract_instagram_media(url, target_dir)
            if ig_files:
                return ig_files

        if is_facebook:
            progress_cb("📸 Fetching high-resolution Facebook photo(s)...")
            fb_files = extract_facebook_media(url, target_dir)
            if fb_files:
                return fb_files

    raise Exception(last_error or "Download failed completely.")

def cleanup_old_downloads(max_age_seconds=900):
    """Safely cleans only orphaned temporary folders older than 15 minutes without touching active downloads."""
    if not os.path.exists(DOWNLOADS_DIR):
        return
    now = time.time()
    try:
        for entry in os.listdir(DOWNLOADS_DIR):
            epath = os.path.join(DOWNLOADS_DIR, entry)
            try:
                mtime = os.path.getmtime(epath)
                if now - mtime > max_age_seconds:
                    if os.path.isdir(epath):
                        shutil.rmtree(epath, ignore_errors=True)
                    elif os.path.isfile(epath):
                        os.remove(epath)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Error cleaning old downloads: {e}")
