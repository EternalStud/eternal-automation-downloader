import os
import json
import time
import tempfile
import urllib.parse
from datetime import datetime
from config import (
    USERS_FILE, REMINDERS_FILE, STATE_FILE, OWNER_ID, UPI_ID,
    SUBSCRIPTION_PRICE_INR, FREE_TRIAL_LIMIT, FREE_MONTHLY_DOWNLOAD_LIMIT,
    FREE_MONTHLY_BYTES_LIMIT, FILE_LOCK, USERS_CACHE_LOCK
)
from telegram_api import send_msg, tg, escape_html


# -----------------------
# ATOMIC JSON PERSISTENCE
# -----------------------

def save_json(filepath, data):
    """Thread-safe, atomic JSON persistence using tempfile + os.replace."""
    with FILE_LOCK:
        try:
            dir_name = os.path.dirname(os.path.abspath(filepath))
            os.makedirs(dir_name, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(data, tf, indent=2, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, filepath)
        except Exception as e:
            print(f"⚠️ Failed atomic write to {filepath}: {e}")
            if "temp_name" in locals() and os.path.exists(temp_name):
                try: os.remove(temp_name)
                except Exception: pass

def load_json(filepath):
    """Safely loads JSON data from disk."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed loading JSON from {filepath}: {e}")
        return None

# -----------------------
# USER DATABASE & CACHING
# -----------------------

_USERS_CACHE = None
_USERS_CACHE_TIME = 0
USERS_CACHE_TTL = 3.0  # 3-second cache to minimize disk IO across threads

def invalidate_users_cache():
    global _USERS_CACHE, _USERS_CACHE_TIME
    with USERS_CACHE_LOCK:
        _USERS_CACHE = None
        _USERS_CACHE_TIME = 0

def load_users():
    """Loads users database with thread-safe short TTL caching and auto-owner initialization."""
    global _USERS_CACHE, _USERS_CACHE_TIME
    now = time.time()
    with USERS_CACHE_LOCK:
        if _USERS_CACHE is not None and (now - _USERS_CACHE_TIME) < USERS_CACHE_TTL:
            return _USERS_CACHE

    users = load_json(USERS_FILE)
    if not isinstance(users, dict):
        users = {}

    changed = False
    if OWNER_ID and OWNER_ID not in users:
        users[OWNER_ID] = {
            "name": "Bot Owner",
            "role": "owner",
            "subscribed_at": int(time.time()),
            "expires_at": None,
            "total_downloads": 0,
            "referral_count": 0
        }
        changed = True

    if changed:
        save_json(USERS_FILE, users)

    with USERS_CACHE_LOCK:
        _USERS_CACHE = users
        _USERS_CACHE_TIME = now
    return users

def save_users(users):
    """Atomically saves the users database."""
    save_json(USERS_FILE, users)
    invalidate_users_cache()

def is_user_banned(user_id, users=None):
    """Checks if a user has been banned by Admin."""
    if str(user_id) == str(OWNER_ID):
        return False
    if users is None:
        users = load_users()
    u = users.get(str(user_id), {})
    return u.get("role") == "banned" or u.get("is_banned", False)

def ban_user(user_id, reason="Spam / Abuse Violation"):
    """Bans a user from using the bot permanently until unbanned."""
    uid = str(user_id)
    if uid == str(OWNER_ID):
        return False, "Cannot ban the Admin / Owner."
    users = load_users()
    if uid not in users:
        users[uid] = {
            "name": f"User_{uid[-4:]}",
            "role": "banned",
            "is_banned": True,
            "banned_at": int(time.time()),
            "ban_reason": reason,
            "total_downloads": 0
        }
    else:
        users[uid]["role"] = "banned"
        users[uid]["is_banned"] = True
        users[uid]["banned_at"] = int(time.time())
        users[uid]["ban_reason"] = reason

    save_users(users)
    return True, f"User {uid} has been banned."

def unban_user(user_id):
    """Unbans a user and restores their account, preserving active subscription status."""
    uid = str(user_id)
    users = load_users()
    if uid not in users or not (users[uid].get("role") == "banned" or users[uid].get("is_banned")):
        return False, f"User {uid} is not banned."

    exp_ts = users[uid].get("expires_at")
    if exp_ts and exp_ts > time.time():
        users[uid]["role"] = "subscriber"
    else:
        users[uid]["role"] = "trial"

    users[uid]["is_banned"] = False
    users[uid].pop("banned_at", None)
    users[uid].pop("ban_reason", None)
    save_users(users)
    return True, f"User {uid} has been unbanned and restored."


def get_banned_users():
    """Returns a list of all banned users."""
    users = load_users()
    banned = []
    for uid, data in users.items():
        if data.get("role") == "banned" or data.get("is_banned"):
            banned.append({
                "user_id": uid,
                "name": data.get("name", "Unknown"),
                "banned_at": data.get("banned_at", 0),
                "reason": data.get("ban_reason", "Spam / Abuse")
            })
    return banned

def is_user_active(user_id, users=None):
    """
    Evaluates user status:
    Returns (is_active, role, days_left, name, trials_left)
    """
    if users is None:
        users = load_users()
    uid_str = str(user_id)
    if uid_str == OWNER_ID or (uid_str in users and users[uid_str].get("role") == "owner"):
        return True, "owner", 999, users.get(uid_str, {}).get("name", "Owner"), 999

    if is_user_banned(uid_str, users):
        return False, "banned", 0, users.get(uid_str, {}).get("name", "Banned User"), 0

    if uid_str not in users:
        return True, "trial", 0, "New Visitor", FREE_MONTHLY_DOWNLOAD_LIMIT


    u = users[uid_str]
    name = u.get("name", f"User_{uid_str[-4:]}")
    role = u.get("role", "trial")

    if role == "trial":
        current_month = datetime.now().strftime("%Y-%m")
        if u.get("free_month") != current_month:
            u["free_month"] = current_month
            u["trial_used"] = 0
            u["free_bytes_used"] = 0
            save_users(users)

        trial_used = u.get("trial_used", 0)
        bytes_used = u.get("free_bytes_used", 0)

        trials_left = max(0, FREE_MONTHLY_DOWNLOAD_LIMIT - trial_used)
        bytes_left = max(0, FREE_MONTHLY_BYTES_LIMIT - bytes_used)

        if trials_left > 0 and bytes_left > 0:
            return True, "trial", 0, name, trials_left
        return False, "trial", 0, name, 0

    exp = u.get("expires_at")
    if exp is None:
        return True, "subscriber", 999, name, 999

    now = int(time.time())
    if exp > now:
        days_left = max(1, (exp - now) // 86400)
        return True, "subscriber", days_left, name, 999

    return False, "subscriber", 0, name, 0

def record_user_download(user_id, file_size_bytes=0):
    """Atomically records user download and increments monthly bandwidth / quota counters."""
    users = load_users()
    uid = str(user_id)
    if uid in users:
        u = users[uid]
        u["total_downloads"] = u.get("total_downloads", 0) + 1
        current_month = datetime.now().strftime("%Y-%m")
        if u.get("free_month") != current_month:
            u["free_month"] = current_month
            u["trial_used"] = 0
            u["free_bytes_used"] = 0
        if u.get("role") == "trial":
            u["trial_used"] = u.get("trial_used", 0) + 1
            u["free_bytes_used"] = u.get("free_bytes_used", 0) + int(file_size_bytes)
        save_users(users)


def add_or_renew_subscription(user_id, days=30, name=None, payment_ref=None):
    """
    Activates or extends a subscriber plan.
    Automatically rewards referrer with +7 Free Days if this is a first-time subscriber!
    """
    users = load_users()
    uid = str(user_id)
    now = int(time.time())
    user_data = users.get(uid, {})

    curr_exp = user_data.get("expires_at")
    base_time = max(curr_exp, now) if (curr_exp and curr_exp > now) else now
    new_exp = base_time + (days * 86400)

    user_name = name or user_data.get("name", f"User_{uid[-4:]}")
    payments = user_data.get("payments", [])
    if payment_ref:
        payments.append({"date": now, "ref": payment_ref, "days": days, "amount": SUBSCRIPTION_PRICE_INR})

    referred_by = user_data.get("referred_by")
    was_trial = (user_data.get("role", "trial") == "trial")

    users[uid] = {
        "name": user_name,
        "role": "subscriber",
        "subscribed_at": user_data.get("subscribed_at", now),
        "expires_at": new_exp,
        "total_downloads": user_data.get("total_downloads", 0),
        "referral_count": user_data.get("referral_count", 0),
        "referred_by": referred_by,
        "payments": payments
    }

    # 🎁 REFERRAL REWARD ENGINE: +7 Free Days for referrer
    if was_trial and referred_by and str(referred_by) in users and str(referred_by) != uid:
        ref_uid = str(referred_by)
        ref_user = users[ref_uid]
        ref_user["referral_count"] = ref_user.get("referral_count", 0) + 1

        if ref_user.get("role") == "subscriber":
            ref_exp = ref_user.get("expires_at")
            ref_base = max(ref_exp, now) if (ref_exp and ref_exp > now) else now
            ref_user["expires_at"] = ref_base + (7 * 86400)
            print(f"🎁 Granted +7 Days Referral Bonus to Referrer {ref_uid} ({ref_user.get('name')})")

            send_msg(
                ref_uid,
                f"🎉 <b>Referral Bonus Reward!</b>\n\n"
                f"Your friend <b>*{user_name}*</b> just subscribed!\n"
                f"🎁 <b>*+7 Days of Free Unlimited Access*</b> have been added to your account.\n"
                f"👥 Total Friends Referred: <b>*{ref_user['referral_count']}*</b>\n\n"
                f"Thank you for sharing Eternal Automation!",
                parse_mode="HTML"
            )

    save_users(users)
    return users[uid]

def get_upi_qr_url(user_id, amount=20):
    """Generates dynamic UPI QR Code via UPI Intent Spec."""
    upi_uri = f"upi://pay?pa={UPI_ID}&pn=EternalAutomation&am={amount}&cu=INR&tn=Sub_{user_id}"
    encoded = urllib.parse.quote(upi_uri)
    return f"https://api.qrserver.com/v1/create-qr-code/?data={encoded}&size=400x400&margin=15", UPI_ID

def check_and_send_expiry_reminders():
    """Checks subscriber expiration dates and sends automated 3-day, 1-day, and expired notifications."""
    users = load_users()
    reminders = load_json(REMINDERS_FILE) or {}

    now = int(time.time())
    today_date = datetime.now().strftime("%Y-%m-%d")

    for uid, data in users.items():
        if data.get("role") == "owner" or str(uid) == OWNER_ID:
            continue

        exp_ts = data.get("expires_at")
        if not exp_ts:
            continue

        user_name = escape_html(data.get("name", "Subscriber"))
        seconds_left = exp_ts - now

        rem_key = None
        rem_msg = None

        if 2 * 86400 < seconds_left <= 3 * 86400:
            rem_key = f"{uid}<i>3d</i>{today_date}"
            rem_msg = (
                f"⏳ <b>Subscription Renewal Notice</b>\n\n"
                f"Hello {user_name}! Your subscription expires in <b>*3 Days*</b>.\n\n"
                f"💎 Renew now for just ₹{SUBSCRIPTION_PRICE_INR}/month to keep uninterrupted HD downloads 24/7!\n"
                f"💳 UPI ID: <code>{UPI_ID}</code> <b>(Tap to copy)</b>"
            )
        elif 0 < seconds_left <= 1 * 86400:
            rem_key = f"{uid}<i>1d</i>{today_date}"
            rem_msg = (
                f"⚠️ <b>Final Reminder: 1 Day Left!</b> ⚠️\n\n"
                f"Hello {user_name}! Your unlimited download access expires in <b>*24 Hours*</b>.\n\n"
                f"💎 Send ₹{SUBSCRIPTION_PRICE_INR} to <code>{UPI_ID}</code> to stay active!"
            )
        elif -1 * 86400 <= seconds_left <= 0:
            rem_key = f"{uid}<i>expired</i>{today_date}"
            rem_msg = (
                f"🔴 <b>Subscription Expired Today</b>\n\n"
                f"Your unlimited access has expired. Send any link to get your renewal QR code and reactivate in 1 minute!"
            )

        if rem_key and rem_key not in reminders:
            print(f"📢 Sending expiry reminder to {uid} ({data.get('name')})")
            send_msg(uid, rem_msg, parse_mode="HTML")
            reminders[rem_key] = now
            save_json(REMINDERS_FILE, reminders)

def send_backup_to_owner():
    """Exports and delivers latest database snapshot to Owner chat."""
    if not os.path.exists(USERS_FILE):
        return
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        caption = f"💾 <b>Automated Database Backup</b>\n📅 Date: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n👥 Users: {len(load_users())}"
        with open(USERS_FILE, "rb") as f:
            tg(
                "sendDocument",
                data={"chat_id": OWNER_ID, "caption": caption, "parse_mode": "Markdown"},
                files={"document": (f"users_backup_{timestamp}.json", f, "application/json")},
                timeout=60
            )
        print("✅ Dispatched database backup to Owner chat")
    except Exception as e:
        print(f"⚠️ Error sending backup to owner: {e}")
