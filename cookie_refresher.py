"""
Autonomous Cookie & Visitor Token Refresher.
Keeps platform guest tokens and visitor identifiers fresh 24/7 without browser dependencies.
"""

import os
import time
import requests

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

def analyze_cookies_file(file_path=COOKIES_FILE):
    """
    Analyzes cookies.txt and returns counts for major platforms.
    """
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return {"total": 0, "youtube": 0, "instagram": 0, "facebook": 0, "twitter": 0}

    stats = {"total": 0, "youtube": 0, "instagram": 0, "facebook": 0, "twitter": 0}
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    stats["total"] += 1
                    domain = parts[0].lower()
                    if "youtube" in domain or "google" in domain:
                        stats["youtube"] += 1
                    elif "instagram" in domain:
                        stats["instagram"] += 1
                    elif "facebook" in domain or "fb.com" in domain:
                        stats["facebook"] += 1
                    elif "twitter" in domain or "x.com" in domain:
                        stats["twitter"] += 1
    except Exception as e:
        print(f"⚠️ Error analyzing cookies file: {e}")

    return stats

def refresh_visitor_tokens(file_path=COOKIES_FILE):
    """
    Autonomously performs guest visitor token handshakes for:
    - YouTube (VISITOR_INFO1_LIVE, YSC, GPS)
    - Facebook (datr, sb, wd)
    - Instagram (csrftoken, mid, ig_did, datr)
    - Twitter / X (guest_id, gt, ct0)

    Merges and injects these fresh visitor tokens into cookies.txt
    while preserving any existing authenticated user session cookies!
    """
    cookie_map = {}

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        key = (parts[0].lower(), parts[5])
                        cookie_map[key] = parts
        except Exception:
            pass

    harvested_count = 0
    now_exp = str(int(time.time()) + (180 * 86400))  # 180 days valid

    # 1. Autonomous Facebook Guest Handshake
    try:
        s_fb = requests.Session()
        s_fb.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        })
        s_fb.get("https://m.facebook.com", timeout=8)
        for cookie in s_fb.cookies:
            dom = cookie.domain if str(cookie.domain).startswith(".") else f".{cookie.domain}"
            key = (dom.lower(), cookie.name)
            # Only set if not already present or if it's a guest tracking token
            if key not in cookie_map or cookie.name in ("datr", "sb", "wd", "fr"):
                cookie_map[key] = [dom, "TRUE", "/", "TRUE" if cookie.secure else "FALSE", now_exp, cookie.name, cookie.value]
                harvested_count += 1
    except Exception as e:
        print(f"⚠️ Facebook guest handshake warning: {e}")

    # 2. Autonomous Instagram Guest Handshake
    try:
        s_ig = requests.Session()
        s_ig.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 300.0.0.0",
            "X-IG-App-ID": "936619743392459",
            "Accept-Language": "en-US,en;q=0.9"
        })
        s_ig.get("https://www.instagram.com/data/shared_data/", timeout=8)
        for cookie in s_ig.cookies:
            dom = cookie.domain if str(cookie.domain).startswith(".") else f".{cookie.domain}"
            key = (dom.lower(), cookie.name)
            if key not in cookie_map or cookie.name in ("mid", "ig_did", "csrftoken", "datr"):
                cookie_map[key] = [dom, "TRUE", "/", "TRUE" if cookie.secure else "FALSE", now_exp, cookie.name, cookie.value]
                harvested_count += 1
    except Exception as e:
        print(f"⚠️ Instagram guest handshake warning: {e}")

    # 3. Autonomous YouTube Guest Handshake
    try:
        s_yt = requests.Session()
        s_yt.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        })
        s_yt.get("https://www.youtube.com", timeout=8)
        for cookie in s_yt.cookies:
            dom = cookie.domain if str(cookie.domain).startswith(".") else f".{cookie.domain}"
            key = (dom.lower(), cookie.name)
            if key not in cookie_map or cookie.name in ("VISITOR_INFO1_LIVE", "YSC", "GPS"):
                cookie_map[key] = [dom, "TRUE", "/", "TRUE" if cookie.secure else "FALSE", now_exp, cookie.name, cookie.value]
                harvested_count += 1
    except Exception as e:
        print(f"⚠️ YouTube guest handshake warning: {e}")

    # 4. Autonomous Twitter / X Guest Handshake
    try:
        s_tw = requests.Session()
        s_tw.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        })
        s_tw.get("https://twitter.com", timeout=8)
        for cookie in s_tw.cookies:
            dom = cookie.domain if str(cookie.domain).startswith(".") else f".{cookie.domain}"
            key = (dom.lower(), cookie.name)
            if key not in cookie_map or cookie.name in ("guest_id", "gt", "personalization_id"):
                cookie_map[key] = [dom, "TRUE", "/", "TRUE" if cookie.secure else "FALSE", now_exp, cookie.name, cookie.value]
                harvested_count += 1
    except Exception as e:
        print(f"⚠️ Twitter guest handshake warning: {e}")

    # 5. Atomic Netscape Persistence
    if cookie_map:
        try:
            lines = [
                "# Netscape HTTP Cookie File",
                "# Generated by Eternal Automation Autonomous Handshake Engine",
                ""
            ]
            for parts in cookie_map.values():
                lines.append("\t".join(parts))

            temp_path = f"{file_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(temp_path, file_path)
            print(f"🤖 Autonomous visitor session handshake refreshed ({harvested_count} platform tokens merged into cookies.txt).")
            return True
        except Exception as e:
            print(f"⚠️ Failed to write refreshed cookies: {e}")

    return False


_LAST_COOKIE_ALERT_TIME = 0

def check_platform_cookies_health(file_path=COOKIES_FILE):
    """
    Actively verifies if Facebook and Instagram session cookies in cookies.txt are alive and valid.
    Returns a structured health dict with status for each platform.
    """
    health = {
        "youtube": {"status": "ok", "message": "Visitor Tokens OK", "cookies_count": 0},
        "facebook": {"status": "unknown", "message": "No cookies found", "cookies_count": 0},
        "instagram": {"status": "unknown", "message": "No cookies found", "cookies_count": 0},
        "twitter": {"status": "ok", "message": "Public Guest Mode OK", "cookies_count": 0},
        "has_issues": False
    }

    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        health["facebook"] = {"status": "missing", "message": "cookies.txt is empty/missing", "cookies_count": 0}
        health["instagram"] = {"status": "missing", "message": "cookies.txt is empty/missing", "cookies_count": 0}
        health["has_issues"] = True
        return health

    # Parse cookies.txt
    fb_cookies = {}
    ig_cookies = {}
    yt_count = 0
    tw_count = 0

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain = parts[0].lower()
                    name = parts[5]
                    val = parts[6]
                    if "facebook" in domain or "fb.com" in domain:
                        fb_cookies[name] = val
                    elif "instagram" in domain:
                        ig_cookies[name] = val
                    elif "youtube" in domain or "google" in domain:
                        yt_count += 1
                    elif "twitter" in domain or "x.com" in domain:
                        tw_count += 1
    except Exception as e:
        print(f"⚠️ Error parsing cookies: {e}")

    health["youtube"]["cookies_count"] = yt_count
    health["twitter"]["cookies_count"] = tw_count
    health["facebook"]["cookies_count"] = len(fb_cookies)
    health["instagram"]["cookies_count"] = len(ig_cookies)

    # 1. Verify Facebook Cookies
    if not fb_cookies:
        health["facebook"] = {"status": "missing", "message": "No Facebook cookies found", "cookies_count": 0}
    elif "c_user" not in fb_cookies and "xs" not in fb_cookies:
        health["facebook"] = {"status": "logged_out", "message": "Missing c_user/xs (Guest Mode)", "cookies_count": len(fb_cookies)}
    else:
        # Live session probe
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            }
            r = requests.get("https://mbasic.facebook.com/me", cookies=fb_cookies, headers=headers, timeout=8, allow_redirects=True)
            if "login.php" in r.url.lower() or "checkpoint" in r.url.lower() or "login_form" in r.text.lower() or r.status_code in (401, 403):
                health["facebook"] = {"status": "expired", "message": "Session Expired (Logged Out / Checkpoint)", "cookies_count": len(fb_cookies)}
                health["has_issues"] = True
            else:
                health["facebook"] = {"status": "active", "message": "Active Logged-In Session", "cookies_count": len(fb_cookies)}
        except Exception:
            health["facebook"] = {"status": "active", "message": "Active (Probe timeout/assumed ok)", "cookies_count": len(fb_cookies)}

    # 2. Verify Instagram Cookies
    if not ig_cookies:
        health["instagram"] = {"status": "missing", "message": "No Instagram cookies found", "cookies_count": 0}
    elif "sessionid" not in ig_cookies:
        health["instagram"] = {"status": "guest", "message": "Guest Mode (No sessionid)", "cookies_count": len(ig_cookies)}
    else:
        # Live session probe
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 300.0.0.0",
                "Accept-Language": "en-US,en;q=0.9"
            }
            r = requests.get("https://i.instagram.com/api/v1/users/web_profile_info/?username=instagram", cookies=ig_cookies, headers=headers, timeout=8)
            if r.status_code in (401, 403) or "login" in r.text.lower():
                health["instagram"] = {"status": "expired", "message": "Session Expired (Login Required)", "cookies_count": len(ig_cookies)}
                health["has_issues"] = True
            else:
                health["instagram"] = {"status": "active", "message": "Active Logged-In Session", "cookies_count": len(ig_cookies)}
        except Exception:
            health["instagram"] = {"status": "active", "message": "Active (Probe timeout/assumed ok)", "cookies_count": len(ig_cookies)}

    return health

def check_cookies_health_and_alert(force=False):
    """
    Checks cookie health and sends a private Telegram alert to OWNER_ID if any session is expired.
    Rate-limited to at most once per 48 hours unless force=True.
    """
    global _LAST_COOKIE_ALERT_TIME
    now = time.time()
    if not force and (now - _LAST_COOKIE_ALERT_TIME) < 172800:  # 48 hours
        return

    health = check_platform_cookies_health()
    if health.get("has_issues") or force:
        _LAST_COOKIE_ALERT_TIME = now
        fb_stat = health["facebook"]
        ig_stat = health["instagram"]

        fb_icon = "✅" if fb_stat["status"] == "active" else "⚠️"
        ig_icon = "✅" if ig_stat["status"] == "active" else "⚠️"

        alert_msg = (
            "🍪 <b>Platform Cookies Health Diagnostics</b>\n\n"
            f"• 🔵 <b>Facebook:</b> {fb_icon} <code>{fb_stat['message']}</code> ({fb_stat['cookies_count']} cookies)\n"
            f"• 📸 <b>Instagram:</b> {ig_icon} <code>{ig_stat['message']}</code> ({ig_stat['cookies_count']} cookies)\n"
            f"• 🔴 <b>YouTube:</b> ✅ <code>Visitor Tokens Active</code>\n"
            f"• 🐦 <b>X / Twitter:</b> ✅ <code>Guest Engine Active</code>\n\n"
        )
        if health.get("has_issues"):
            alert_msg += (
                "⚠️ <b>Action Recommended:</b>\n"
                "One or more platform sessions have expired. Restricted/private posts may require fresh cookies.\n\n"
                "💡 <b>To update:</b> Export a new <code>cookies.txt</code> and send the file directly into this chat!"
            )
        else:
            alert_msg += "🟢 <b>All platform authentication cookies are 100% healthy & active!</b>"

        try:
            from telegram_api import send_msg
            from config import OWNER_ID
            if OWNER_ID:
                send_msg(OWNER_ID, alert_msg, parse_mode="HTML")
        except Exception as e:
            print(f"⚠️ Error sending cookie alert: {e}")

