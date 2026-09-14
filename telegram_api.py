import re
import json
import html
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config import BOT_TOKEN, ADMIN_HANDLE, OWNER_ID
import config


# -----------------------
# TELEGRAM API CLIENT
# -----------------------

TG_SESSION = requests.Session()
_retries = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    raise_on_status=False
)
_adapter = HTTPAdapter(max_retries=_retries, pool_connections=20, pool_maxsize=20)
TG_SESSION.mount("https://", _adapter)
TG_SESSION.mount("http://", _adapter)

REMOVE_KEYBOARD = json.dumps({"remove_keyboard": True})

def escape_html(text):
    """Safely escapes HTML characters to prevent Telegram formatting breakages."""
    if not text:
        return ""
    return html.escape(str(text))

def _sanitize_err(err_obj):
    s = str(err_obj)
    if BOT_TOKEN:
        s = s.replace(BOT_TOKEN, "[REDACTED_TOKEN]")
    return s

def _get_stream_positions(files):
    """Finds all seekable streams in files dictionary or iterable and records initial positions."""
    positions = []
    if not files:
        return positions

    if isinstance(files, dict):
        items = files.values()
    elif isinstance(files, (list, tuple)):
        items = files
    else:
        items = [files]

    for item in items:
        fileobj = None
        if hasattr(item, "seek") and hasattr(item, "tell"):
            fileobj = item
        elif isinstance(item, (tuple, list)):
            for part in item:
                if hasattr(part, "seek") and hasattr(part, "tell"):
                    fileobj = part
                    break
        if fileobj is not None:
            try:
                positions.append((fileobj, fileobj.tell()))
            except Exception:
                pass
    return positions


def _rewind_streams(positions):
    """Rewinds all recorded streams back to their starting position before retry."""
    for fileobj, pos in positions:
        try:
            fileobj.seek(pos)
        except Exception:
            pass


def tg(method, data=None, files=None, timeout=30, _depth=0):
    """Sends an API request to Telegram with automatic rate-limit backoff, transient drop retry, and error logging."""
    if _depth > 4:
        class DummyResp:
            ok = False
            status_code = 429
            text = "Max retries exceeded on 429"
            def json(self): return {"ok": False, "description": self.text}
        return DummyResp()

    cur_token = getattr(config, "BOT_TOKEN", BOT_TOKEN)
    if not cur_token:
        class DummyResp:
            ok = False
            status_code = 401
            text = "BOT_TOKEN is missing or empty"
            def json(self): return {"ok": False, "description": self.text}
        return DummyResp()

    url = f"https://api.telegram.org/bot{cur_token}/{method}"
    stream_positions = _get_stream_positions(files) if files else []
    max_network_retries = 3

    for attempt in range(max_network_retries + 1):
        if attempt > 0 and stream_positions:
            _rewind_streams(stream_positions)

        try:
            if files:
                resp = TG_SESSION.post(url, data=data, files=files, timeout=timeout)
            elif data:
                resp = TG_SESSION.post(url, data=data, timeout=timeout)
            else:
                resp = TG_SESSION.get(url, timeout=timeout)

            # Telegram Flood Control: 429 Too Many Requests
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 2)
                print(f"⚠️ Telegram Rate Limit hit: Sleeping for {retry_after}s...")
                time.sleep(retry_after + 0.5)
                if stream_positions:
                    _rewind_streams(stream_positions)
                return tg(method, data=data, files=files, timeout=timeout, _depth=_depth + 1)

            # Transient Telegram Gateway errors: 502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout
            if resp.status_code in (502, 503, 504) and attempt < max_network_retries:
                safe_text = _sanitize_err(resp.text)
                wait_sec = 1.5 * (attempt + 1)
                print(f"⚠️ Telegram API gateway error ({method} HTTP {resp.status_code}): {safe_text}. Retrying in {wait_sec:.1f}s (attempt {attempt + 1}/{max_network_retries})...")
                time.sleep(wait_sec)
                continue

            if resp.status_code != 200:
                safe_text = _sanitize_err(resp.text)
                print(f"⚠️ Telegram API response error ({method}): {resp.status_code} - {safe_text}")

            return resp

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            safe_err = _sanitize_err(e)
            if attempt < max_network_retries:
                wait_sec = 1.5 * (attempt + 1)
                print(f"⚠️ Telegram API transient connection drop ({method}: {type(e).__name__} - {safe_err}). Retrying in {wait_sec:.1f}s (attempt {attempt + 1}/{max_network_retries})...")
                time.sleep(wait_sec)
                continue
            print(f"❌ Telegram API exception ({method}) after {max_network_retries} retries: {safe_err}")
            class DummyResp:
                ok = False
                status_code = 500
                text = safe_err
                def json(self): return {"ok": False, "description": safe_err}
            return DummyResp()

        except Exception as e:
            safe_err = _sanitize_err(e)
            print(f"⚠️ Telegram API exception ({method}): {safe_err}")
            class DummyResp:
                ok = False
                status_code = 500
                text = safe_err
                def json(self): return {"ok": False, "description": safe_err}
            return DummyResp()


def send_msg(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = tg("sendMessage", data=payload)
    # Automatic fallback to plain text if HTML entity parsing failed
    if not resp.ok and parse_mode:
        payload.pop("parse_mode", None)
        resp = tg("sendMessage", data=payload)
    return resp

def send_photo_url(chat_id, photo_url, caption=None, parse_mode="HTML", reply_markup=None):
    payload = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = tg("sendPhoto", data=payload)
    if not resp.ok and parse_mode and caption:
        payload.pop("parse_mode", None)
        resp = tg("sendPhoto", data=payload)
    return resp

def delete_msg(chat_id, message_id):
    if not message_id:
        return
    try:
        tg("deleteMessage", data={"chat_id": chat_id, "message_id": message_id}, timeout=5)
    except Exception:
        pass

def edit_msg(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = tg("editMessageText", data=payload)
    if not resp.ok and parse_mode:
        payload.pop("parse_mode", None)
        resp = tg("editMessageText", data=payload)
    return resp


def set_bot_commands():
    """Auto-registers bot command menu: user commands for all, admin commands exclusively for OWNER_ID."""
    user_commands = [
        {"command": "start", "description": "Overview, status & quick start"},
        {"command": "mp3", "description": "Extract pristine MP3 audio from any link"},
        {"command": "whoami", "description": "Check your active plan & days remaining"},
        {"command": "mystats", "description": "View your total downloads & referral stats"},
        {"command": "refer", "description": "Get referral link & earn +7 Free Days"},
        {"command": "help", "description": "Supported apps & pro-saving tips"},
        {"command": "support", "description": "Contact admin directly inside bot"},
        {"command": "status", "description": "Server health & uptime metrics"},
        {"command": "ping", "description": "Check bot server response latency"},
    ]

    admin_commands = [
        {"command": "admin", "description": "🎛️ Interactive Admin Dashboard & Controls"},
        {"command": "users", "description": "👥 View all subscribers & usage breakdown"},
        {"command": "revenue", "description": "💰 Business analytics & monthly collections"},
        {"command": "health", "description": "🌡️ Phone server diagnostics (RAM/Disk/Temp)"},
        {"command": "cookies", "description": "🍪 Check FB/IG/YT cookie status & health"},
        {"command": "backup", "description": "💾 Download instant JSON database backup"},
        {"command": "banned", "description": "🚫 View list of all banned accounts"},
        {"command": "broadcast", "description": "📢 Broadcast announcement to all users"},
        {"command": "reply", "description": "💬 Reply to customer support ticket"},
        {"command": "ban", "description": "⛔ Ban abusive user (/ban <uid> [reason])"},
        {"command": "unban", "description": "✅ Unban user account (/unban <uid>)"},
        {"command": "start", "description": "Overview, status & quick start"},
        {"command": "help", "description": "View full command guide"},
    ]

    try:
        # 1. Default scope for all normal users
        tg("setMyCommands", data={
            "commands": json.dumps(user_commands),
            "scope": json.dumps({"type": "default"})
        }, timeout=10)

        # 2. Exclusive admin scope registered only for OWNER_ID chat
        if OWNER_ID:
            try:
                tg("setMyCommands", data={
                    "commands": json.dumps(admin_commands),
                    "scope": json.dumps({"type": "chat", "chat_id": int(OWNER_ID)})
                }, timeout=10)
                print("👑 Registered exclusive Admin commands menu for Owner chat")
            except Exception as e_adm:
                print(f"⚠️ Failed to register admin-scoped commands: {e_adm}")

        print("✅ Registered bot commands menu with Telegram")
    except Exception as e:
        print(f"⚠️ Failed to set bot commands: {e}")
