import os
import re
import time
import json
import uuid
import shutil
import traceback
from datetime import datetime

from config import (
    BOT_TOKEN, OWNER_ID, UPI_ID, ADMIN_HANDLE, SUBSCRIPTION_PRICE_INR,
    FREE_TRIAL_LIMIT, MAX_DOWNLOADS_PER_HOUR, DOWNLOADS_DIR, DOWNLOAD_EXECUTOR,
    FAST_LANE_MAX_BYTES, PAID_HEAVY_MAX_BYTES, OWNER_MAX_BYTES, FREE_TRIAL_MAX_BYTES,
    COOKIES_FILE, USERS_FILE
)
from telegram_api import (
    tg, send_msg, send_photo_url, delete_msg, edit_msg,
    escape_html, REMOVE_KEYBOARD, TG_SESSION
)
from users import (
    load_users, save_users, is_user_active, add_or_renew_subscription,
    get_upi_qr_url, invalidate_users_cache, send_backup_to_owner, record_user_download,
    ban_user, unban_user, is_user_banned, get_banned_users, save_json, load_json
)

from downloader import (
    download_media, extract_urls, is_safe_url, detect_platform,
    clean_media_title, format_caption, format_clean_audio_filename,
    format_clean_video_filename, generate_video_thumbnail, get_video_metadata,
    get_js_runtime_config, probe_media_info, probe_playlist_info, extract_playlist_entries,
    mark_job_cancelled, is_job_cancelled, clear_job_cancelled, DownloadCancelledException
)




from health import get_server_metrics
from mtproto_uploader import mtproto_uploader
from queue_manager import queue_manager
from cookie_refresher import (
    analyze_cookies_file, refresh_visitor_tokens,
    check_platform_cookies_health, check_cookies_health_and_alert
)




# -----------------------
# RATE LIMITING & CHANNEL DEDUPLICATION
# -----------------------

USER_DOWNLOAD_HISTORY = {}
PENDING_MEDIA_REQUESTS = {}
CHANNEL_SEEN_POSTS_FILE = os.path.join(os.path.dirname(__file__), "channel_seen_posts.json")

_channel_seen_posts = {}
_channel_seen_loaded = False

def load_channel_seen_posts():
    global _channel_seen_posts, _channel_seen_loaded
    if _channel_seen_loaded:
        return _channel_seen_posts
    loaded = load_json(CHANNEL_SEEN_POSTS_FILE)
    if isinstance(loaded, dict):
        _channel_seen_posts = loaded
    else:
        _channel_seen_posts = {}
    _channel_seen_loaded = True
    return _channel_seen_posts

def save_channel_seen_posts():
    global _channel_seen_posts
    try:
        save_json(CHANNEL_SEEN_POSTS_FILE, _channel_seen_posts)
    except Exception as e:
        print(f"⚠️ Error saving channel seen posts: {e}")

def normalize_channel_headline(text: str) -> str:
    """Extract canonical normalized headline from channel post text or caption."""
    if not text:
        return ""
    # Strip HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    
    # If line 1 is only hashtags (e.g. #Market #ET #Companies), use line 2
    headline = lines[0]
    if all(word.startswith("#") for word in headline.split()) and len(lines) > 1:
        headline = lines[1]
    
    # Strip URLs and hashtags
    headline = re.sub(r"https?://\S+", "", headline)
    headline = re.sub(r"#[A-Za-z0-9_]+", "", headline)
    # Strip punctuation & symbols (keep letters, numbers, spaces)
    headline = re.sub(r"[^\w\s]", " ", headline)
    return " ".join(headline.lower().split()).strip()

def check_and_record_channel_post(chat_id, msg_id, text: str) -> bool:
    """
    Returns True if post is a duplicate (and should be deleted).
    Returns False if post is fresh.
    """
    global _channel_seen_posts
    load_channel_seen_posts()
    norm = normalize_channel_headline(text)
    if not norm or len(norm) < 15:
        return False
    
    now = time.time()
    # Prune posts older than 48 hours (172800 seconds)
    cutoff = now - 172800
    expired = [k for k, v in _channel_seen_posts.items() if isinstance(v, dict) and v.get("ts", 0) < cutoff]
    for k in expired:
        del _channel_seen_posts[k]
    
    if norm in _channel_seen_posts:
        prev_info = _channel_seen_posts[norm]
        if isinstance(prev_info, dict) and prev_info.get("msg_id") == msg_id:
            return False
        return True
    
    _channel_seen_posts[norm] = {"msg_id": msg_id, "ts": now, "chat_id": chat_id}
    save_channel_seen_posts()
    return False

def check_rate_limit(user_id, is_owner=False):
    """
    Two-stage anti-spam & flood protection:
    1. Rapid burst protection: Max 4 requests in 15 seconds.
    2. Hourly limit: MAX_DOWNLOADS_PER_HOUR requests per 60 minutes.
    Admin / Owner is 100% unrestricted.
    """
    if is_owner or str(user_id) == str(OWNER_ID):
        return True, 0, None

    now = time.time()
    uid = str(user_id)
    history = USER_DOWNLOAD_HISTORY.get(uid, [])
    # Keep only timestamps in the last 3600s
    history = [t for t in history if now - t < 3600]
    USER_DOWNLOAD_HISTORY[uid] = history

    # 1. Burst flood check (more than 4 requests in last 15s)
    recent_burst = [t for t in history if now - t < 15]
    if len(recent_burst) >= 4:
        return False, 1, "burst"

    # 2. Hourly limit check
    if len(history) >= MAX_DOWNLOADS_PER_HOUR:
        oldest = history[0]
        wait_mins = max(1, int((3600 - (now - oldest)) / 60))
        return False, wait_mins, "hourly"

    history.append(now)
    USER_DOWNLOAD_HISTORY[uid] = history
    return True, 0, None


# -----------------------
# CONCURRENT MEDIA PROCESSING (ISOLATED JOB DIRECTORIES)
# -----------------------

def process_media_job(url, chat_id, user_id=None, force_audio=False, quality_choice="best", existing_progress_msg_id=None, custom_job_id=None):
    """
    Executes media downloading and uploads result to Telegram.
    Each job operates in an isolated temporary directory to prevent concurrency collisions!
    """
    job_id = custom_job_id or uuid.uuid4().hex[:10]
    job_dir = os.path.join(DOWNLOADS_DIR, f"job_{job_id}")
    os.makedirs(job_dir, exist_ok=True)

    progress_msg_id = existing_progress_msg_id
    action_type = "Audio" if force_audio else "Media"
    platform = detect_platform(url)

    stop_kb = json.dumps({
        "inline_keyboard": [
            [{"text": "🛑 Stop / Cancel Download", "callback_data": f"stop_{job_id}"}]
        ]
    })

    if not progress_msg_id:
        try:
            p_resp = send_msg(
                chat_id,
                f"⚡ <b>Processing {platform} {action_type}...</b>\n_Connecting to ultra-fast dedicated pipeline..._",
                reply_markup=stop_kb,
                parse_mode="HTML"
            )
            if p_resp.ok:
                progress_msg_id = p_resp.json().get("result", {}).get("message_id")
        except Exception:
            pass

    def update_progress(text):
        if not progress_msg_id or is_job_cancelled(job_id):
            return
        try:
            tg(
                "editMessageText",
                data={
                    "chat_id": chat_id,
                    "message_id": progress_msg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": stop_kb
                },
                timeout=5
            )
        except Exception:
            pass

    files = []
    try:
        if is_job_cancelled(job_id):
            raise DownloadCancelledException("Download cancelled by user.")

        # Check user quota and lane
        if user_id:
            is_active, role, days_left, name, trials_left = is_user_active(user_id)
            is_paid = role in ("owner", "subscriber")
        else:
            is_paid = False

        user_quota_bytes = queue_manager.get_user_quota_bytes(user_id or chat_id, is_paid)

        t_down_start = time.time()
        print(f"🎬 [Chat {chat_id}] Processing {platform} link (Job: {job_id}): {url[:70]} (AudioMode: {force_audio}, Quality: {quality_choice}, Quota: {user_quota_bytes // (1024*1024)}MB)")
        files = download_media(url, update_progress, force_audio=force_audio, job_dir=job_dir, max_bytes=user_quota_bytes, quality_choice=quality_choice, job_id=job_id)
        t_down = time.time() - t_down_start

        if is_job_cancelled(job_id):
            raise DownloadCancelledException("Download cancelled by user.")

        if not files:
            raise Exception("No media formats could be extracted from this link.")

        # Separate text messages from media files
        text_items = [f for f in files if f[0] == "text_only"]
        media_items = [f for f in files if f[0] != "text_only" and os.path.exists(f[0])]

        total_bytes = sum(os.path.getsize(f[0]) for f in media_items if os.path.exists(f[0]))
        if total_bytes > user_quota_bytes:
            mb_tot = total_bytes // (1024 * 1024)
            mb_max = user_quota_bytes // (1024 * 1024)
            delete_msg(chat_id, progress_msg_id)
            send_msg(chat_id, f"⚠️ <b>File Size Limit ({mb_tot} MB)</b>\n\nYour current plan allows up to {mb_max} MB per file.\nUpgrade to Premium for up to 4 GB transfers! Contact {ADMIN_HANDLE}", parse_mode="HTML")
            return

        # Heavy lane serialization (> 50 MB) to keep phone server healthy
        is_heavy = total_bytes > FAST_LANE_MAX_BYTES
        if is_heavy:
            def notify_heavy_pos(pos):
                update_progress(f"⏳ <b>Queued for Heavy Transfer (Position #{pos})</b>\n<i>Waiting for active transfer to finish to keep phone server healthy...</i>")
            queue_manager.acquire_heavy_slot(user_id or chat_id, job_id, notify_cb=notify_heavy_pos)

        # Upload phase
        update_progress("📤 <b>Uploading in Original Quality...</b>")
        t_up_start = time.time()
        sent = 0

        # 1. Deliver any standalone text chunks first (e.g. unrolled Twitter/X threads)
        for _, t_content, _ in text_items:
            r = send_msg(chat_id, t_content, parse_mode="HTML")
            if r.ok:
                sent += 1

        def _deliver_single_item(item):
            nonlocal sent
            if is_job_cancelled(job_id):
                raise DownloadCancelledException("Download cancelled by user.")

            file_path = item[0]
            title = item[1] if len(item) > 1 else "Media"
            is_audio_fallback = item[2] if len(item) > 2 else False

            if not os.path.exists(file_path):
                return

            ext = os.path.splitext(file_path)[1].lower()
            caption_text = format_caption(title, file_path, is_audio_fallback=is_audio_fallback, is_explicit_audio=force_audio)
            data_dict = {"chat_id": chat_id, "parse_mode": "HTML"}
            if caption_text:
                data_dict["caption"] = caption_text

            fsize = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            thumb_file = generate_video_thumbnail(file_path) if ext in (".mp4", ".mov", ".mkv", ".webm") else None
            clean_vname = format_clean_video_filename(title, ext=ext)
            v_meta = get_video_metadata(file_path) if ext in (".mp4", ".mov", ".mkv", ".webm") else {}

            if fsize >= 49 * 1024 * 1024:
                # Large Heavy Transfer (>= 50 MB up to 4 GB): Route via MTProto
                if fsize > 2000 * 1024 * 1024 and not mtproto_uploader.has_premium_user():
                    delete_msg(chat_id, progress_msg_id)
                    send_msg(
                        chat_id,
                        f"⚠️ <b>Large File Delivery Notice ({fsize // (1024*1024)} MB)</b>\n\n"
                        f"Files exceeding 2 GB require a valid Telegram Premium user session (up to 4 GB).\n\n"
                        f"💡 Please configure <code>USER_SESSION_STRING</code> or contact {ADMIN_HANDLE}.",
                        parse_mode="HTML"
                    )
                elif mtproto_uploader.is_available():
                    if ext in (".mp3", ".m4a", ".wav", ".ogg") or is_audio_fallback or force_audio:
                        clean_name = clean_media_title(title, default="Audio Track")
                        ok = mtproto_uploader.send_audio_sync(
                            chat_id, file_path, caption=caption_text or "",
                            title=clean_name[:64], performer="Eternal Automation",
                            progress_msg_id=progress_msg_id, tg_edit_func=edit_msg
                        )
                        if ok: sent += 1
                    else:
                        ok = mtproto_uploader.send_video_sync(
                            chat_id, file_path,
                            caption=caption_text or "",
                            duration=v_meta.get("duration"),
                            width=v_meta.get("width"),
                            height=v_meta.get("height"),
                            thumb_path=thumb_file,
                            progress_msg_id=progress_msg_id,
                            tg_edit_func=edit_msg
                        )
                        if ok: sent += 1
                else:
                    print(f"⚠️ MTProto unavailable for large file {file_path} ({fsize // (1024*1024)}MB)")
                    delete_msg(chat_id, progress_msg_id)
                    send_msg(
                        chat_id,
                        f"⚠️ <b>Large File Delivery Notice ({fsize // (1024*1024)} MB)</b>\n\n"
                        f"Telegram's MTProto high-speed gateway is currently completing a brief cooldown.\n\n"
                        f"💡 <b>Tip:</b> Choose <b>⚖️ Balanced Quality (< 50 MB)</b> or <b>🎵 MP3 Audio</b> for immediate delivery right now!",
                        parse_mode="HTML"
                    )
            else:
                # Fast Standard Transfer (< 50 MB): Follow the classic HTTP Bot API
                with open(file_path, "rb") as f:
                    if ext in (".jpg", ".jpeg", ".png", ".webp"):
                        resp = tg("sendPhoto", data=data_dict, files={"photo": (os.path.basename(file_path), f, "image/jpeg")}, timeout=120)
                        if not resp.ok and "IMAGE_PROCESS_FAILED" in resp.text:
                            f.seek(0)
                            resp = tg("sendDocument", data=data_dict, files={"document": (os.path.basename(file_path), f, "application/octet-stream")}, timeout=120)
                    elif ext in (".mp3", ".m4a", ".wav", ".ogg") or is_audio_fallback or force_audio:
                        clean_name = clean_media_title(title, default="Audio Track")
                        audio_filename = format_clean_audio_filename(clean_name, ext=".mp3" if ext in (".mp3", ".m4a") else ext)
                        data_dict["title"] = clean_name[:64]
                        data_dict["performer"] = "Eternal Automation"
                        resp = tg(
                            "sendAudio",
                            data=data_dict,
                            files={"audio": (audio_filename, f, "audio/mpeg" if ext == ".mp3" else "audio/mp4")},
                            timeout=120
                        )
                    else:
                        clean_vname = format_clean_video_filename(title, ext=ext)
                        data_dict["supports_streaming"] = True
                        if v_meta.get("duration"): data_dict["duration"] = v_meta["duration"]
                        if v_meta.get("width"): data_dict["width"] = v_meta["width"]
                        if v_meta.get("height"): data_dict["height"] = v_meta["height"]
                        files_payload = {"video": (clean_vname, f, "video/mp4")}
                        thumb_fh = None
                        if thumb_file and os.path.exists(thumb_file):
                            try:
                                thumb_fh = open(thumb_file, "rb")
                                files_payload["thumb"] = ("thumb.jpg", thumb_fh, "image/jpeg")
                            except Exception:
                                pass
                        try:
                            resp = tg("sendVideo", data=data_dict, files=files_payload, timeout=120)
                        finally:
                            if thumb_fh:
                                try: thumb_fh.close()
                                except Exception: pass

                if resp.ok:
                    sent += 1
                else:
                    print(f"⚠️ Failed to send media to {chat_id}: {resp.status_code} {resp.text}")

        # 2. Check if media qualifies for Telegram grouped album delivery (2 or more items, all < 49MB)
        can_group = (
            len(media_items) > 1 and
            not force_audio and
            all(
                os.path.splitext(f[0])[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv") and
                not f[2] and
                (os.path.getsize(f[0]) < 49 * 1024 * 1024 if os.path.exists(f[0]) else True)
                for f in media_items
            )
        )

        if can_group:
            chunks = [media_items[i:i + 10] for i in range(0, len(media_items), 10)]
            for chunk in chunks:
                if is_job_cancelled(job_id):
                    raise DownloadCancelledException("Download cancelled by user.")
                media_list = []
                files_dict = {}
                open_handles = []

                for idx, (fpath, title, _) in enumerate(chunk):
                    ext = os.path.splitext(fpath)[1].lower()
                    mtype = "photo" if ext in (".jpg", ".jpeg", ".png", ".webp") else "video"
                    attach_key = f"file_{idx}"
                    fh = open(fpath, "rb")
                    open_handles.append(fh)

                    mime_type = "image/jpeg"
                    if ext == ".png":
                        mime_type = "image/png"
                    elif ext == ".webp":
                        mime_type = "image/webp"
                    elif ext in (".mp4", ".mov", ".mkv"):
                        mime_type = "video/mp4"

                    clean_fname = format_clean_video_filename(title, ext=ext) if mtype == "video" else os.path.basename(fpath)
                    files_dict[attach_key] = (clean_fname, fh, mime_type)

                    item_obj = {"type": mtype, "media": f"attach://{attach_key}"}
                    if idx == 0:
                        cap = format_caption(title, fpath)
                        if cap:
                            item_obj["caption"] = cap
                            item_obj["parse_mode"] = "HTML"
                    media_list.append(item_obj)

                try:
                    if media_list:
                        resp = tg(
                            "sendMediaGroup",
                            data={"chat_id": chat_id, "media": json.dumps(media_list)},
                            files=files_dict,
                            timeout=120
                        )
                        if resp.ok:
                            sent += len(media_list)
                        else:
                            print(f"⚠️ sendMediaGroup failed ({resp.status_code} {resp.text}). Retrying without caption...")
                            for item in media_list:
                                item.pop("caption", None)
                                item.pop("parse_mode", None)
                            for fh in open_handles:
                                fh.seek(0)

                            resp2 = tg(
                                "sendMediaGroup",
                                data={"chat_id": chat_id, "media": json.dumps(media_list)},
                                files=files_dict,
                                timeout=120
                            )
                            if resp2.ok:
                                sent += len(media_list)
                            else:
                                print(f"⚠️ sendMediaGroup retry failed ({resp2.status_code} {resp2.text}). Falling back to individual delivery...")
                                for fh in open_handles:
                                    try: fh.close()
                                    except Exception: pass
                                open_handles.clear()
                                for item in chunk:
                                    _deliver_single_item(item)
                finally:
                    for fh in open_handles:
                        try: fh.close()
                        except Exception: pass
        else:
            # Single item or audio media delivery
            for item in media_items:
                _deliver_single_item(item)

        t_up = time.time() - t_up_start
        delete_msg(chat_id, progress_msg_id)
        print(f"✅ [Chat {chat_id}] Delivered {sent} media file(s) in {t_down + t_up:.1f}s (Download: {t_down:.1f}s, Upload: {t_up:.1f}s)")

        # 📊 Update user download metrics & monthly free quota tracker
        if user_id and sent > 0:
            try:
                record_user_download(user_id, file_size_bytes=total_bytes)
                is_active_now, rle, d_left, nm, t_left = is_user_active(user_id)
                if rle == "trial":
                    users_now = load_users()
                    u_data = users_now.get(str(user_id), {})
                    used_mb = u_data.get("free_bytes_used", 0) // (1024 * 1024)
                    rem_mb = max(0, 1024 - used_mb)
                    if is_active_now and t_left > 0 and rem_mb > 0:
                        send_msg(
                            chat_id,
                            f"🎁 <b>Free Monthly Plan:</b> You have <b>*{t_left}/10 downloads*</b> and <b>*{rem_mb} MB / 1,024 MB*</b> left this month!\n\n"
                            f"⚡ Loved the speed? Unlock <b>*Unlimited High-Speed Access*</b> 24/7 for just ₹{SUBSCRIPTION_PRICE_INR}/month with /renew!",
                            parse_mode="HTML"
                        )
                    else:
                        send_msg(
                            chat_id,
                            f"⚡ <b>You've reached your Free Monthly Limit (10 Videos or 1 GB Bandwidth)!</b>\n\n"
                            f"To keep saving high-speed videos, music & photos 24/7 with zero ads, unlock <b>*Unlimited Access for just ₹{SUBSCRIPTION_PRICE_INR} / Month*</b>!\n\n"
                            f"👇 Type <code>/renew</code> to get your instant UPI payment QR code!",
                            parse_mode="HTML"
                        )

            except Exception as e:
                print(f"⚠️ Error updating download metrics for user {user_id}: {e}")

    except DownloadCancelledException:
        print(f"🛑 [Chat {chat_id}] Job {job_id} cancelled by user.")
    except Exception as e:
        if is_job_cancelled(job_id) or "cancelled" in str(e).lower():
            print(f"🛑 [Chat {chat_id}] Job {job_id} cancelled cleanly: {e}")
            return
        delete_msg(chat_id, progress_msg_id)
        print(f"❌ Error processing media {url}: {e}")
        send_msg(
            chat_id,
            f"❌ <b>Download Error</b>\n\n"
            f"Could not download media from this link.\n"
            f"💡 <b>Details:</b> <code>{escape_html(str(e)[:120])}</code>\n\n"
            f"Please verify the post is public and try again.",
            parse_mode="HTML"
        )

    finally:
        clear_job_cancelled(job_id)
        queue_manager.release_heavy_slot(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)

def process_playlist_job(url, chat_id, user_id=None, force_audio=False, quality_choice="best", existing_progress_msg_id=None, custom_job_id=None):
    """
    Pipelined playlist processor:
    Extracts item list, downloads ONE item at a time, immediately uploads to Telegram,
    and immediately purges the item from disk before fetching the next item!
    Keeps phone storage at near 0 MB and provides real-time streaming progress to the user.
    """
    job_id = custom_job_id or uuid.uuid4().hex[:10]
    job_dir = os.path.join(DOWNLOADS_DIR, f"job_{job_id}")
    os.makedirs(job_dir, exist_ok=True)
    progress_msg_id = existing_progress_msg_id

    stop_kb = json.dumps({
        "inline_keyboard": [
            [{"text": "🛑 Stop / Cancel Download", "callback_data": f"stop_{job_id}"}]
        ]
    })

    def update_progress(text):
        if not progress_msg_id or is_job_cancelled(job_id):
            return
        try:
            tg(
                "editMessageText",
                data={
                    "chat_id": chat_id,
                    "message_id": progress_msg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": stop_kb
                },
                timeout=5
            )
        except Exception:
            pass

    try:
        if is_job_cancelled(job_id):
            raise DownloadCancelledException("Playlist download cancelled.")

        # 1. Fetch playlist items in flat mode (< 1s)
        pl_title, raw_entries = extract_playlist_entries(url)
        if not raw_entries:
            raise Exception("No playable items found in this playlist.")

        # User quota check and safe batch limiting
        if user_id:
            is_active, role, days_left, name, trials_left = is_user_active(user_id)
            is_paid = role in ("owner", "subscriber")
        else:
            is_active, role, days_left, name, trials_left = True, "trial", 0, "Guest", 10
            is_paid = False

        if not is_paid and str(user_id) != OWNER_ID and trials_left <= 0:
            delete_msg(chat_id, progress_msg_id)
            send_msg(
                chat_id,
                f"⚡ <b>Free Plan Quota Exceeded</b>\n\n"
                f"You've completed your 10 free downloads for this month!\n"
                f"To download playlists (up to 30 items) with 24/7 unlimited access, unlock <b>*Premium for ₹{SUBSCRIPTION_PRICE_INR}/month*</b>!\n\n"
                f"👇 Type <code>/renew</code> for your instant UPI QR code!",
                parse_mode="HTML"
            )
            return

        batch_limit = queue_manager.get_playlist_item_limit(user_id, role, trials_left)
        total_items = min(len(raw_entries), batch_limit)
        entries = raw_entries[:total_items]
        user_quota_bytes = queue_manager.get_user_quota_bytes(user_id or chat_id, is_paid)

        # 2. Acquire dedicated Playlist Lane slot to protect phone server health
        def notify_pl_pos(pos):
            update_progress(f"⏳ <b>Queued in Playlist Lane (Position #{pos})</b>\n<i>Waiting for active batch transfer to finish to keep phone server cool & fast...</i>")
        queue_manager.acquire_playlist_slot(user_id or chat_id, job_id, notify_cb=notify_pl_pos)

        update_progress(f"📋 <b>Starting Playlist Delivery (0/{total_items})</b>\n📌 <i>{escape_html(pl_title[:60])}</i>\n_Stream-and-upload pipeline active..._")

        delivered_count = 0
        for idx, entry in enumerate(entries, 1):
            if is_job_cancelled(job_id):
                raise DownloadCancelledException("Playlist download cancelled.")

            item_url = entry.get("url") or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else None)
            if not item_url:
                continue
            if not str(item_url).startswith("http"):
                item_url = f"https://www.youtube.com/watch?v={item_url}"

            item_title = entry.get("title") or f"Item {idx}"

            # Ensure phone storage has >= 500MB free space before writing
            queue_manager.ensure_storage_available(min_free_mb=500)

            # Download single item
            update_progress(
                f"📥 <b>Downloading Item {idx}/{total_items}</b>\n"
                f"🎵 <i>{escape_html(item_title[:50])}</i>\n"
                f"⏳ <i>Stream-and-upload pipeline active...</i>"
            )

            # Single item download with item progress
            def item_progress_cb(d_msg):
                pl_pct = int(((idx - 1) / total_items) * 100)
                pl_bar = '█' * (pl_pct // 10) + '░' * (10 - (pl_pct // 10))
                full_card = (
                    f"📋 <b>Playlist Progress ({idx}/{total_items}):</b>\n"
                    f"<code>[{pl_bar}]</code> <b>{pl_pct}%</b>\n"
                    f"🎵 <b>Current:</b> <i>{escape_html(item_title[:45])}</i>\n\n"
                    f"{d_msg}"
                )
                update_progress(full_card)

            item_files = download_media(
                item_url,
                item_progress_cb,
                force_audio=force_audio,
                job_dir=job_dir,
                max_bytes=user_quota_bytes,
                quality_choice=quality_choice,
                job_id=job_id
            )

            if is_job_cancelled(job_id):
                raise DownloadCancelledException("Playlist download cancelled.")

            if not item_files:
                continue

            # Immediately upload downloaded item
            for fpath, title, is_aud in item_files:
                if fpath == "text_only" or not os.path.exists(fpath):
                    continue

                fsize = os.path.getsize(fpath)
                f_mb = fsize / (1024 * 1024)
                ext = os.path.splitext(fpath)[1].lower()
                thumb_file = generate_video_thumbnail(fpath) if ext in (".mp4", ".mov", ".mkv", ".webm") else None
                v_meta = get_video_metadata(fpath) if ext in (".mp4", ".mov", ".mkv", ".webm") else {}

                pl_pct = int(((idx - 0.5) / total_items) * 100)
                pl_bar = '█' * (pl_pct // 10) + '░' * (10 - (pl_pct // 10))
                delivery_text = (
                    f"📋 <b>Playlist Progress ({idx}/{total_items}):</b>\n"
                    f"<code>[{pl_bar}]</code> <b>{pl_pct}%</b>\n"
                    f"🎬 <b>Current:</b> <i>{escape_html(title or item_title)[:45]}</i>\n\n"
                    f"📤 <b>Delivering to Telegram ({f_mb:.1f} MB):</b>\n"
                    f"<code>[████████░░]</code> <b>Uploading in Original Quality...</b>"
                )
                update_progress(delivery_text)

                sent_ok = False
                caption_text = format_caption(title or item_title, fpath, is_explicit_audio=is_aud or force_audio)

                # Send via MTProto if >= 50MB, else HTTP Bot API
                if fsize >= 49 * 1024 * 1024 and mtproto_uploader.is_available():
                    if ext in (".mp3", ".m4a", ".wav", ".ogg") or is_aud or force_audio:
                        clean_name = clean_media_title(title or item_title, default="Audio Track")
                        sent_ok = mtproto_uploader.send_audio_sync(
                            chat_id, fpath, caption=caption_text or "", title=clean_name[:64],
                            performer="Eternal Automation", progress_msg_id=progress_msg_id, tg_edit_func=edit_msg
                        )
                    else:
                        sent_ok = mtproto_uploader.send_video_sync(
                            chat_id, fpath, caption=caption_text or "",
                            duration=v_meta.get("duration"),
                            width=v_meta.get("width"),
                            height=v_meta.get("height"),
                            thumb_path=thumb_file,
                            progress_msg_id=progress_msg_id,
                            tg_edit_func=edit_msg
                        )
                else:
                    data_dict = {"chat_id": chat_id, "parse_mode": "HTML"}
                    if caption_text:
                        data_dict["caption"] = caption_text

                    with open(fpath, "rb") as f:
                        if ext in (".mp3", ".m4a", ".wav", ".ogg") or is_aud or force_audio:
                            clean_name = clean_media_title(title or item_title, default="Audio Track")
                            audio_fname = format_clean_audio_filename(clean_name, ext=".mp3" if ext in (".mp3", ".m4a") else ext)
                            data_dict["title"] = clean_name[:64]
                            data_dict["performer"] = "Eternal Automation"
                            resp = tg("sendAudio", data=data_dict, files={"audio": (audio_fname, f, "audio/mpeg" if ext == ".mp3" else "audio/mp4")}, timeout=120)
                            sent_ok = resp.ok
                        else:
                            clean_vname = format_clean_video_filename(title or item_title, ext=ext)
                            data_dict["supports_streaming"] = True
                            if v_meta.get("duration"): data_dict["duration"] = v_meta["duration"]
                            if v_meta.get("width"): data_dict["width"] = v_meta["width"]
                            if v_meta.get("height"): data_dict["height"] = v_meta["height"]
                            files_payload = {"video": (clean_vname, f, "video/mp4")}
                            thumb_fh = None
                            if thumb_file and os.path.exists(thumb_file):
                                try:
                                    thumb_fh = open(thumb_file, "rb")
                                    files_payload["thumb"] = ("thumb.jpg", thumb_fh, "image/jpeg")
                                except Exception:
                                    pass
                            try:
                                resp = tg("sendVideo", data=data_dict, files=files_payload, timeout=120)
                                sent_ok = resp.ok
                            finally:
                                if thumb_fh:
                                    try: thumb_fh.close()
                                    except Exception: pass

                if sent_ok:
                    delivered_count += 1
                    if user_id:
                        try:
                            record_user_download(user_id, fsize)
                        except Exception:
                            pass

                # IMMEDIATELY PURGE FILE FROM DISK
                try:
                    os.remove(fpath)
                except Exception:
                    pass
                if thumb_file and os.path.exists(thumb_file):
                    try:
                        os.remove(thumb_file)
                    except Exception:
                        pass

            # Adaptive Thermal & Battery Pacing Cooldown
            cooldown = queue_manager.get_adaptive_cooldown()
            time.sleep(cooldown)

        delete_msg(chat_id, progress_msg_id)
        limit_notice = f"\n💡 <i>Delivered first {total_items} items (Batch limit for current tier).</i>" if len(raw_entries) > total_items else ""
        send_msg(
            chat_id,
            f"✅ <b>Playlist Delivery Complete!</b>\n\n"
            f"📦 <b>Delivered:</b> <code>{delivered_count}/{total_items} items</code>\n"
            f"📌 <b>Playlist:</b> <i>{escape_html(pl_title)}</i>{limit_notice}\n\n"
            f"💾 <i>Phone disk space kept 100% free & clean.</i>",
            parse_mode="HTML"
        )

    except DownloadCancelledException:
        print(f"🛑 [Chat {chat_id}] Playlist job {job_id} cancelled by user.")
    except Exception as e:
        if is_job_cancelled(job_id) or "cancelled" in str(e).lower():
            print(f"🛑 [Chat {chat_id}] Playlist job {job_id} cancelled cleanly: {e}")
            return
        print(f"❌ Error in process_playlist_job: {e}")
        delete_msg(chat_id, progress_msg_id)
        send_msg(chat_id, f"❌ <b>Playlist Processing Notice:</b>\n{escape_html(str(e))}", parse_mode="HTML")

    finally:
        clear_job_cancelled(job_id)
        queue_manager.release_playlist_slot(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)


def dispatch_media_request(target_url, chat_id, user_id, force_audio=False):
    """
    Zero-blocking instant async dispatch + real-time pipeline connection.
    """
    def worker():
        platform = detect_platform(target_url)
        action_type = "Audio" if force_audio else "Media"
        job_id = uuid.uuid4().hex[:10]

        stop_kb = json.dumps({
            "inline_keyboard": [
                [{"text": "🛑 Stop / Cancel Download", "callback_data": f"stop_{job_id}"}]
            ]
        })

        # 1. Instant Telegram Chat Action
        try:
            tg("sendChatAction", data={"chat_id": chat_id, "action": "upload_document" if force_audio else "upload_video"})
        except Exception:
            pass

        # 2. Instant placeholder message
        progress_msg_id = None
        try:
            p_resp = send_msg(
                chat_id,
                f"⚡ <b>Connecting to {platform} {action_type}...</b>\n_Probing stream formats & establishing pipeline..._",
                reply_markup=stop_kb,
                parse_mode="HTML"
            )
            if p_resp.ok:
                progress_msg_id = p_resp.json().get("result", {}).get("message_id")
        except Exception:
            pass

        if not force_audio:
            # Check if URL is a Playlist first
            probe_pl = probe_playlist_info(target_url)
            if probe_pl and probe_pl.get("is_playlist"):
                req_id = uuid.uuid4().hex[:8]
                PENDING_MEDIA_REQUESTS[req_id] = {
                    "url": target_url.strip(),
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "job_id": job_id,
                    "is_playlist": True
                }
                msg_text = (
                    f"📋 <b>Playlist Detected ({probe_pl['item_count']} items)</b>\n"
                    f"📌 <b>Title:</b> <i>{escape_html(probe_pl['title'])}</i>\n\n"
                    f"⚡ <i>Stream-and-upload pipeline will deliver files one-by-one with zero phone storage buildup.</i>\n\n"
                    f"👇 <b>Select delivery format for this playlist:</b>"
                )
                buttons = [
                    [{"text": f"🎬 Download All as Video (MP4 • {probe_pl['item_count']} items)", "callback_data": f"pl:video:{req_id}"}],
                    [{"text": f"🎵 Download All as Audio (MP3 • {probe_pl['item_count']} items)", "callback_data": f"pl:audio:{req_id}"}],
                    [{"text": "❌ Cancel", "callback_data": f"pl:cancel:{req_id}"}]
                ]
                kb = json.dumps({"inline_keyboard": buttons})
                if progress_msg_id:
                    edit_msg(chat_id, progress_msg_id, msg_text, reply_markup=kb, parse_mode="HTML")
                else:
                    send_msg(chat_id, msg_text, reply_markup=kb, parse_mode="HTML")
                return

            probe = probe_media_info(target_url)
            if probe and probe.get("is_large"):
                req_id = uuid.uuid4().hex[:8]
                PENDING_MEDIA_REQUESTS[req_id] = {
                    "url": target_url.strip(),
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "job_id": job_id
                }
                dur_info = f"\n⏱️ <b>Duration:</b> <code>{probe['duration_str']}</code>" if probe.get("duration_str") else ""
                msg_text = (
                    f"🎬 <b>Large Media Detected (~{probe['best_mb']} MB)</b>\n"
                    f"📌 <b>Title:</b> <i>{escape_html(probe['title'])}</i>{dur_info}\n\n"
                    f"👇 <b>Select your preferred download format:</b>"
                )
                buttons = [
                    [{"text": f"🎬 Highest Quality (Full HD • ~{probe['best_mb']} MB)", "callback_data": f"dl:best:{req_id}"}],
                    [{"text": "⚖️ Balanced Quality (Fast • < 50 MB)", "callback_data": f"dl:balanced:{req_id}"}],
                    [{"text": "🎵 MP3 Audio Only (~8 MB)", "callback_data": f"dl:audio:{req_id}"}],
                    [{"text": "❌ Cancel", "callback_data": f"dl:cancel:{req_id}"}]
                ]
                kb = json.dumps({"inline_keyboard": buttons})
                if progress_msg_id:
                    edit_msg(chat_id, progress_msg_id, msg_text, reply_markup=kb, parse_mode="HTML")
                else:
                    send_msg(chat_id, msg_text, reply_markup=kb, parse_mode="HTML")
                return

        # Direct download execution with existing progress message
        process_media_job(target_url.strip(), chat_id, user_id=user_id, force_audio=force_audio, quality_choice="best", existing_progress_msg_id=progress_msg_id, custom_job_id=job_id)

    DOWNLOAD_EXECUTOR.submit(worker)


def get_users_breakdown_text(users):
    """Generates the detailed member usage breakdown identical to /users command."""
    sorted_users = sorted(
        users.items(),
        key=lambda item: item[1].get("total_downloads", 0),
        reverse=True
    )
    active_list = []
    trial_list = []
    expired_list = []

    for uid, data in sorted_users:
        act, rle, d_left, nm, t_left = is_user_active(uid, users)
        tot_dl = data.get("total_downloads", 0)
        ref_cnt = data.get("referral_count", 0)
        ref_suffix = f" · 👥 {ref_cnt} refs" if ref_cnt > 0 else ""
        safe_nm = escape_html(nm)

        if rle == "owner" or str(uid) == str(OWNER_ID):
            active_list.append(f"👑 <code>{uid}</code> — <b>*{safe_nm}*</b>\n   📥 {tot_dl} downloads{ref_suffix} · ♾️ Lifetime")
        elif rle == "subscriber" and act:
            exp_str = datetime.fromtimestamp(data.get("expires_at", 0)).strftime('%d %b')
            active_list.append(f"🟢 <code>{uid}</code> — <b>*{safe_nm}*</b>\n   📥 {tot_dl} downloads{ref_suffix} · ⏳ {d_left}d left ({exp_str})")
        elif rle == "trial" and act:
            trial_used = data.get("trial_used", 0)
            bytes_used_mb = data.get("free_bytes_used", 0) // (1024 * 1024)
            trial_list.append(f"🎁 <code>{uid}</code> — <b>*{safe_nm}*</b>\n   📥 {tot_dl} downloads{ref_suffix} · 🎁 {t_left}/10 left ({trial_used}/10 used) · 💾 {bytes_used_mb}/1,024 MB")
        else:
            if rle == "trial":
                trial_used = data.get("trial_used", 0)
                bytes_used_mb = data.get("free_bytes_used", 0) // (1024 * 1024)
                expired_list.append(f"🔴 <code>{uid}</code> — <b>*{safe_nm}*</b>\n   📥 {tot_dl} downloads{ref_suffix} · 🔴 Monthly Limit Reached ({trial_used}/10 used · {bytes_used_mb}/1,024 MB)")
            else:
                expired_list.append(f"🔴 <code>{uid}</code> — <b>*{safe_nm}*</b>\n   📥 {tot_dl} downloads{ref_suffix} · 🔴 Expired")


    lines = [f"📊 <b>Detailed Member Usage Breakdown ({len(users)} Total)</b>\n"]
    if active_list:
        lines.append("<b>Paying Subscribers:</b>\n" + "\n".join(active_list))
    if trial_list:
        lines.append("\n<b>Free Trial Members:</b>\n" + "\n".join(trial_list))
    if expired_list:
        lines.append("\n<b>Expired Members:</b>\n" + "\n".join(expired_list))
    return "\n".join(lines)


def get_cookies_health_text():
    """Generates the full live cookies health report identical to /checkcookies command."""
    target_cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
    health = check_platform_cookies_health(target_cookie_path)
    stats = analyze_cookies_file(target_cookie_path)
    age_str = "Never"
    if os.path.exists(target_cookie_path):
        mtime = os.path.getmtime(target_cookie_path)
        age_str = datetime.fromtimestamp(mtime).strftime('%d %b %Y, %I:%M %p')

    fb_stat = health["facebook"]
    ig_stat = health["instagram"]
    fb_icon = "✅" if fb_stat["status"] == "active" else "⚠️"
    ig_icon = "✅" if ig_stat["status"] == "active" else "⚠️"

    return (
        f"🍪 <b>Live Cookies Engine & Session Health:</b>\n\n"
        f"📅 <b>Last Updated:</b> <code>{age_str}</code>\n"
        f"• 🔴 <b>YouTube:</b> ✅ <code>Visitor Tokens Active</code> ({stats['youtube']} cookies)\n"
        f"• 📸 <b>Instagram:</b> {ig_icon} <code>{ig_stat['message']}</code> ({stats['instagram']} cookies)\n"
        f"• 🔵 <b>Facebook:</b> {fb_icon} <code>{fb_stat['message']}</code> ({stats['facebook']} cookies)\n"
        f"• 🐦 <b>Twitter/X:</b> ✅ <code>Guest Engine Active</code> ({stats['twitter']} cookies)\n"
        f"• 📁 <b>Total Cookies:</b> <code>{stats['total']}</code>\n\n"
        f"💡 <b>To Update:</b> Simply send your new <code>cookies.txt</code> file directly as a document into this chat!"
    )


def get_server_diagnostics_text(users):
    """Generates the full phone server diagnostics report identical to /status command."""
    metrics = get_server_metrics()
    active_count = sum(1 for uid in users if is_user_active(uid, users)[0])

    bat_str = f"{metrics.get('battery_pct')}%" if metrics.get("battery_pct") is not None else "6,000mAh Battery Active"
    if metrics.get("battery_status") and metrics.get("battery_status") != "Unknown":
        bat_str += f" ({metrics.get('battery_status')})"
    if metrics.get("battery_temp"):
        bat_str += f" · {metrics.get('battery_temp')}°C"

    cpu_str = f"{metrics.get('cpu_pct')}%" if metrics.get("cpu_pct") is not None else "Active"
    ram_str = f"{metrics.get('ram_used_mb')} MB / {metrics.get('ram_total_mb')} MB ({metrics.get('ram_pct')}%)" if metrics.get("ram_total_mb") else "Active"
    disk_str = f"{metrics.get('disk_free_gb')} GB Free / {metrics.get('disk_total_gb')} GB ({metrics.get('disk_free_pct')}% Free)" if metrics.get("disk_total_gb") else "Active"

    return (
        "🤖 <b>Eternal Automation Server Diagnostics</b>\n\n"
        "📱 Host: Samsung Galaxy M31 (Dedicated ARM64)\n"
        f"⚡ CPU Load: <b>*{cpu_str}*</b>\n"
        f"🧠 Memory (RAM): <b>*{ram_str}*</b>\n"
        f"💾 Storage (Disk): <b>*{disk_str}*</b>\n"
        f"🔋 Battery: <b>*{bat_str}*</b>\n"
        f"🧹 Downloads Cache: <b>*{metrics.get('downloads_size_mb')} MB*</b>\n"
        f"👥 Database Users: <b>*{len(users)}*</b> (Active: {active_count})\n"
        f"⏱️ Bot Uptime: <b>*{metrics.get('uptime_str')}*</b>\n"
        "🟢 Health Status: <b>*100% Operational*</b>"
    )


def get_revenue_analytics_text(users):
    """Generates the business analytics report identical to /revenue command."""
    active_paying = sum(1 for uid, d in users.items() if is_user_active(uid, users)[0] and d.get("role") == "subscriber")
    trial_active = sum(1 for uid, d in users.items() if is_user_active(uid, users)[0] and d.get("role") == "trial")

    total_collected = 0
    for uid, d in users.items():
        if d.get("role") == "owner": continue
        pmts = d.get("payments", [])
        if pmts:
            total_collected += sum(p.get("amount", SUBSCRIPTION_PRICE_INR) for p in pmts)
        elif d.get("role") == "subscriber":
            total_collected += SUBSCRIPTION_PRICE_INR

    est_mrr = active_paying * SUBSCRIPTION_PRICE_INR
    expired_count = sum(1 for uid, d in users.items() if d.get("role") == "subscriber" and not is_user_active(uid, users)[0])

    return (
        "📊 <b>EternalAutomationBot Business Analytics</b>\n\n"
        f"💵 Plan Price: ₹{SUBSCRIPTION_PRICE_INR} / month\n"
        f"👥 Active Paying Subscribers: <b>*{active_paying}*</b>\n"
        f"🎁 Active Free Trial Users: <b>*{trial_active}*</b>\n"
        f"📈 Monthly Recurring Revenue (MRR): <b>*₹{est_mrr}*</b>\n"
        f"💰 Total Collections: <b>*₹{total_collected}*</b>\n"
        f"📉 Expired Members: <b>*{expired_count}*</b>\n\n"
        f"📱 Host: Samsung Galaxy M31 (Cost: ₹0/mo)\n"
        f"⚡ Server Margin: <b>*100%*</b>"
    )


def get_banned_users_text():
    """Generates the list of banned users identical to /banned command."""
    b_list = get_banned_users()
    if not b_list:
        return "🚫 <b>Banned Accounts:</b>\n\n<i>No users are currently banned.</i>"
    lines = [f"🚫 <b>Banned Users List ({len(b_list)} total):</b>\n"]
    for idx, b in enumerate(b_list, 1):
        b_date = datetime.fromtimestamp(b['banned_at']).strftime('%d %b %Y, %I:%M %p') if b.get('banned_at') else "Unknown"
        lines.append(f"{idx}. 👤 <b>{escape_html(b['name'])}</b> (<code>{b['user_id']}</code>)\n   📅 <i>{b_date}</i> · Reason: <i>{escape_html(b.get('reason', 'Policy violation'))}</i>")
    return "\n\n".join(lines)


def get_admin_dashboard_view(users=None):

    """Generates the interactive Admin Dashboard text and 1-tap inline buttons."""

    if users is None:
        users = load_users()
    metrics = get_server_metrics()
    active_count = sum(1 for uid in users if is_user_active(uid, users)[0])
    uptime = metrics.get('uptime_str', 'Active')
    bat_str = f"{metrics.get('battery_pct')}%" if metrics.get("battery_pct") is not None else "6,000mAh"
    if metrics.get("battery_temp"): bat_str += f" ({metrics.get('battery_temp')}°C)"

    text = (
        "🎛️ <b>Eternal Automation Admin Control Center</b>\n\n"
        "📱 <b>Host:</b> Samsung Galaxy M31 (Dedicated ARM64 Server)\n"
        f"⏱️ <b>Uptime:</b> <code>{uptime}</code>\n"
        f"🔋 <b>Battery:</b> <code>{bat_str}</code>\n"
        f"👥 <b>Total Members:</b> <b>*{len(users)}*</b> (Active: {active_count})\n"
        "🟢 <b>Status:</b> 100% Operational\n\n"
        "👇 <b>Select an action below for instant 1-tap management:</b>"
    )
    buttons = [
        [
            {"text": "👥 Member Stats", "callback_data": "adm_users"},
            {"text": "💰 Revenue & MRR", "callback_data": "adm_revenue"}
        ],
        [
            {"text": "🌡️ Server Health", "callback_data": "adm_health"},
            {"text": "🍪 Cookie Health", "callback_data": "adm_cookies"}
        ],
        [
            {"text": "💾 DB Backup", "callback_data": "adm_backup"},
            {"text": "🚫 Banned Accounts", "callback_data": "adm_banned"}
        ],
        [
            {"text": "🔄 Refresh Dashboard", "callback_data": "adm_menu"}
        ]
    ]
    return text, json.dumps({"inline_keyboard": buttons})


# -----------------------
# BOT EVENT CYCLE
# -----------------------


def run_bot_cycle(last_update_id=0):
    """Executes a 30s long-polling cycle, processing messages, callbacks, and media downloads."""
    try:
        resp = TG_SESSION.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 30},
            timeout=35
        )
        if resp.status_code != 200:
            return last_update_id
        updates = resp.json().get("result", [])
    except Exception as e:
        print(f"⚠️ getUpdates error: {e}")
        time.sleep(1)
        return last_update_id

    max_update_id = last_update_id
    users = load_users()

    for update in updates:
        up_id = update.get("update_id", 0)
        if up_id > max_update_id:
            max_update_id = up_id

        # -----------------------
        # 0. CHANNEL POST MODERATION & REAL-TIME DEDUPLICATION
        # -----------------------
        ch_post = update.get("channel_post") or update.get("edited_channel_post")
        if ch_post:
            ch_text = ch_post.get("text", "") or ch_post.get("caption", "")
            ch_chat_id = ch_post.get("chat", {}).get("id")
            ch_msg_id = ch_post.get("message_id")
            
            # 0a. Spam Keyword Moderation
            if "share price live updates" in ch_text.lower():
                delete_msg(ch_chat_id, ch_msg_id)
                print(f"🗑️ [SPAM DELETED] Deleted matching channel post (msg_id: {ch_msg_id}): {ch_text[:40]}...")
                continue

            # 0b. Real-Time Duplicate Headline Moderation
            if ch_text and check_and_record_channel_post(ch_chat_id, ch_msg_id, ch_text):
                delete_msg(ch_chat_id, ch_msg_id)
                print(f"🗑️ [DUPLICATE DELETED] Removed duplicate channel post (msg_id: {ch_msg_id}): {ch_text[:60]}...")
            continue

        # -----------------------
        # 1. INLINE CALLBACK QUERIES (APPROVE / REJECT PAYMENTS)
        # -----------------------
        callback = update.get("callback_query")
        if callback:
            cb_id = callback.get("id")
            from_user = callback.get("from", {})
            cb_user_id = str(from_user.get("id"))
            cb_data = callback.get("data", "")
            cb_msg = callback.get("message", {})
            cb_chat_id = cb_msg.get("chat", {}).get("id")
            cb_msg_id = cb_msg.get("message_id")

            # Check if user is banned before processing any interactive callbacks
            if is_user_banned(cb_user_id, users):
                tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "⛔ Your account has been suspended by Admin.", "show_alert": True})
                continue

            if cb_data.startswith("app_30_") or cb_data.startswith("app_60_"):
                if cb_user_id != OWNER_ID:
                    tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "⛔ Only Admin can approve.", "show_alert": True})
                    continue

                parts = cb_data.split("_")
                days = int(parts[1])
                target_uid = str(parts[2])

                added_user = add_or_renew_subscription(target_uid, days=days, payment_ref=f"inline_{days}d")
                exp_ts = added_user.get("expires_at")
                exp_date_str = datetime.fromtimestamp(exp_ts).strftime('%d %b %Y') if exp_ts else "Lifetime"

                tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": f"✅ Approved {days} Days!"})
                tg(
                    "editMessageCaption",
                    data={
                        "chat_id": cb_chat_id,
                        "message_id": cb_msg_id,
                        "caption": f"✅ <b>APPROVED ({days} Days)</b>\n👤 User: <code>{target_uid}</code> ({escape_html(added_user.get('name'))})\n📅 Expiry: {exp_date_str}\nApproved by Owner at {datetime.now().strftime('%I:%M %p')}",
                        "parse_mode": "HTML"
                    }
                )

                send_msg(
                    target_uid,
                    f"🎉 <b>Payment Approved & Subscription Active!</b>\n\n"
                    f"💎 Plan: <b>*₹{SUBSCRIPTION_PRICE_INR} / {days} Days Unlimited HD Access*</b>\n"
                    f"📅 Valid until: <b>*{exp_date_str}*</b>\n\n"
                    f"🚀 Paste any link from YouTube, Instagram, Facebook, TikTok, Pinterest, or X to start downloading!",
                    parse_mode="HTML"
                )
                continue

            elif cb_data.startswith("rej_"):
                if cb_user_id != OWNER_ID:
                    tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "⛔ Only Admin can reject.", "show_alert": True})
                    continue

                target_uid = cb_data[4:].strip()
                tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "❌ Payment Rejected."})
                tg(
                    "editMessageCaption",
                    data={
                        "chat_id": cb_chat_id,
                        "message_id": cb_msg_id,
                        "caption": f"❌ <b>REJECTED / INVALID PAYMENT</b>\n👤 User: <code>{target_uid}</code>\nMarked as rejected at {datetime.now().strftime('%I:%M %p')}",
                        "parse_mode": "HTML"
                    }
                )

                send_msg(
                    target_uid,
                    f"⚠️ <b>Payment Verification Failed</b>\n\n"
                    f"Your payment screenshot could not be verified.\n"
                    f"Please send the correct transaction screenshot or contact {ADMIN_HANDLE} for support.",
                    parse_mode="HTML"
                )
                continue

            elif cb_data.startswith("ban_"):
                if cb_user_id != OWNER_ID:
                    tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "⛔ Only Admin can ban.", "show_alert": True})
                    continue

                target_uid = cb_data[4:].strip()
                tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": f"🚫 User {target_uid} Banned."})
                ok, msg = ban_user(target_uid, reason="Admin Manual Ban (Spam / Abuse)")
                if ok:
                    try:
                        edit_msg(cb_chat_id, cb_msg_id, f"🚫 <b>USER BANNED PERMANENTLY</b>\n👤 User ID: <code>{target_uid}</code>\nAction executed at {datetime.now().strftime('%I:%M %p')}", parse_mode="HTML")
                    except Exception:
                        pass
                    send_msg(
                        target_uid,
                        f"⛔ <b>Account Suspended</b>\n\n"
                        f"Your access to Eternal Automation Bot has been suspended by Admin for spamming or violating fair use policies.\n"
                        f"For support, contact Admin: {ADMIN_HANDLE}",
                        parse_mode="HTML"
                    )
                continue


            elif cb_data == "btn_help_ss":
                tg("answerCallbackQuery", data={"callback_query_id": cb_id})
                send_msg(
                    cb_chat_id,
                    "📸 <b>How to Send Payment Screenshot:</b>\n\n"
                    "1️⃣ Complete your ₹20 payment on GPay / PhonePe / Paytm / BHIM.\n"
                    "2️⃣ Take a clear screenshot of the Success screen showing UTR / Ref No.\n"
                    "3️⃣ Tap the 📎 Attachment icon in this chat, select the screenshot and send it!\n\n"
                    "⚡ Admin will approve it and activate your account immediately!",
                    parse_mode="HTML"
                )
                continue

            elif cb_data.startswith("dl:"):
                parts = cb_data.split(":", 2)
                choice = parts[1]
                req_id = parts[2]
                tg("answerCallbackQuery", data={"callback_query_id": cb_id})

                if choice == "cancel":
                    PENDING_MEDIA_REQUESTS.pop(req_id, None)
                    edit_msg(cb_chat_id, cb_msg_id, "❌ <b>Download Request Cancelled.</b>", parse_mode="HTML")
                    continue

                req = PENDING_MEDIA_REQUESTS.pop(req_id, None)
                if not req:
                    edit_msg(cb_chat_id, cb_msg_id, "⚠️ <i>This download request has expired. Please paste the link again.</i>", parse_mode="HTML")
                    continue

                label = "Highest Quality (Full HD)" if choice == "best" else ("Balanced Quality (<50MB)" if choice == "balanced" else "MP3 Audio")
                edit_msg(cb_chat_id, cb_msg_id, f"⚡ <b>Processing in {label}...</b>\n_Connecting to dedicated pipeline..._", parse_mode="HTML")
                DOWNLOAD_EXECUTOR.submit(
                    process_media_job,
                    req["url"],
                    req["chat_id"],
                    user_id=req["user_id"],
                    force_audio=(choice == "audio"),
                    quality_choice=choice,
                    existing_progress_msg_id=cb_msg_id,
                    custom_job_id=req.get("job_id")
                )
            elif cb_data.startswith("pl:"):
                parts = cb_data.split(":", 2)
                choice = parts[1]
                req_id = parts[2]
                tg("answerCallbackQuery", data={"callback_query_id": cb_id})

                if choice == "cancel":
                    PENDING_MEDIA_REQUESTS.pop(req_id, None)
                    edit_msg(cb_chat_id, cb_msg_id, "❌ <b>Playlist Download Cancelled.</b>", parse_mode="HTML")
                    continue

                req = PENDING_MEDIA_REQUESTS.pop(req_id, None)
                if not req:
                    edit_msg(cb_chat_id, cb_msg_id, "⚠️ <i>This request has expired. Please send the link again.</i>", parse_mode="HTML")
                    continue

                mode_label = "High-Definition Video (MP4)" if choice == "video" else "High-Quality MP3 Audio"
                edit_msg(cb_chat_id, cb_msg_id, f"📋 <b>Processing Playlist ({mode_label})...</b>\n_Connecting stream-and-upload pipeline..._", parse_mode="HTML")
                DOWNLOAD_EXECUTOR.submit(
                    process_playlist_job,
                    req["url"],
                    req["chat_id"],
                    user_id=req["user_id"],
                    force_audio=(choice == "audio"),
                    quality_choice="best",
                    existing_progress_msg_id=cb_msg_id,
                    custom_job_id=req.get("job_id")
                )
                continue

            elif cb_data.startswith("stop_"):
                raw_job_id = cb_data[5:].strip()
                if not re.match(r'^[a-zA-Z0-9_-]+$', raw_job_id):
                    tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "⚠️ Invalid Job Request", "show_alert": True})
                    continue

                job_id = raw_job_id
                mark_job_cancelled(job_id)
                queue_manager.release_heavy_slot(job_id)
                queue_manager.release_playlist_slot(job_id)

                tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "🛑 Download stopped."})
                job_dir = os.path.join(DOWNLOADS_DIR, f"job_{job_id}")
                if os.path.abspath(job_dir).startswith(os.path.abspath(DOWNLOADS_DIR) + os.sep):
                    shutil.rmtree(job_dir, ignore_errors=True)
                edit_msg(cb_chat_id, cb_msg_id, "🛑 <b>Download Cancelled</b>\n\n<i>Your download request was stopped as requested.</i>", parse_mode="HTML")
                continue

            elif cb_data == "btn_help_support":
                tg("answerCallbackQuery", data={"callback_query_id": cb_id})
                send_msg(
                    cb_chat_id,
                    f"💬 <b>Need Help?</b>\n\n"
                    f"Type <code>/support <your message></code> right here to message the Admin directly!\n"
                    f"Or message Admin on Telegram: {ADMIN_HANDLE}",
                    parse_mode="HTML"
                )
                continue

            elif cb_data.startswith("adm_"):
                if cb_user_id != OWNER_ID:
                    tg("answerCallbackQuery", data={"callback_query_id": cb_id, "text": "⛔ Owner Only.", "show_alert": True})
                    continue

                tg("answerCallbackQuery", data={"callback_query_id": cb_id})
                back_kb = json.dumps({"inline_keyboard": [[{"text": "⬅️ Back to Admin Dashboard", "callback_data": "adm_menu"}]]})

                if cb_data == "adm_menu":
                    dash_text, dash_kb = get_admin_dashboard_view(users)
                    edit_msg(cb_chat_id, cb_msg_id, dash_text, reply_markup=dash_kb, parse_mode="HTML")
                    continue

                elif cb_data == "adm_users":
                    txt = get_users_breakdown_text(users)
                    edit_msg(cb_chat_id, cb_msg_id, txt, reply_markup=back_kb, parse_mode="HTML")
                    continue

                elif cb_data == "adm_revenue":
                    txt = get_revenue_analytics_text(users)
                    edit_msg(cb_chat_id, cb_msg_id, txt, reply_markup=back_kb, parse_mode="HTML")
                    continue

                elif cb_data == "adm_health":
                    txt = get_server_diagnostics_text(users)
                    edit_msg(cb_chat_id, cb_msg_id, txt, reply_markup=back_kb, parse_mode="HTML")
                    continue

                elif cb_data == "adm_cookies":
                    txt = get_cookies_health_text()
                    edit_msg(cb_chat_id, cb_msg_id, txt, reply_markup=back_kb, parse_mode="HTML")
                    continue

                elif cb_data == "adm_banned":
                    txt = get_banned_users_text()
                    edit_msg(cb_chat_id, cb_msg_id, txt, reply_markup=back_kb, parse_mode="HTML")
                    continue


                elif cb_data == "adm_backup":
                    send_backup_to_owner()
                    edit_msg(cb_chat_id, cb_msg_id, "💾 <b>Backup Dispatched!</b>\n\n<i>Latest JSON database backup has been uploaded to your private chat.</i>", reply_markup=back_kb, parse_mode="HTML")
                    continue





        # -----------------------
        # 2. MESSAGE UPDATES
        # -----------------------
        message = update.get("message")
        if not message:
            continue

        chat_id = message["chat"]["id"]
        from_user = message.get("from", {})
        user_id = str(from_user.get("id", chat_id))
        username = from_user.get("username", "")
        first_name = from_user.get("first_name", "")
        last_name = from_user.get("last_name", "")
        user_name = f"{first_name} {last_name}".strip() or username or f"User_{user_id[-4:]}"

        original_text = message.get("text") or message.get("caption") or ""
        text = original_text.strip().lower()

        # Auto-registration for new visitors
        if user_id not in users and user_id != OWNER_ID:
            ref_id = None
            if text.startswith("/start ref_"):
                parts = text.split("ref_")
                if len(parts) > 1 and parts[1].strip().isdigit():
                    ref_id = parts[1].strip()

            users[user_id] = {
                "name": user_name,
                "role": "trial",
                "trial_used": 0,
                "subscribed_at": int(time.time()),
                "expires_at": None,
                "total_downloads": 0,
                "referral_count": 0,
                "referred_by": ref_id,
                "free_month": datetime.now().strftime("%Y-%m"),
                "free_bytes_used": 0
            }
            save_users(users)


        is_active, user_role, days_left, db_name, trials_left = is_user_active(user_id, users)

        if user_role == "banned" or is_user_banned(user_id, users):
            send_msg(
                chat_id,
                f"⛔ <b>Account Suspended</b>\n\n"
                f"Your account has been suspended by Admin for spamming or violating terms.\n"
                f"For support, contact Admin: {ADMIN_HANDLE}",
                parse_mode="HTML"
            )
            continue


        # -----------------------
        # 3. OWNER COOKIE UPLOAD INTERCEPTOR
        # -----------------------
        has_photo = bool(message.get("photo"))
        has_document = bool(message.get("document"))

        if user_id == OWNER_ID and has_document:
            doc = message.get("document", {})
            fname = doc.get("file_name", "").lower()
            if "cookie" in fname or fname.endswith(".txt") or text in ("/cookies", "/updatecookies", "cookies"):
                file_id = doc.get("file_id")
                f_info = tg("getFile", data={"file_id": file_id})
                if f_info.ok:
                    remote_fpath = f_info.json().get("result", {}).get("file_path")
                    if remote_fpath:
                        d_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{remote_fpath}"
                        try:
                            r = TG_SESSION.get(d_url, timeout=20)
                            if r.status_code == 200:
                                content = r.text
                                if "# netscape" in content.lower() or "\t" in content:
                                    target_cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
                                    backup_cookie_path = os.path.join(os.path.dirname(__file__), "cookies_backup.txt")
                                    if os.path.exists(target_cookie_path):
                                        shutil.copyfile(target_cookie_path, backup_cookie_path)
                                    with open(target_cookie_path, "w", encoding="utf-8") as f:
                                        f.write(content)

                                    stats = analyze_cookies_file(target_cookie_path)
                                    send_msg(
                                        chat_id,
                                        f"🍪 <b>Cookies Database Updated Successfully!</b>\n\n"
                                        f"📊 <b>Platform Analysis:</b>\n"
                                        f"• 🔴 <b>YouTube:</b> {'✅ Active' if stats['youtube'] else '⚠️ Missing'} ({stats['youtube']} cookies)\n"
                                        f"• 📸 <b>Instagram:</b> {'✅ Active' if stats['instagram'] else '⚠️ Missing'} ({stats['instagram']} cookies)\n"
                                        f"• 🔵 <b>Facebook:</b> {'✅ Active' if stats['facebook'] else '⚠️ Missing'} ({stats['facebook']} cookies)\n"
                                        f"• 🐦 <b>Twitter/X:</b> {'✅ Active' if stats['twitter'] else '⚠️ Missing'} ({stats['twitter']} cookies)\n"
                                        f"• 📁 <b>Total Cookies:</b> <code>{stats['total']}</code>\n\n"
                                        f"⚡ Downloader engine instantly reloaded with fresh credentials on phone server!",
                                        parse_mode="HTML"
                                    )
                        except Exception as ce:
                            print(f"⚠️ Error downloading cookie file: {ce}")
            elif ("user" in fname or "backup" in fname) and fname.endswith(".json"):
                # Database restore from Owner document upload
                file_id = doc.get("file_id")
                f_info = tg("getFile", data={"file_id": file_id})
                if f_info.ok:
                    remote_fpath = f_info.json().get("result", {}).get("file_path")
                    if remote_fpath:
                        d_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{remote_fpath}"
                        try:
                            r = TG_SESSION.get(d_url, timeout=20)
                            if r.status_code == 200:
                                new_data = json.loads(r.text)
                                if isinstance(new_data, dict):
                                    backup_name = os.path.join(os.path.dirname(USERS_FILE), f"users_backup_pre_restore_{int(time.time())}.json")
                                    if os.path.exists(USERS_FILE):
                                        shutil.copyfile(USERS_FILE, backup_name)
                                    current_users = load_users()
                                    current_users.update(new_data)
                                    save_users(current_users)
                                    send_msg(
                                        chat_id,
                                        f"👥 <b>User Database Restored Successfully!</b>\n\n"
                                        f"• <b>Total Users in Database:</b> <code>{len(current_users)}</code>\n"
                                        f"• <b>Pre-restore Safety Backup:</b> <code>{os.path.basename(backup_name)}</code>\n\n"
                                        f"Run /users to view the updated member breakdown.",
                                        parse_mode="HTML"
                                    )
                                    continue
                        except Exception as ue:
                            send_msg(chat_id, f"⚠️ Failed to restore user database: {escape_html(str(ue))}", parse_mode="HTML")


        # -----------------------
        # 4. PAYMENT SCREENSHOT ROUTING (EARLY RENEWAL & TRIAL USERS)
        # -----------------------
        if has_photo or has_document:
            # Forward payment screenshot to Admin with inline approval buttons

            file_id = message["photo"][-1]["file_id"] if has_photo else message["document"]["file_id"]
            admin_caption = (
                "💳 <b>New Payment Screenshot Received!</b>\n\n"
                f"👤 From: <b>{escape_html(user_name)}</b> (@{escape_html(username)})\n"
                f"🆔 User ID: <code>{user_id}</code>\n"
                f"💎 Current Status: <b>{user_role.capitalize()}</b> ({days_left}d left)\n"
                f"📅 Submitted: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n"
                "👇 <b>Tap below to Approve or Reject:</b>",
            )
            approval_keyboard = json.dumps({
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve 30 Days", "callback_data": f"app_30_{user_id}"},
                        {"text": "⭐ Approve 60 Days", "callback_data": f"app_60_{user_id}"}
                    ],
                    [
                        {"text": "❌ Reject Payment", "callback_data": f"rej_{user_id}"}
                    ]
                ]
            })

            # Send photo or document to Owner
            if has_photo:
                tg(
                    "sendPhoto",
                    data={
                        "chat_id": OWNER_ID,
                        "photo": file_id,
                        "caption": admin_caption[0],
                        "parse_mode": "HTML",
                        "reply_markup": approval_keyboard
                    }
                )
            else:
                tg(
                    "sendDocument",
                    data={
                        "chat_id": OWNER_ID,
                        "document": file_id,
                        "caption": admin_caption[0],
                        "parse_mode": "HTML",
                        "reply_markup": approval_keyboard
                    }
                )

            send_msg(
                chat_id,
                "✅ <b>Payment Screenshot Received!</b>\n\n"
                "Your screenshot has been forwarded to the Admin for instant verification.\n"
                "You will receive a confirmation message as soon as it is approved! 🚀",
                parse_mode="HTML"
            )
            continue

        # -----------------------
        # 4. COMMAND DISPATCH
        # -----------------------
        reply = None

        if text.startswith("/start"):
            if user_role == "owner":
                status_txt = "👑 <b>Owner</b> (Unlimited Lifetime Access)"
            elif user_role == "subscriber" and is_active:
                status_txt = f"💎 <b>Active Subscriber</b> ({days_left} Days Remaining)"
            elif user_role == "trial" and is_active:
                status_txt = f"🎁 <b>Free Monthly Plan</b> ({trials_left}/10 Downloads Left • Up to 1 GB)"
            else:
                status_txt = "🔴 <b>Monthly Limit Reached / Inactive</b>"

            reply = (
                "👋 <b>Welcome to Eternal Automation Bot!</b> ✨\n\n"
                f"👤 <b>Status:</b> {status_txt}\n\n"
                "📥 <b>How to Download Any Video, Song, or Photo:</b>\n"
                "Simply <b>copy and paste any link</b> from your favorite app directly into this chat!\n\n"
                "🌐 <b>Supported Platforms:</b>\n"
                "• 🔴 <b>YouTube</b> (Videos, Shorts & Playlists)\n"
                "• 📸 <b>Instagram</b> (Reels, Posts, Carousels & Audio)\n"
                "• 🔵 <b>Facebook</b> (Reels, Watch & HD Videos)\n"
                "• 📌 <b>Pinterest</b> (Photos, GIFs & Videos)\n"
                "• 🐦 <b>X / Twitter</b> (Videos & Media Threads)\n"
                "• 🎵 <b>TikTok</b> · 👾 <b>Reddit</b> · 🎧 <b>SoundCloud</b>\n\n"
                "✨ <b>Quick Features & Commands:</b>\n"
                "• 🎵 <code>/mp3 &lt;url&gt;</code> — Extract pure MP3 audio / song\n"
                "• 💎 <code>/whoami</code> — View active plan and expiry date\n"
                "• 🎁 <code>/refer</code> — Share invite link to earn +7 Free Days\n"
                "• 📊 <code>/mystats</code> — View your personal download history\n"
                "• 💬 <code>/support &lt;message&gt;</code> — Contact Admin directly\n"
                "• 📖 <code>/help</code> — View full user guide & tips\n\n"
                f"💎 <i>Unlimited 24/7 High-Speed Access for just ₹{SUBSCRIPTION_PRICE_INR}/Month.</i>"
            )

        elif text.startswith("/mp3 ") or text.startswith("/audio "):
            parts = original_text.split(None, 1)
            target_url = parts[1].strip() if len(parts) > 1 else ""
            extracted = extract_urls(target_url)

            if not extracted:
                reply = "ℹ️ <b>Usage:</b> <code>/mp3 &lt;url&gt;</code>\n\n<i>Example:</i> <code>/mp3 https://youtu.be/dQw4w9WgXcQ</code>"
            else:
                target_url = extracted[0]
                u = target_url.lower()
                if "spotify.com" in u:
                    send_msg(
                        chat_id,
                        "👋 <b>Spotify DRM Notice</b>\n\n"
                        "Spotify tracks are DRM-encrypted and cannot be downloaded directly.\n\n"
                        "💡 <b>Tip:</b> Search the song title on <b>*YouTube or SoundCloud*</b> and paste the link here for instant MP3 delivery!",
                        parse_mode="HTML"
                    )
                    continue

                if not is_safe_url(target_url):
                    send_msg(chat_id, "⚠️ <b>Security Notice:</b> Invalid or non-routable link.", parse_mode="HTML")
                    continue

                if not is_active:
                    qr_url, upi_address = get_upi_qr_url(user_id, SUBSCRIPTION_PRICE_INR)
                    caption = (
                        f"🔒 <b>Access Expired / Monthly Limit Reached</b>\n\n"
                        f"Unlock <b>*Unlimited High-Speed Access for ₹{SUBSCRIPTION_PRICE_INR}/Month*</b>:\n"
                        f"💳 Pay to UPI: <code>{upi_address}</code> <b>(Tap to copy)</b>\n\n"
                        f"👇 After paying, send screenshot here for instant activation!"
                    )
                    send_photo_url(chat_id, qr_url, caption=caption, parse_mode="HTML")
                    continue


                allowed, wait_mins, reason = check_rate_limit(user_id, is_owner=(user_role == "owner" or str(user_id) == str(OWNER_ID)))
                if not allowed:
                    if reason == "burst":
                        send_msg(
                            chat_id,
                            "⚠️ <b>Anti-Spam Warning</b>\n\n"
                            "Please slow down! You are sending requests too quickly.\n"
                            "💡 <i>To ensure fair server performance for all users, please wait a moment before sending more links.</i>",
                            parse_mode="HTML"
                        )
                    else:
                        send_msg(
                            chat_id,
                            f"⚠️ <b>Hourly Download Limit Reached</b>\n\n"
                            f"You have reached the maximum threshold of {MAX_DOWNLOADS_PER_HOUR} downloads per hour.\n"
                            f"⏳ Please wait <b>*{wait_mins} minute(s)*</b> before requesting more downloads.",
                            parse_mode="HTML"
                        )
                    continue
                else:
                    print(f"🎵 Dispatching audio extraction task for {target_url[:60]}")
                    dispatch_media_request(target_url, chat_id, user_id=user_id, force_audio=True)
                    continue



        elif text in ("/whoami", "/myplan", "whoami", "plan"):
            tot_dl = users.get(user_id, {}).get("total_downloads", 0)
            if user_role == "owner":
                reply = f"👑 <b>Account:</b> Owner\n🆔 User ID: <code>{user_id}</code>\n📥 Total Downloads: <b>*{tot_dl}*</b>\n⚡ Access: ♾️ Lifetime"
            elif user_role == "subscriber" and is_active:
                exp_ts = users[user_id].get("expires_at", 0)
                exp_date = datetime.fromtimestamp(exp_ts).strftime('%d %b %Y')
                reply = f"💎 <b>Account:</b> Active Subscriber\n👤 Name: {user_name}\n⏳ Days Remaining: <b>*{days_left} Days*</b> (Valid until {exp_date})\n📥 Downloads: <b>*{tot_dl}*</b>"
            elif user_role == "trial" and is_active:
                u_entry = users.get(user_id, {})
                used_mb = u_entry.get("free_bytes_used", 0) // (1024 * 1024)
                reply = f"🎁 <b>Account:</b> Free Plan Member\n👤 Name: {user_name}\n🎁 Downloads Left: <b>*{trials_left} of 10 this month*</b>\n💾 Bandwidth Used: <b>*{used_mb} MB / 1,024 MB (1 GB)*</b>\n\nType <code>/renew</code> to unlock Unlimited 4GB HD Access for ₹20/mo!"
            else:
                reply = f"🔴 <b>Account:</b> Inactive / Monthly Limit Reached\nType <code>/renew</code> to activate Unlimited Access for ₹{SUBSCRIPTION_PRICE_INR}/month."

        elif text in ("/mystats", "/stats", "mystats", "stats"):
            u_info = users.get(user_id, {})
            tot_dl = u_info.get("total_downloads", 0)
            ref_cnt = u_info.get("referral_count", 0)
            bonus_days = ref_cnt * 7

            status_txt = "👑 Owner (Lifetime)" if user_role == "owner" else (f"💎 Active ({days_left}d left)" if is_active else f"🎁 Free Plan ({trials_left}/10 left)")


            reply = (
                "📊 <b>Your Usage & Download Statistics</b>\n\n"
                f"👤 Member: <b>*{escape_html(user_name)}*</b>\n"
                f"🆔 User ID: <code>{user_id}</code>\n"
                f"💎 Status: <b>*{status_txt}*</b>\n"
                f"📥 Total Downloads Completed: <b>*{tot_dl}*</b>\n"
                f"👥 Friends Referred: <b>*{ref_cnt}*</b>\n"
                f"🎉 Referral Bonus Earned: <b>*+{bonus_days} Free Days*</b>"
            )

        elif text in ("/refer", "/referral", "refer", "referral"):
            u_info = users.get(user_id, {})
            ref_cnt = u_info.get("referral_count", 0)
            bonus_days = ref_cnt * 7

            reply = (
                "🎁 <b>Eternal Automation Referral Program</b>\n\n"
                "Invite friends and earn <b>*7 Days of Free Unlimited Access*</b> for every friend who subscribes!\n\n"
                "🔗 <b>Your Personal Referral Link:</b>\n"
                f"<code>https://t.me/EternalAutomationBot?start=ref_{user_id}</code> <b>(Tap to copy)</b>\n\n"
                f"👥 Friends Referred: <b>*{ref_cnt}*</b>\n"
                f"🎉 Total Bonus Earned: <b>*{bonus_days} Days Free*</b>"
            )

        elif text in ("💳 upgrade / renew", "upgrade", "renew", "/renew", "/upgrade"):
            qr_url, upi_address = get_upi_qr_url(user_id, SUBSCRIPTION_PRICE_INR)
            renew_caption = (
                "💳 <b>Subscription Renewal / Upgrade</b>\n\n"
                f"💎 Plan: <b>*₹{SUBSCRIPTION_PRICE_INR} / 30 Days Unlimited Access*</b>\n"
                f"• ⚡ Unlimited HD Downloads\n"
                f"• 🚀 2-Second Instant Delivery\n"
                f"• 📱 YouTube, Instagram, Facebook, TikTok, Pinterest, X\n\n"
                "📱 <b>Scan QR Code above or pay to UPI ID:</b>\n"
                f"💳 UPI ID: <code>{upi_address}</code> <b>(Tap to copy)</b>\n"
                f"👤 Your User ID: <code>{user_id}</code> <b>(Tap to copy)</b>\n\n"
                "👇 <b>After paying, send your payment screenshot here for instant activation!</b>"
            )
            renew_keyboard = json.dumps({
                "inline_keyboard": [
                    [
                        {"text": "📸 How to Send Screenshot", "callback_data": "btn_help_ss"},
                        {"text": "💬 Support", "callback_data": "btn_help_support"}
                    ]
                ]
            })
            send_photo_url(chat_id, qr_url, caption=renew_caption, parse_mode="HTML", reply_markup=renew_keyboard)
            continue

        elif text.startswith(("/support ", "/msg ", "/contact ")):
            query_msg = original_text.split(None, 1)[1].strip()
            if not query_msg:
                reply = "ℹ️ <b>Usage:</b> <code>/support <your question or message></code>"
            else:
                admin_notif = (
                    "📩 <b>New Customer Support Message!</b>\n\n"
                    f"👤 From: <b>*{escape_html(user_name)}*</b> (@{escape_html(username)})\n"
                    f"🆔 User ID: <code>{user_id}</code>\n"
                    f"📅 Date: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n"
                    f"💬 <b>User Message:</b>\n\"{escape_html(query_msg)}\"\n\n"
                    "👇 <b>To reply, use:</b>\n"
                    f"<code>/reply {user_id} <your message></code>"
                )
                admin_kb = json.dumps({
                    "inline_keyboard": [
                        [{"text": "🚫 Ban User", "callback_data": f"ban_{user_id}"}]
                    ]
                })
                send_msg(OWNER_ID, admin_notif, reply_markup=admin_kb, parse_mode="HTML")
                reply = "✅ <b>Support Message Delivered!</b>\nThank you! Admin will reply directly in this chat shortly! 🚀"


        elif text.startswith("/reply "):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can use this command."
            else:
                parts = original_text.split(None, 2)
                if len(parts) >= 3:
                    target_uid = parts[1].strip()
                    admin_resp = parts[2].strip()
                    user_notif = (
                        "👑 <b>Customer Support Reply:</b>\n\n"
                        f"{escape_html(admin_resp)}\n\n"
                        "💬 <i>If you have more questions, reply anytime using</i> <code>/support <message></code>"
                    )
                    send_msg(target_uid, user_notif, parse_mode="HTML")
                    reply = f"✅ Reply sent to User <code>{target_uid}</code>!"
                else:
                    reply = "ℹ️ <b>Usage:</b> <code>/reply &lt;user_id&gt; &lt;your reply message&gt;</code>"


        elif text in ("💬 support", "support", "/support", "contact", "/contact"):
            reply = (
                "💬 <b>Customer Support Desk</b>\n\n"
                "Type <code>/support &lt;your message&gt;</code> to contact Admin directly!\n"
                f"<i>Example:</i> <code>/support Hello, I have a question</code>\n\n"
                f"Or message Admin on Telegram: {ADMIN_HANDLE}"
            )

        elif text in ("/status", "/server", "/health"):
            if user_role == "owner":
                reply = get_server_diagnostics_text(users)
            else:
                metrics = get_server_metrics()
                active_count = sum(1 for uid in users if is_user_active(uid, users)[0])
                reply = (
                    "🤖 <b>EternalAutomationBot Server Metrics</b>\n\n"
                    f"📱 Host: Samsung Galaxy M31 (Dedicated ARM64 Server)\n"
                    f"🌐 Network: Ultra-High-Speed Pipeline Active\n"
                    f"👥 Total Users: {len(users)} (Active: {active_count})\n"
                    f"⏱️ Uptime: {metrics.get('uptime_str')}\n"
                    "🟢 Server Health: 100% Operational"
                )

        elif text in ("/cookies", "/getcookies", "/cookie", "cookies", "/cookiehealth", "/checkcookies") and user_role == "owner":
            target_cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
            reply_text = get_cookies_health_text()
            if os.path.exists(target_cookie_path) and os.path.getsize(target_cookie_path) > 0:
                with open(target_cookie_path, "rb") as cf:
                    tg("sendDocument", data={"chat_id": chat_id, "caption": reply_text, "parse_mode": "HTML"}, files={"document": ("cookies.txt", cf, "text/plain")})
            else:
                send_msg(chat_id, reply_text, parse_mode="HTML")
            continue




        elif text in ("/ping", "ping"):
            t0 = time.time()
            res = tg("getMe", timeout=5)
            latency_ms = int((time.time() - t0) * 1000)
            reply = f"🏓 <b>Pong!</b>\n⚡ Server Latency: <code>{latency_ms}ms</code>\n🟢 Dedicated Server Pipeline Active"

        elif text in ("/admin", "admin") and user_role == "owner":
            dash_text, dash_kb = get_admin_dashboard_view(users)
            send_msg(chat_id, dash_text, reply_markup=dash_kb, parse_mode="HTML")
            continue

        elif text in ("/help", "help"):
            if user_role == "owner":
                reply = (
                    "👑 <b>Eternal Automation Admin & User Guide</b>\n\n"
                    "🎛️ <b>Admin Quick Management (Owner Only):</b>\n"
                    "• <code>/admin</code> — Open 1-tap interactive control dashboard\n"
                    "• <code>/users</code> — View detailed member list & downloads\n"
                    "• <code>/revenue</code> — Business analytics, collections & MRR\n"
                    "• <code>/health</code> — Live phone server hardware diagnostics\n"
                    "• <code>/cookies</code> — Cookie status & visitor handshake health\n"
                    "• <code>/backup</code> — Download JSON database backup\n"
                    "• <code>/banned</code> — View all banned user accounts\n"
                    "• <code>/ban &lt;uid&gt; [reason]</code> — Ban abusive user\n"
                    "• <code>/unban &lt;uid&gt;</code> — Unban user account\n"
                    "• <code>/reply &lt;uid&gt; &lt;msg&gt;</code> — Direct reply to user support ticket\n"
                    "• <code>/broadcast &lt;msg&gt;</code> — Broadcast message to all members\n"
                    "• <code>/addsub &lt;uid&gt; [days] [name]</code> — Manually grant subscription\n"
                    "• <code>/deluser &lt;uid&gt;</code> — Remove user from database\n\n"
                    "📥 <b>User Features & Supported Platforms:</b>\n"
                    "• YouTube, Instagram, Facebook, Pinterest, X, TikTok, Reddit, SoundCloud\n"
                    "• <code>/mp3 &lt;url&gt;</code> — Audio extraction\n"
                    "• <code>/refer</code> — Referral link & bonus days\n"
                    "• <code>/whoami</code> — Plan status & quota"
                )
            else:
                reply = (
                    "📖 <b>Eternal Automation User Guide</b>\n\n"
                    "📥 <b>Supported Platforms:</b>\n"
                    "• 🔴 <b>YouTube</b> (Shorts & HD Videos)\n"
                    "• 📸 <b>Instagram</b> (Reels, Posts & Albums)\n"
                    "• 🔵 <b>Facebook</b> (Reels & Public/Private HD Videos)\n"
                    "• 📌 <b>Pinterest</b> (HD Photos & Direct Videos)\n"
                    "• 🐦 <b>X / Twitter</b> (Videos, Threads & Photos)\n"
                    "• 🎵 <b>TikTok</b> · 👾 <b>Reddit</b> · 🎧 <b>SoundCloud</b>\n\n"
                    "💡 <b>Pro-Tips:</b>\n"
                    "• Send <code>/mp3 &lt;link&gt;</code> to extract pure audio / song.\n"
                    "• Send <code>/refer</code> to earn +7 Free Days per friend.\n"
                    "• Send <code>/support &lt;msg&gt;</code> to chat with Admin."
                )


        # -----------------------
        # 5. ADMIN COMMANDS (OWNER ONLY)
        # -----------------------
        elif text.startswith("/broadcast "):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can broadcast messages."
            else:
                broadcast_content = original_text.split(None, 1)[1].strip()
                if not broadcast_content:
                    reply = "ℹ️ <b>Usage:</b> <code>/broadcast &lt;your announcement message&gt;</code>"
                else:
                    send_msg(chat_id, "📢 <b>Starting broadcast to all registered members...</b>", parse_mode="HTML")
                    success_cnt = 0
                    fail_cnt = 0
                    for target_uid in list(users.keys()):
                        try:
                            res = send_msg(target_uid, f"📢 <b>Announcement from Admin:</b>\n\n{escape_html(broadcast_content)}", parse_mode="HTML")
                            if res.ok: success_cnt += 1
                            else: fail_cnt += 1
                            time.sleep(0.05)
                        except Exception:
                            fail_cnt += 1


                    reply = (
                        f"✅ <b>Broadcast Finished!</b>\n\n"
                        f"📤 Successfully Delivered: <b>*{success_cnt}*</b>\n"
                        f"⚠️ Failed / Blocked: <b>*{fail_cnt}*</b>\n"
                        f"👥 Total Targeted: <b>*{len(users)}*</b>"
                    )

        elif text in ("/backup", "backup"):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can trigger database backups."
            else:
                send_backup_to_owner()
                reply = "✅ Latest database backup created and dispatched to your chat!"

        elif text.startswith("/addsub ") or text.startswith("/adduser ") or text.startswith("/add "):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can use this command."
            else:
                parts = original_text.split(None, 3)
                if len(parts) >= 2:
                    target_id = parts[1].strip()
                    if not target_id.isdigit() or len(target_id) < 5:
                        reply = "⚠️ <b>Invalid User ID</b>: Must be a numeric Telegram ID."
                    else:
                        days = 30
                        target_name = None
                        if len(parts) >= 3 and parts[2].isdigit():
                            days = int(parts[2])
                            if len(parts) >= 4:
                                target_name = parts[3].strip()
                        elif len(parts) >= 3:
                            target_name = parts[2].strip()

                        added_user = add_or_renew_subscription(target_id, days=days, name=target_name)
                        exp_ts = added_user.get("expires_at")
                        exp_date_str = datetime.fromtimestamp(exp_ts).strftime('%d %b %Y') if exp_ts else "Lifetime"

                        reply = (
                            f"✅ <b>Subscription Activated!</b>\n\n"
                            f"👤 Member: {added_user.get('name')}\n"
                            f"🆔 User ID: <code>{target_id}</code>\n"
                            f"⏳ Duration: {days} Days\n"
                            f"📅 Expiry Date: {exp_date_str}"
                        )
                        send_msg(
                            target_id,
                            f"🎉 <b>Your Subscription is Active!</b>\n\n"
                            f"💎 You now have <b>*{days} Days*</b> of Unlimited High-Speed HD Downloads!\n"
                            f"📅 Valid until: {exp_date_str}\n\n"
                            f"Send any link from YouTube, Instagram, Facebook, or TikTok to start downloading!"
                        )
                else:
                    reply = "ℹ️ Usage: <code>/addsub &lt;user_id&gt; [days] [name]</code>"

        elif text.startswith("/deluser ") or text.startswith("/cancelsub ") or text.startswith("/del "):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can use this command."
            else:
                parts = original_text.split()
                if len(parts) >= 2:
                    del_id = parts[1].strip()
                    if not del_id.isdigit():
                        reply = "⚠️ <b>Invalid User ID</b>: Must be a numeric Telegram ID."
                    elif del_id == user_id or del_id == OWNER_ID:
                        reply = "⚠️ You cannot remove your own Owner account."
                    elif del_id in users:
                        del_info = users.pop(del_id)
                        save_users(users)
                        reply = f"🗑️ <b>Subscriber Removed:</b>\n👤 Name: {del_info.get('name')}\n🆔 ID: <code>{del_id}</code>"
                    else:
                        reply = f"❓ User ID <code>{del_id}</code> was not found in database."
                else:
                    reply = "ℹ️ Usage: <code>/deluser &lt;user_id&gt;</code>"

        elif text in ("/subs", "/subscribers", "/users", "/userstats", "userstats", "users"):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can view member lists."
            else:
                reply = get_users_breakdown_text(users)

        elif text in ("/revenue", "/analytics", "/earnings"):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can view business analytics."
            else:
                reply = get_revenue_analytics_text(users)


        elif text.startswith("/ban "):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can ban users."
            else:
                parts = original_text.split(None, 2)
                if len(parts) >= 2:
                    target_id = parts[1].strip()
                    reason = parts[2].strip() if len(parts) >= 3 else "Spamming / Policy Violation"
                    if not target_id.isdigit():
                        reply = "⚠️ <b>Invalid User ID</b>: Must be a numeric Telegram ID."
                    elif target_id == user_id or target_id == OWNER_ID:
                        reply = "⚠️ You cannot ban the Admin / Owner account."
                    else:
                        ok, msg = ban_user(target_id, reason=reason)
                        if ok:
                            reply = (
                                f"🚫 <b>User Banned Successfully!</b>\n\n"
                                f"🆔 User ID: <code>{target_id}</code>\n"
                                f"📝 Reason: <i>{escape_html(reason)}</i>\n\n"
                                f"💡 User has been completely blocked from bot access."
                            )
                            send_msg(
                                target_id,
                                f"⛔ <b>Account Suspended</b>\n\n"
                                f"Your access to Eternal Automation Bot has been suspended by Admin.\n"
                                f"📝 <b>Reason:</b> <i>{escape_html(reason)}</i>\n\n"
                                f"For inquiries or appeal, contact Admin: {ADMIN_HANDLE}",
                                parse_mode="HTML"
                            )
                        else:
                            reply = f"⚠️ {msg}"
                else:
                    reply = "ℹ️ <b>Usage:</b> <code>/ban &lt;user_id&gt; [reason]</code>"

        elif text.startswith("/unban "):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can unban users."
            else:
                parts = original_text.split()
                if len(parts) >= 2:
                    target_id = parts[1].strip()
                    if not target_id.isdigit():
                        reply = "⚠️ <b>Invalid User ID</b>: Must be a numeric Telegram ID."
                    else:
                        ok, msg = unban_user(target_id)
                        if ok:
                            reply = (
                                f"✅ <b>User Unbanned Successfully!</b>\n\n"
                                f"🆔 User ID: <code>{target_id}</code>\n"
                                f"🟢 Account restored to active status."
                            )
                            send_msg(
                                target_id,
                                "✅ <b>Account Restored!</b>\n\n"
                                "Your access to Eternal Automation Bot has been restored by Admin. You can now send links to download again!",
                                parse_mode="HTML"
                            )
                        else:
                            reply = f"⚠️ {msg}"
                else:
                    reply = "ℹ️ <b>Usage:</b> <code>/unban &lt;user_id&gt;</code>"

        elif text in ("/banned", "/banlist", "banned"):
            if user_role != "owner":
                reply = "⛔ Only the Admin / Owner can view banned users."
            else:
                reply = get_banned_users_text()



        # -----------------------
        # 6. URL EXTRACTION & DOWNLOAD DISPATCH
        # -----------------------
        else:
            urls = extract_urls(original_text)
            if urls:
                valid_count = 0
                for target_url in urls[:5]:
                    u = target_url.lower()
                    if "spotify.com" in u:
                        send_msg(
                            chat_id,
                            "👋 <b>Spotify DRM Notice</b>\n\n"
                            "Spotify tracks are DRM-encrypted and cannot be downloaded directly.\n\n"
                            "💡 <b>Tip:</b> Search the song title on <b>YouTube or SoundCloud</b> and paste the link here for instant MP3 delivery!",
                            parse_mode="HTML"
                        )
                        continue

                    if not is_safe_url(target_url):
                        send_msg(chat_id, "⚠️ <b>Security Notice:</b> Invalid or non-routable link.", parse_mode="HTML")
                        continue

                    if not is_active:
                        qr_url, upi_address = get_upi_qr_url(user_id, SUBSCRIPTION_PRICE_INR)
                        caption = (
                            f"⚡ <b>Free Monthly Limit Reached (10 Videos / 1 GB)!</b>\n\n"
                            f"You have reached your 10 free downloads or 1 GB quota for this month. To enjoy uninterrupted, unlimited 4GB Full HD downloads 24/7, unlock <b>Unlimited Access for just ₹{SUBSCRIPTION_PRICE_INR} / Month</b>!\n\n"
                            f"📱 <b>Pay ₹{SUBSCRIPTION_PRICE_INR} via UPI:</b>\n"
                            f"💳 UPI ID: <code>{upi_address}</code> <b>(Tap to copy)</b>\n"
                            f"👤 Your ID: <code>{user_id}</code> <b>(Tap to copy)</b>\n\n"
                            f"👇 <b>After paying, send your payment screenshot here for instant activation!</b>"
                        )
                        send_photo_url(chat_id, qr_url, caption=caption, parse_mode="HTML")
                        valid_count += 1
                        break


                    allowed, wait_mins, reason = check_rate_limit(user_id, is_owner=(user_role == "owner" or str(user_id) == str(OWNER_ID)))
                    if not allowed:
                        if reason == "burst":
                            send_msg(
                                chat_id,
                                "⚠️ <b>Anti-Spam Warning</b>\n\n"
                                "Please slow down! You are sending requests too quickly.\n"
                                "💡 <i>To ensure fair server performance for all users, please wait a moment before sending more links.</i>",
                                parse_mode="HTML"
                            )
                        else:
                            send_msg(
                                chat_id,
                                f"⚠️ <b>Hourly Download Limit Reached</b>\n\n"
                                f"You have reached the maximum threshold of {MAX_DOWNLOADS_PER_HOUR} downloads per hour.\n"
                                f"⏳ Please wait <b>{wait_mins} minute(s)</b> before requesting more downloads.",
                                parse_mode="HTML"
                            )
                        break


                    print(f"🚀 Dispatching interactive download task for {target_url[:60]}")
                    dispatch_media_request(target_url.strip(), chat_id, user_id=user_id, force_audio=False)
                    valid_count += 1



                if valid_count == 0 and not urls:
                    reply = "⚠️ <b>Invalid Link</b>: Please check the link address and try again."
                else:
                    reply = None
            else:
                reply = (
                    "👋 <b>Welcome to Eternal Automation Bot!</b>\n\n"
                    "📥 <b>How to Save Any Post:</b>\n"
                    "Paste any link from <b>YouTube, Instagram, Facebook, Pinterest, X (Twitter), or TikTok</b> directly in this chat!\n\n"
                    "💡 <i>Type</i> <code>/help</code> <i>for a quick guide or</i> <code>/support &lt;msg&gt;</code> <i>to message Admin.</i>"
                )

        if reply:
            send_msg(chat_id, reply, parse_mode="HTML", reply_markup=REMOVE_KEYBOARD)

    return max_update_id
