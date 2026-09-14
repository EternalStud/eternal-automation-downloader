#!/usr/bin/env bash
# ==========================================================
# 🤖 EternalAutomationBot — Resilient 24/7 Watchdog Runner
# Standardized Signal-Aware Watchdog with Clean Shutdown Trapping
# ==========================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOG_FILE="bot.log"
MAX_LOG_SIZE=5242880  # 5MB
SENTINEL_SHUTDOWN="${HOME:-/data/data/com.termux/files/home}/.shutdown_in_progress"

rotate_log() {
    if [ -f "$LOG_FILE" ]; then
        FILE_SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$FILE_SIZE" -gt "$MAX_LOG_SIZE" ]; then
            mv "$LOG_FILE" "${LOG_FILE}.old"
            echo "🔄 [$(date '+%Y-%m-%d %H:%M:%S')] Log rotated (exceeded 5MB)" > "$LOG_FILE"
        fi
    fi
}

STOP_REQUESTED=0
CHILD_PID=0

int_handler() {
    echo "🛑 [$(date '+%Y-%m-%d %H:%M:%S')] Watchdog received SIGINT. Forwarding to child..." >> "$LOG_FILE"
    STOP_REQUESTED=1
    if [ "$CHILD_PID" -ne 0 ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -INT "$CHILD_PID" 2>/dev/null
    fi
}

term_handler() {
    echo "🛑 [$(date '+%Y-%m-%d %H:%M:%S')] Watchdog received SIGTERM. Forwarding to child..." >> "$LOG_FILE"
    STOP_REQUESTED=1
    if [ "$CHILD_PID" -ne 0 ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -TERM "$CHILD_PID" 2>/dev/null
    fi
}

hup_handler() {
    echo "🛑 [$(date '+%Y-%m-%d %H:%M:%S')] Watchdog received SIGHUP. Forwarding to child..." >> "$LOG_FILE"
    STOP_REQUESTED=1
    if [ "$CHILD_PID" -ne 0 ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill -HUP "$CHILD_PID" 2>/dev/null
    fi
}

trap int_handler SIGINT
trap term_handler SIGTERM
trap hup_handler SIGHUP

echo "=========================================================="
echo "🚀 Starting EternalAutomationBot Engine (24/7 Watchdog)..."
echo "=========================================================="

PY_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
    PY_BIN="python"
fi

while [ "$STOP_REQUESTED" -eq 0 ]; do
    if [ -f "$SENTINEL_SHUTDOWN" ]; then
        echo "🛑 [$(date '+%Y-%m-%d %H:%M:%S')] Shutdown sentinel detected ($SENTINEL_SHUTDOWN). Exiting watchdog loop." >> "$LOG_FILE"
        break
    fi

    rotate_log
    echo "⚡ [$(date '+%Y-%m-%d %H:%M:%S')] Starting $PY_BIN main.py..." >> "$LOG_FILE"
    $PY_BIN -u main.py >> "$LOG_FILE" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" 2>/dev/null
    EXIT_CODE=$?
    CHILD_PID=0

    # Clean termination: signal received, code 0 (clean), 130 (SIGINT), 143 (SIGTERM), or shutdown sentinel exists
    if [ "$STOP_REQUESTED" -eq 1 ] || [ "$EXIT_CODE" -eq 0 ] || [ "$EXIT_CODE" -eq 130 ] || [ "$EXIT_CODE" -eq 143 ] || [ -f "$SENTINEL_SHUTDOWN" ]; then
        echo "🛑 [$(date '+%Y-%m-%d %H:%M:%S')] Clean termination (Exit Code: $EXIT_CODE). Watchdog exiting." >> "$LOG_FILE"
        exit 0
    fi

    echo "⚠️ [$(date '+%Y-%m-%d %H:%M:%S')] Process exited (Code: $EXIT_CODE). Restarting in 2s..." >> "$LOG_FILE"
    sleep 2
done

exit 0
