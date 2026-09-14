#!/usr/bin/env bash
# ==============================================================================
# Pre-Reboot Graceful Multi-Bot Shutdown & State Synchronization Orchestrator
# Compatible with Android / Termux on Samsung Galaxy Phone Server
# ==============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_AUTO_DIR="${DEFAULT_AUTO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Configuration & Defaults
TIMEOUT=10
DRY_RUN=0
FORCED_KILL=0
DB_ERROR=0

SENTINEL_FILE="${SHUTDOWN_SENTINEL:-$HOME/.shutdown_in_progress}"
LOG_FILE="${SHUTDOWN_LOG:-$HOME/shutdown.log}"
TMUX_SESSIONS=("bot" "feed-bot" "heartcast")

# Parse CLI Arguments
print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -t, --timeout <seconds>   Graceful drain timeout (default: 10)"
    echo "  -d, --dry-run             Simulate shutdown sequence without stopping processes or modifying state"
    echo "  -h, --help                Show this help message"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--timeout)
            if [[ -n "${2:-}" && "$2" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
                TIMEOUT="$2"
                shift 2
            else
                echo "❌ Error: --timeout requires a numeric argument" >&2
                exit 1
            fi
            ;;
        -d|--dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "❌ Unknown argument: $1" >&2
            print_usage
            exit 1
            ;;
    esac
done

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo -e "$msg"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
        echo -e "$msg" >> "$LOG_FILE" 2>/dev/null || true
    fi
}

log "================================================================="
log "🛑 INITIATING PRE-REBOOT MULTI-BOT SHUTDOWN SEQUENCE"
log "================================================================="
log "ℹ️ Timeout: ${TIMEOUT}s | Dry Run: ${DRY_RUN}"

# Sentinel Cleanup Trap
cleanup_sentinel() {
    if [[ "$DRY_RUN" -eq 0 && -f "$SENTINEL_FILE" ]]; then
        rm -f "$SENTINEL_FILE" 2>/dev/null || true
        log "🧹 Sentinel file removed ($SENTINEL_FILE)"
    fi
}
trap cleanup_sentinel EXIT

# Step 0: Set Shutdown Sentinel
if [[ "$DRY_RUN" -eq 1 ]]; then
    log "🔍 [Dry Run] Would create shutdown sentinel: $SENTINEL_FILE"
else
    mkdir -p "$(dirname "$SENTINEL_FILE")" 2>/dev/null || true
    touch "$SENTINEL_FILE"
    log "🚩 Shutdown sentinel activated: $SENTINEL_FILE"
fi

# Discover Bot Installation Directories
BOT_DIRS=("$DEFAULT_AUTO_DIR")

PARENT_DIR="$(dirname "$DEFAULT_AUTO_DIR")"
for root_candidate in "$PARENT_DIR" "$HOME"; do
    if [[ -d "$root_candidate" ]]; then
        for bdir in "$root_candidate"/eternal-* "$root_candidate"/*-bot; do
            if [[ -d "$bdir" && ! " ${BOT_DIRS[*]} " =~ " ${bdir} " ]]; then
                BOT_DIRS+=("$bdir")
            fi
        done
    fi
done

# Step 1: Broadcast SIGINT/SIGTERM to tmux sessions and bot processes
log "📡 Broadcasting termination signals to active bot sessions..."

# Signal Tmux Sessions
for sess in "${TMUX_SESSIONS[@]}"; do
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$sess" 2>/dev/null; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "🔍 [Dry Run] Would send SIGINT (C-c) to tmux session: $sess"
        else
            log "⚡ Sending SIGINT (C-c) to tmux session: $sess"
            tmux send-keys -t "$sess" C-c 2>/dev/null || true
        fi
    fi
done

# Identify and Signal Bot Process PIDs
get_bot_pids() {
    local pids=()
    # 1. Pgrep for matching python main.py in bot dirs
    if command -v pgrep >/dev/null 2>&1; then
        while IFS= read -r pid; do
            [[ -n "$pid" ]] && pids+=("$pid")
        done < <(pgrep -f "eternal-(automation|feed|heartcast)-bot.*main\.py" 2>/dev/null || true)
    fi

    # 2. Fallback check for python main.py under specific bot dirs via ps
    if command -v ps >/dev/null 2>&1; then
        for bdir in "${BOT_DIRS[@]}"; do
            while IFS= read -r pid; do
                if [[ -n "$pid" && ! " ${pids[*]:-} " =~ " ${pid} " && "$pid" -ne $$ ]]; then
                    pids+=("$pid")
                fi
            done < <(ps aux 2>/dev/null | grep -E "python.*$bdir.*main\.py" | grep -v grep | awk '{print $2}' || true)
        done
    fi

    echo "${pids[@]:-}"
}

ACTIVE_PIDS=($(get_bot_pids))

if [[ ${#ACTIVE_PIDS[@]} -gt 0 ]]; then
    log "🎯 Found ${#ACTIVE_PIDS[@]} active bot process(es): ${ACTIVE_PIDS[*]}"
    for pid in "${ACTIVE_PIDS[@]}"; do
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "🔍 [Dry Run] Would send SIGTERM to PID: $pid"
        else
            log "⚡ Sending SIGTERM to PID: $pid"
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
else
    log "ℹ️ No direct standalone bot PIDs detected."
fi

# Step 2: Graceful Drain Window (up to TIMEOUT seconds)
log "⏳ Waiting up to ${TIMEOUT}s for in-flight tasks to drain..."

START_TIME=$(date +%s)
TIMEOUT_INT=${TIMEOUT%.*}
[[ $TIMEOUT_INT -le 0 ]] && TIMEOUT_INT=1

if [[ "$DRY_RUN" -eq 0 ]]; then
    while true; do
        CURRENT_TIME=$(date +%s)
        ELAPSED=$((CURRENT_TIME - START_TIME))

        REMAINING_PIDS=($(get_bot_pids))
        if [[ ${#REMAINING_PIDS[@]} -eq 0 ]]; then
            log "✅ All bot processes drained and stopped cleanly within ${ELAPSED}s."
            break
        fi

        if [[ $ELAPSED -ge $TIMEOUT_INT ]]; then
            log "⚠️ Graceful timeout (${TIMEOUT}s) reached with ${#REMAINING_PIDS[@]} lingering process(es): ${REMAINING_PIDS[*]}"
            log "🔥 Force killing lingering processes (SIGKILL)..."
            for pid in "${REMAINING_PIDS[@]}"; do
                kill -9 "$pid" 2>/dev/null || true
            done
            FORCED_KILL=1
            break
        fi

        sleep 0.5
    done
else
    log "🔍 [Dry Run] Simulated ${TIMEOUT}s drain window check."
fi

# Step 3: Atomic State & Database Integrity Verification
log "💾 Validating persistent database integrity across bot suites..."

validate_json() {
    local file="$1"
    if [[ -f "$file" ]]; then
        if python3 -c "import json, sys; json.load(open(sys.argv[1], 'r', encoding='utf-8'))" "$file" 2>/dev/null; then
            log "  ✅ JSON DB intact: $file"
            return 0
        else
            log "  ❌ Corrupt JSON DB: $file"
            return 1
        fi
    fi
    return 0
}

for bdir in "${BOT_DIRS[@]}"; do
    if [[ -d "$bdir" ]]; then
        # Check standard JSON databases
        for dbname in "users.json" "state.json" "channel_seen_posts.json" "reminders.json"; do
            target_json="$bdir/$dbname"
            if [[ -f "$target_json" ]]; then
                if ! validate_json "$target_json"; then
                    DB_ERROR=1
                fi
            fi
        done

        # Check SQLite databases (e.g. feedbot.db)
        if [[ -f "$bdir/feedbot.db" ]] && (command -v sqlite3 >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1); then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                log "🔍 [Dry Run] Would execute SQLite WAL checkpoint on $bdir/feedbot.db"
            else
                log "📦 Executing SQLite WAL checkpoint: $bdir/feedbot.db"
                if command -v sqlite3 >/dev/null 2>&1; then
                    sqlite3 "$bdir/feedbot.db" "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;" >/dev/null 2>&1 || {
                        log "⚠️ Warning: SQLite checkpoint on $bdir/feedbot.db returned error"
                        DB_ERROR=1
                    }
                elif command -v python3 >/dev/null 2>&1; then
                    python3 -c "import sqlite3, sys; con = sqlite3.connect(sys.argv[1]); con.execute('PRAGMA wal_checkpoint(TRUNCATE);'); con.execute('PRAGMA integrity_check;'); con.close()" "$bdir/feedbot.db" >/dev/null 2>&1 || {
                        log "⚠️ Warning: SQLite checkpoint on $bdir/feedbot.db returned error"
                        DB_ERROR=1
                    }
                fi
            fi
        fi
    fi
done

# Step 4: Purge Transient Files, Orphaned Downloads, and Locks
log "🧹 Purging orphaned temporary files, partial downloads, and lock files..."

for bdir in "${BOT_DIRS[@]}"; do
    if [[ -d "$bdir" ]]; then
        # 1. Clean downloads scratch directory
        if [[ -d "$bdir/downloads" ]]; then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                log "🔍 [Dry Run] Would purge transient files in $bdir/downloads/"
            else
                find "$bdir/downloads" -mindepth 1 -maxdepth 3 -delete 2>/dev/null || rm -rf "$bdir/downloads"/* 2>/dev/null || true
                log "  🗑️ Purged temporary downloads: $bdir/downloads/"
            fi
        fi

        # 2. Clean temporary files (*.tmp)
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "🔍 [Dry Run] Would purge *.tmp files in $bdir"
        else
            rm -f "$bdir"/*.tmp "$bdir"/*/*.tmp 2>/dev/null || true
        fi

        # 3. Release lock files (*.lock, .feedbot.lock)
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "🔍 [Dry Run] Would purge lock files in $bdir"
        else
            rm -f "$bdir"/.feedbot.lock "$bdir"/*.lock 2>/dev/null || true
        fi
    fi
done

# Step 5: Cleanly Terminate Tmux Sessions
log "🖥️ Terminating tmux sessions..."

for sess in "${TMUX_SESSIONS[@]}"; do
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$sess" 2>/dev/null; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "🔍 [Dry Run] Would kill tmux session: $sess"
        else
            tmux kill-session -t "$sess" 2>/dev/null || true
            log "  🛑 Tmux session closed: $sess"
        fi
    fi
done

# Step 6: Atomic Filesystem Sync
log "🔄 Executing filesystem sync..."
if [[ "$DRY_RUN" -eq 1 ]]; then
    log "🔍 [Dry Run] Would invoke sync"
else
    sync 2>/dev/null || true
    log "✅ Filesystem sync completed."
fi

# Step 7: Summary & Status Determination
log "================================================================="
if [[ "$DB_ERROR" -eq 0 && "$FORCED_KILL" -eq 0 ]]; then
    log "🎉 PRE-REBOOT SHUTDOWN COMPLETED SUCCESSFULLY (Zero errors)"
    log "================================================================="
    exit 0
else
    log "⚠️ PRE-REBOOT SHUTDOWN COMPLETED WITH ISSUES (Forced Kill: $FORCED_KILL, DB Error: $DB_ERROR)"
    log "================================================================="
    exit 1
fi
