import os
import re
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

# -----------------------
# ENVIRONMENT & CONSTANTS
# -----------------------

def load_env():
    """Auto-load variables from .env if present without requiring external libraries."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key:
                        os.environ[key] = val
            print("📄 Auto-loaded settings from .env")
        except Exception as e:
            print(f"⚠️ Warning loading .env: {e}")

load_env()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN is missing. Set it in .env or environment variables.")

API_ID = int(os.environ.get("API_ID", "0")) if os.environ.get("API_ID", "").strip().isdigit() else 0
API_HASH = os.environ.get("API_HASH", "").strip()
USER_SESSION_STRING = os.environ.get("USER_SESSION_STRING", "").strip()

OWNER_ID = os.environ.get("OWNER_ID", "").strip()
UPI_ID = os.environ.get("UPI_ID", "").strip()
ADMIN_HANDLE = os.environ.get("ADMIN_HANDLE", "@YourAdminHandle").strip()
SUBSCRIPTION_PRICE_INR = 20
FREE_MONTHLY_DOWNLOAD_LIMIT = 10
FREE_MONTHLY_BYTES_LIMIT = 1024 * 1024 * 1024  # 1.0 GB
FREE_TRIAL_LIMIT = FREE_MONTHLY_DOWNLOAD_LIMIT
MAX_DOWNLOADS_PER_HOUR = 20

# -----------------------
# INTELLIGENT DUAL-LANE LIMITS
# -----------------------
FAST_LANE_MAX_BYTES = 50 * 1024 * 1024     # 50 MB: Unlimited fast processing for paid users
PAID_HEAVY_MAX_BYTES = 2000 * 1024 * 1024  # 2 GB: Serialized queue for paid users
OWNER_MAX_BYTES = 4000 * 1024 * 1024       # 4 GB: Priority processing for Telegram Premium Owner
FREE_TRIAL_MAX_BYTES = 50 * 1024 * 1024    # 50 MB: Safe limit for free trials
MAX_TELEGRAM_SIZE_BYTES = 4000 * 1024 * 1024


IS_GITHUB_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))


# -----------------------
# PATHS & RUNTIME FILES
# -----------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
REMINDERS_FILE = os.path.join(BASE_DIR, "reminders.json")
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")

# Thread safety locks
FILE_LOCK = threading.Lock()
USERS_CACHE_LOCK = threading.Lock()

# Worker thread pool
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=8)


# -----------------------
# COOKIES INITIALIZATION
# -----------------------

fb_cookies = os.environ.get("FACEBOOK_COOKIES_TXT", "").strip()
yt_cookies = (os.environ.get("YOUTUBE_COOKIES", "") or os.environ.get("YOUTUBE_COOKIES_TXT", "")).strip()
gen_cookies = os.environ.get("COOKIES_TXT", "").strip()

all_cookies = "\n".join(filter(None, [fb_cookies, yt_cookies, gen_cookies])).strip()
if all_cookies:
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(all_cookies)
        print("🍪 Loaded cookies into cookies.txt")
    except Exception as e:
        print(f"⚠️ Failed to write cookies.txt: {e}")

# -----------------------
# PLATFORMS & DOMAINS
# -----------------------

TRUSTED_DOMAINS = frozenset([
    "youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "twitter.com", "x.com", "mobile.twitter.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
    "reddit.com", "www.reddit.com", "old.reddit.com", "redd.it",
    "threads.net", "www.threads.net",
    "pinterest.com", "www.pinterest.com", "pin.it", "in.pinterest.com", "i.pinimg.com",
    "soundcloud.com", "www.soundcloud.com",
    "snapchat.com", "www.snapchat.com",
])

URL_REGEX = re.compile(
    r'((?:https?://|intent://|www\.)[^\s<>"]+|(?:\b(?:instagram\.com|youtube\.com|youtu\.be|facebook\.com|tiktok\.com|twitter\.com|x\.com|pinterest\.com|pin\.it)/[^\s<>"]+))',
    re.IGNORECASE
)

# -----------------------
# RUNTIME VALIDATION
# -----------------------

def validate_runtime_environment():
    """Checks for critical and recommended runtime binaries on startup."""
    diagnostics = {}
    tools = ["ffmpeg", "ffprobe", "node", "tmux", "termux-battery-status"]
    for tool in tools:
        path = shutil.which(tool)
        diagnostics[tool] = bool(path)

    print("🔍 Runtime Environment Diagnostics:")
    print(f"  • FFmpeg: {'✅ Available' if diagnostics['ffmpeg'] else '⚠️ Not Found (Recommended for video muxing)'}")
    print(f"  • Node.js: {'✅ Available' if diagnostics['node'] else '⚠️ Not Found (Optional for YouTube JS signature solver)'}")
    print(f"  • Tmux: {'✅ Available' if diagnostics['tmux'] else 'ℹ️ Not in PATH'}")
    print(f"  • Termux API: {'✅ Active' if diagnostics['termux-battery-status'] else 'ℹ️ Termux:API not installed (using sysfs fallback)'}")
    return diagnostics
