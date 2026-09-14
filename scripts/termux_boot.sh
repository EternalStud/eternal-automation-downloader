#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# 📱 Samsung Galaxy M31 — 24/7 Phone Server Autonomous Boot Recovery Suite
# Termux:Boot Startup Entrypoint (~/.termux/boot/01-phone-server-boot.sh)
# ==============================================================================

# 1. Environment & Path Initialization
export HOME="${HOME:-/data/data/com.termux/files/home}"
export PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME}"
export PATH="${PATH:+$PATH:}$PREFIX/bin:/usr/local/bin:/usr/bin:/bin"
export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TMPDIR="${TMPDIR:-$PREFIX/tmp}"
export LANG="${LANG:-en_US.UTF-8}"

BOOT_LOG="${BOOT_LOG:-$HOME/boot.log}"

# Redirect stdout and stderr to boot log
mkdir -p "$(dirname "$BOOT_LOG")" 2>/dev/null || true
exec >> "$BOOT_LOG" 2>&1

echo "=========================================================="
echo "🚀 [$(date '+%Y-%m-%d %H:%M:%S')] Starting Termux:Boot Recovery Suite"
echo "=========================================================="

# 2. Acquire CPU Wake Lock Immediately
echo "⚡ Acquiring Termux CPU Wake Lock..."
WAKE_LOCK_CMD="${TERMUX_WAKE_LOCK_BIN:-termux-wake-lock}"
if [ "$WAKE_LOCK_CMD" != "none" ] && command -v "$WAKE_LOCK_CMD" >/dev/null 2>&1; then
    "$WAKE_LOCK_CMD" && echo "✅ termux-wake-lock acquired successfully." || echo "⚠️ Warning: termux-wake-lock exited with non-zero code."
else
    echo "ℹ️ termux-wake-lock not found in PATH. Skipping wake lock acquisition."
fi

# 3. Ensure Cron Daemon is Running
echo "⏰ Checking Cron Daemon (crond)..."
if pgrep crond >/dev/null 2>&1; then
    echo "✅ crond daemon is already running."
elif command -v crond >/dev/null 2>&1; then
    echo "⏰ Starting crond daemon..."
    crond && echo "✅ crond daemon started." || echo "⚠️ Warning: crond failed to start."
else
    echo "ℹ️ crond binary not installed in PATH. Skipping."
fi

# 3.1. Ensure SSH Daemon is Running
echo "🔑 Checking SSH Server (sshd)..."
if pgrep sshd >/dev/null 2>&1; then
    echo "✅ sshd is already running."
elif command -v sshd >/dev/null 2>&1; then
    echo "🔑 Starting sshd..."
    sshd && echo "✅ sshd daemon started." || echo "⚠️ Warning: sshd failed to start."
else
    echo "ℹ️ sshd binary not installed in PATH. Skipping."
fi

# 3.2. Ensure Cloudflare Tunnel Daemon is Running
echo "☁️ Checking Cloudflare Tunnel (cloudflared)..."
if pgrep cloudflared >/dev/null 2>&1; then
    echo "✅ cloudflared is already running."
elif [ -f "$HOME/.cloudflared/token" ] && command -v cloudflared >/dev/null 2>&1; then
    echo "☁️ Starting cloudflared tunnel daemon..."
    nohup cloudflared tunnel run --token-file "$HOME/.cloudflared/token" >> "$HOME/cloudflared.log" 2>&1 &
    echo "✅ cloudflared daemon started."
else
    echo "ℹ️ cloudflared token or binary not found. Skipping."
fi

# 4. Layered 4-Tier Network Connectivity Poller (120s timeout)
check_tier1_socket() {
    # Tier 1: Socket level raw IP check (1.1.1.1:53 or 8.8.8.8:53)
    (timeout 2 bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/53' 2>/dev/null) || \
    (timeout 2 bash -c 'cat < /dev/null > /dev/tcp/8.8.8.8/53' 2>/dev/null) || \
    (command -v nc >/dev/null 2>&1 && (nc -z -w 2 1.1.1.1 53 2>/dev/null || nc -z -w 2 8.8.8.8 53 2>/dev/null)) || \
    (python3 -c "import socket; s = socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 53)); s.close()" 2>/dev/null)
}

check_tier2_dns() {
    # Tier 2: DNS resolution for api.telegram.org
    nslookup api.telegram.org >/dev/null 2>&1 || \
    host api.telegram.org >/dev/null 2>&1 || \
    getent hosts api.telegram.org >/dev/null 2>&1 || \
    python3 -c "import socket; socket.gethostbyname('api.telegram.org')" 2>/dev/null
}

check_tier3_http() {
    # Tier 3: HTTP/HTTPS Status 204 or API endpoint
    if command -v curl >/dev/null 2>&1; then
        HTTP_CODE=$(curl -s -m 3 -o /dev/null -w "%{http_code}" https://api.telegram.org 2>/dev/null || echo "000")
        if [[ "$HTTP_CODE" =~ ^[234] ]]; then
            return 0
        fi
        curl -s -m 3 --head http://clients3.google.com/generate_204 >/dev/null 2>&1 && return 0
    fi
    python3 -c "import urllib.request; urllib.request.urlopen('https://api.telegram.org', timeout=3)" 2>/dev/null
}

check_tier4_ping() {
    # Tier 4: ICMP Ping check
    ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 || \
    ping -c 1 -w 2 1.1.1.1 >/dev/null 2>&1 || \
    ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1 || \
    ping -c 1 -w 2 8.8.8.8 >/dev/null 2>&1
}

poll_network() {
    echo "📡 [4-Tier Network Poller] Waiting for network connectivity..."
    MAX_WAIT="${BOOT_NETWORK_TIMEOUT:-120}"
    WAITED=0
    ONLINE=0

    # If mock network mode is active for offline testing
    if [ "$BOOT_MOCK_NETWORK" = "offline" ]; then
        echo "⏳ [Mock Mode] Simulating offline network..."
        sleep "$MAX_WAIT"
        echo "⚠️ Network wait timed out after ${MAX_WAIT}s. Proceeding with service startup."
        return 0
    fi

    while [ "$WAITED" -lt "$MAX_WAIT" ]; do
        if check_tier1_socket || check_tier2_dns || check_tier3_http || check_tier4_ping; then
            echo "✅ Network is ONLINE (Waited ${WAITED}s)"
            ONLINE=1
            break
        fi
        echo "⏳ [${WAITED}s/${MAX_WAIT}s] Waiting for network connection..."
        sleep 2
        WAITED=$((WAITED + 2))
    done

    if [ "$ONLINE" -ne 1 ]; then
        echo "⚠️ Network wait timed out after ${MAX_WAIT}s. Proceeding with service startup."
    fi
}

poll_network

# 5. Clean Orphaned .lock and .tmp Files
echo "🧹 Cleaning orphaned lock and temporary files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"

if [ -d "$PROJECT_ROOT/eternal-automation-bot" ]; then
    BOT_BASE="$PROJECT_ROOT"
elif [ -d "$HOME/eternal-automation-bot" ]; then
    BOT_BASE="$HOME"
elif [ -d "$PARENT_DIR/eternal-automation-bot" ]; then
    BOT_BASE="$PARENT_DIR"
else
    BOT_BASE="$PROJECT_ROOT"
fi

rm -rf "$BOT_BASE"/eternal-*/downloads/* 2>/dev/null || true
rm -f "$BOT_BASE"/eternal-*/*.lock 2>/dev/null || true
rm -f "$BOT_BASE"/eternal-*/.*.lock 2>/dev/null || true
rm -f "$BOT_BASE"/eternal-*/*.tmp 2>/dev/null || true
rm -f "$BOT_BASE"/eternal-*/.*.tmp 2>/dev/null || true
rm -f "$HOME/.shutdown_in_progress" 2>/dev/null || true
echo "✅ Stale lock & temp cleanup complete."

# 6. Spawn Multi-Bot Tmux Sessions
echo "🤖 Spawning multi-bot tmux sessions..."
spawn_bot_session() {
    local session_name="$1"
    local bot_dir="$2"
    local runner_cmd="${3:-bash start.sh}"

    if [ -d "$bot_dir" ]; then
        if tmux has-session -t "$session_name" 2>/dev/null; then
            echo "ℹ️ Tmux session '$session_name' is already active."
        else
            echo "🚀 Spawning tmux session '$session_name' in $bot_dir..."
            tmux new-session -d -s "$session_name" "cd '$bot_dir' && $runner_cmd"
            if tmux has-session -t "$session_name" 2>/dev/null; then
                echo "✅ Tmux session '$session_name' started successfully."
            else
                echo "⚠️ Failed to spawn tmux session '$session_name'."
            fi
        fi
    else
        echo "ℹ️ Directory $bot_dir does not exist. Skipping session '$session_name'."
    fi
}

if command -v tmux >/dev/null 2>&1; then
    spawn_bot_session "bot" "$BOT_BASE/eternal-automation-bot" "bash start.sh"
    spawn_bot_session "feed-bot" "$BOT_BASE/eternal-feed-bot" "bash start.sh"
    spawn_bot_session "heartcast" "$BOT_BASE/eternal-heartcast-bot" "bash start.sh"
else
    echo "ℹ️ tmux command not found in PATH. Skipping tmux session spawning."
fi

# 7. Post-Boot Health Verification & Notification
echo "📊 Invoking post-boot health verification & Telegram notification..."
POST_NOTIFY_SCRIPT="$BOT_BASE/eternal-automation-bot/scripts/post_boot_notify.py"
if [ ! -f "$POST_NOTIFY_SCRIPT" ] && [ -f "$REPO_DIR/scripts/post_boot_notify.py" ]; then
    POST_NOTIFY_SCRIPT="$REPO_DIR/scripts/post_boot_notify.py"
fi

if [ "$BOOT_SKIP_NOTIFY" != "1" ] && [ -f "$POST_NOTIFY_SCRIPT" ]; then
    PY_BIN="python3"
    command -v python3 >/dev/null 2>&1 || PY_BIN="python"
    $PY_BIN "$POST_NOTIFY_SCRIPT" 2>&1 || echo "⚠️ Warning: post_boot_notify.py exited with non-zero code."
else
    echo "ℹ️ post_boot_notify.py not found or skipped. Skipping notification."
fi

echo "=========================================================="
echo "✨ [$(date '+%Y-%m-%d %H:%M:%S')] Termux:Boot Recovery Suite Completed Successfully."
echo "=========================================================="
exit 0
