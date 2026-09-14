#!/usr/bin/env bash
# ==============================================================================
# ⏰ Samsung Galaxy M31 — Automated Pre-Reboot Maintenance Cron Setup
# ==============================================================================
# Configures crontab with Termux environment headers and schedules
# pre_reboot_shutdown.sh prior to the Samsung OS Auto-Restart window.
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# Default Constants and Environment Detection
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Termux detection
IS_TERMUX=0
if [[ -d "/data/data/com.termux/files/usr" ]] || [[ -n "${PREFIX:-}" && "${PREFIX:-}" == *"com.termux"* ]] || [[ "${HOME:-}" == *"com.termux"* ]]; then
    IS_TERMUX=1
fi

TERMUX_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
TERMUX_HOME="${HOME:-/data/data/com.termux/files/home}"

# Default Environment Headers for Crontab
if [[ "$IS_TERMUX" -eq 1 ]]; then
    DEFAULT_SHELL="${TERMUX_PREFIX}/bin/bash"
    DEFAULT_PATH="${TERMUX_PREFIX}/bin:${TERMUX_PREFIX}/bin/applets:/usr/bin:/bin:${PATH}"
    DEFAULT_HOME="${TERMUX_HOME}"
    DEFAULT_SCRIPT="${TERMUX_HOME}/eternal-automation-bot/scripts/pre_reboot_shutdown.sh"
    DEFAULT_LOG="${TERMUX_HOME}/pre_reboot.log"
else
    DEFAULT_SHELL="${SHELL:-/bin/bash}"
    DEFAULT_PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"
    DEFAULT_HOME="${HOME:-$(pwd)}"
    DEFAULT_SCRIPT="${REPO_ROOT}/scripts/pre_reboot_shutdown.sh"
    DEFAULT_LOG="${DEFAULT_HOME}/pre_reboot.log"
fi

# Override default script if repo script exists locally
if [[ -f "${SCRIPT_DIR}/pre_reboot_shutdown.sh" && "$IS_TERMUX" -eq 0 ]]; then
    DEFAULT_SCRIPT="${SCRIPT_DIR}/pre_reboot_shutdown.sh"
fi

CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
CROND_BIN="${CROND_BIN:-crond}"

# Managed Block Markers
BLOCK_START="# >>> eternal-automation-bot pre-reboot maintenance >>>"
BLOCK_END="# <<< eternal-automation-bot pre-reboot maintenance <<<"

# ------------------------------------------------------------------------------
# Configuration Variables (Defaults & Env overrides)
# ------------------------------------------------------------------------------
REBOOT_TIME="${REBOOT_TIME:-03:00}"
REBOOT_HOUR=""
REBOOT_MINUTE=""
LEAD_MINUTES="${LEAD_MINUTES:-5}"
CRON_DOM="*"
CRON_MON="*"
CRON_DOW="${CRON_DOW:-*}"
SCRIPT_PATH="${PRE_REBOOT_SCRIPT:-$DEFAULT_SCRIPT}"
LOG_FILE="${PRE_REBOOT_LOG:-$DEFAULT_LOG}"
CRON_SHELL="${CRON_SHELL:-$DEFAULT_SHELL}"
CRON_PATH="${CRON_PATH:-$DEFAULT_PATH}"
CRON_HOME="${CRON_HOME:-$DEFAULT_HOME}"

ACTION="install"
DRY_RUN=0

# ------------------------------------------------------------------------------
# Helper: Print Usage
# ------------------------------------------------------------------------------
print_usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Manages the pre-reboot maintenance crontab schedule for eternal-automation-bot.

Actions:
  -i, --install          Install or update the maintenance cron schedule (default)
  -s, --show, --status   Show current crontab and status of maintenance schedule
  -r, --remove, --uninstall
                         Remove the maintenance schedule from crontab
  -c, --check-daemon     Check if crond daemon is running
  -e, --ensure-daemon    Start crond daemon if not running

Timing Options:
  -t, --reboot-time HH:MM   Samsung Auto-Restart time in 24h format (default: 03:00)
      --reboot-hour H       Reboot hour (0-23)
      --reboot-minute M     Reboot minute (0-59)
  -l, --lead-minutes N      Minutes prior to restart to run shutdown (default: 5)
  -d, --days-of-week DOW    Crontab day-of-week field (default: *)

Target Options:
      --script-path PATH    Path to pre_reboot_shutdown.sh
      --log-file PATH       Path to pre_reboot.log
      --shell PATH          Shell header for crontab
      --path PATH           PATH header for crontab
      --home PATH           HOME header for crontab

General Options:
  -n, --dry-run          Print the generated crontab without applying it
  -h, --help             Display this help message and exit

Examples:
  $(basename "$0") --install
  $(basename "$0") --install --reboot-time 03:00 --lead-minutes 5
  $(basename "$0") --install --reboot-time 00:00
  $(basename "$0") --show
  $(basename "$0") --remove
  $(basename "$0") --ensure-daemon
EOF
}

# ------------------------------------------------------------------------------
# Helper: Parse Time and Calculate Cron Schedule
# ------------------------------------------------------------------------------
calculate_cron_schedule() {
    local h m lead
    
    if [[ -n "$REBOOT_HOUR" && -n "$REBOOT_MINUTE" ]]; then
        h="$REBOOT_HOUR"
        m="$REBOOT_MINUTE"
    elif [[ "$REBOOT_TIME" =~ ^([0-9]{1,2}):([0-9]{1,2})$ ]]; then
        h="${BASH_REMATCH[1]}"
        m="${BASH_REMATCH[2]}"
    else
        echo "❌ Error: Invalid reboot time format: '$REBOOT_TIME'. Expected HH:MM (e.g. 03:00)" >&2
        exit 1
    fi

    # Strip leading zeros for arithmetic
    h=$((10#$h))
    m=$((10#$m))
    lead=$((10#$LEAD_MINUTES))

    if (( h < 0 || h > 23 )); then
        echo "❌ Error: Invalid hour '$h'. Must be between 0 and 23." >&2
        exit 1
    fi
    if (( m < 0 || m > 59 )); then
        echo "❌ Error: Invalid minute '$m'. Must be between 0 and 59." >&2
        exit 1
    fi
    if (( lead < 1 || lead > 1439 )); then
        echo "❌ Error: Invalid lead minutes '$lead'. Must be between 1 and 1439." >&2
        exit 1
    fi

    # Total reboot minutes from midnight
    local total_minutes=$(( h * 60 + m ))
    # Subtract lead minutes
    local scheduled_minutes=$(( (total_minutes - lead) % 1440 ))
    if (( scheduled_minutes < 0 )); then
        scheduled_minutes=$(( scheduled_minutes + 1440 ))
    fi

    CALC_HOUR=$(( scheduled_minutes / 60 ))
    CALC_MIN=$(( scheduled_minutes % 60 ))

    FORMATTED_REBOOT_TIME=$(printf "%02d:%02d" "$h" "$m")
    FORMATTED_CRON_TIME=$(printf "%02d:%02d" "$CALC_HOUR" "$CALC_MIN")
}

# ------------------------------------------------------------------------------
# Helper: Check and Ensure crond Daemon
# ------------------------------------------------------------------------------
check_daemon() {
    local running=0
    if command -v pgrep >/dev/null 2>&1; then
        if pgrep -x "$CROND_BIN" >/dev/null 2>&1 || pgrep -f "$CROND_BIN" >/dev/null 2>&1; then
            running=1
        fi
    elif command -v pidof >/dev/null 2>&1; then
        if pidof "$CROND_BIN" >/dev/null 2>&1; then
            running=1
        fi
    fi

    if [[ "$running" -eq 1 ]]; then
        echo "✅ Cron daemon ($CROND_BIN) is RUNNING."
        return 0
    else
        echo "⚠️ Cron daemon ($CROND_BIN) is NOT running."
        return 1
    fi
}

ensure_daemon() {
    if check_daemon >/dev/null 2>&1; then
        echo "✅ Cron daemon ($CROND_BIN) is already running."
        return 0
    fi

    echo "⏰ Starting cron daemon ($CROND_BIN -b)..."
    if command -v "$CROND_BIN" >/dev/null 2>&1; then
        "$CROND_BIN" -b || "$CROND_BIN" || true
    elif [[ -x "${TERMUX_PREFIX}/bin/crond" ]]; then
        "${TERMUX_PREFIX}/bin/crond" -b || true
    elif [[ -x "/usr/sbin/crond" ]]; then
        /usr/sbin/crond || true
    else
        echo "❌ Error: '$CROND_BIN' command not found. On Termux, install with: pkg install cronie" >&2
        return 1
    fi

    sleep 1
    if check_daemon >/dev/null 2>&1; then
        echo "✅ Cron daemon successfully started."
        return 0
    else
        echo "❌ Warning: Failed to verify cron daemon startup." >&2
        return 1
    fi
}

# ------------------------------------------------------------------------------
# Helper: Generate Managed Crontab Block
# ------------------------------------------------------------------------------
generate_managed_block() {
    calculate_cron_schedule

    cat <<EOF
$BLOCK_START
# Pre-Reboot Graceful Multi-Bot Shutdown
# Configured for Samsung OS Auto-Restart at ${FORMATTED_REBOOT_TIME} (${LEAD_MINUTES} min lead time -> ${FORMATTED_CRON_TIME})
SHELL=${CRON_SHELL}
PATH=${CRON_PATH}
HOME=${CRON_HOME}
${CALC_MIN} ${CALC_HOUR} ${CRON_DOM} ${CRON_MON} ${CRON_DOW} ${SCRIPT_PATH} >> ${LOG_FILE} 2>&1
$BLOCK_END
EOF
}

# ------------------------------------------------------------------------------
# Helper: Read Current Crontab
# ------------------------------------------------------------------------------
read_current_crontab() {
    # crontab -l exits with 1 when crontab is empty on some systems
    "$CRONTAB_BIN" -l 2>/dev/null || true
}

# ------------------------------------------------------------------------------
# Helper: Strip Managed Block from Crontab
# ------------------------------------------------------------------------------
strip_managed_block() {
    local input="$1"
    # Use awk to remove everything between BLOCK_START and BLOCK_END inclusive
    echo "$input" | awk -v start="$BLOCK_START" -v end="$BLOCK_END" '
        $0 == start { in_block=1; next }
        $0 == end   { in_block=0; next }
        !in_block   { print }
    ' | sed -e :a -e '/^\n*$/{$d;N;};/\n$/ba' # Strip trailing newlines
}

# ------------------------------------------------------------------------------
# Action: Install / Update Crontab
# ------------------------------------------------------------------------------
do_install() {
    calculate_cron_schedule

    local current_cron
    current_cron="$(read_current_crontab)"

    local cleaned_cron
    cleaned_cron="$(strip_managed_block "$current_cron")"

    local new_block
    new_block="$(generate_managed_block)"

    local final_cron=""
    if [[ -n "$cleaned_cron" ]]; then
        final_cron="${cleaned_cron}"$'\n\n'"${new_block}"$'\n'
    else
        final_cron="${new_block}"$'\n'
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "--- Generated Crontab (Dry Run) ---"
        echo -n "$final_cron"
        echo "-----------------------------------"
        echo "ℹ️ Dry-run complete. Crontab was not modified."
        return 0
    fi

    # Install new crontab atomically
    local tmp_file
    tmp_file="$(mktemp "${TMPDIR:-/tmp}/crontab_XXXXXX")"
    trap 'rm -f "$tmp_file"' EXIT

    echo -n "$final_cron" > "$tmp_file"
    "$CRONTAB_BIN" "$tmp_file"
    rm -f "$tmp_file"
    trap - EXIT

    echo "✅ Successfully installed pre-reboot maintenance cron schedule!"
    echo "   • Samsung Auto-Restart Window : ${FORMATTED_REBOOT_TIME}"
    echo "   • Pre-Reboot Shutdown Schedule: ${CALC_MIN} ${CALC_HOUR} ${CRON_DOM} ${CRON_MON} ${CRON_DOW} (${FORMATTED_CRON_TIME})"
    echo "   • Script Target               : ${SCRIPT_PATH}"
    echo "   • Log Output                  : ${LOG_FILE}"

    # Verify daemon
    if ! check_daemon >/dev/null 2>&1; then
        echo "ℹ️ Hint: Crond is not running. Start it with: $(basename "$0") --ensure-daemon"
    fi
}

# ------------------------------------------------------------------------------
# Action: Show Crontab & Status
# ------------------------------------------------------------------------------
do_show() {
    echo "=========================================================="
    echo "📋 Pre-Reboot Maintenance Cron Status"
    echo "=========================================================="
    
    # Daemon status
    check_daemon || true
    echo ""

    # Current Crontab
    local current_cron
    current_cron="$(read_current_crontab)"

    if [[ -z "$current_cron" ]]; then
        echo "ℹ️ No crontab entries found for user '$(whoami 2>/dev/null || echo "$USER")'."
        return 0
    fi

    echo "📄 Current User Crontab:"
    echo "----------------------------------------------------------"
    echo "$current_cron"
    echo "----------------------------------------------------------"

    if echo "$current_cron" | grep -Fq "$BLOCK_START"; then
        echo "🟢 Pre-reboot maintenance schedule is ACTIVE in crontab."
    else
        echo "🟡 Pre-reboot maintenance schedule is NOT installed in crontab."
    fi

    if [[ -f "$SCRIPT_PATH" ]]; then
        if [[ -x "$SCRIPT_PATH" ]]; then
            echo "🟢 Shutdown script exists and is executable: $SCRIPT_PATH"
        else
            echo "⚠️ Warning: Shutdown script exists but is NOT executable: $SCRIPT_PATH"
        fi
    else
        echo "⚠️ Note: Target shutdown script not found at: $SCRIPT_PATH"
    fi
}

# ------------------------------------------------------------------------------
# Action: Remove Crontab Entry
# ------------------------------------------------------------------------------
do_remove() {
    local current_cron
    current_cron="$(read_current_crontab)"

    if [[ -z "$current_cron" ]] || ! echo "$current_cron" | grep -Fq "$BLOCK_START"; then
        echo "ℹ️ No pre-reboot maintenance schedule found in crontab. Nothing to remove."
        return 0
    fi

    local cleaned_cron
    cleaned_cron="$(strip_managed_block "$current_cron")"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "--- Resulting Crontab after Removal (Dry Run) ---"
        echo "$cleaned_cron"
        echo "-------------------------------------------------"
        echo "ℹ️ Dry-run complete. Crontab was not modified."
        return 0
    fi

    if [[ -n "$cleaned_cron" ]]; then
        local tmp_file
        tmp_file="$(mktemp "${TMPDIR:-/tmp}/crontab_XXXXXX")"
        trap 'rm -f "$tmp_file"' EXIT

        echo "$cleaned_cron" > "$tmp_file"
        "$CRONTAB_BIN" "$tmp_file"
        rm -f "$tmp_file"
        trap - EXIT
        echo "✅ Pre-reboot maintenance schedule removed. Remaining crontab preserved."
    else
        # If crontab is now empty, remove crontab completely
        "$CRONTAB_BIN" -r 2>/dev/null || "$CRONTAB_BIN" /dev/null 2>/dev/null || true
        echo "✅ Pre-reboot maintenance schedule removed. Crontab is now empty."
    fi
}

# ------------------------------------------------------------------------------
# Argument Parsing
# ------------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--install)
            ACTION="install"
            shift
            ;;
        -s|--show|--status)
            ACTION="show"
            shift
            ;;
        -r|--remove|--uninstall)
            ACTION="remove"
            shift
            ;;
        -c|--check-daemon)
            ACTION="check_daemon"
            shift
            ;;
        -e|--ensure-daemon)
            ACTION="ensure_daemon"
            shift
            ;;
        -t|--reboot-time)
            REBOOT_TIME="$2"
            shift 2
            ;;
        --reboot-hour)
            REBOOT_HOUR="$2"
            shift 2
            ;;
        --reboot-minute)
            REBOOT_MINUTE="$2"
            shift 2
            ;;
        -l|--lead-minutes)
            LEAD_MINUTES="$2"
            shift 2
            ;;
        -d|--days-of-week)
            CRON_DOW="$2"
            shift 2
            ;;
        --script-path)
            SCRIPT_PATH="$2"
            shift 2
            ;;
        --log-file)
            LOG_FILE="$2"
            shift 2
            ;;
        --shell)
            CRON_SHELL="$2"
            shift 2
            ;;
        --path)
            CRON_PATH="$2"
            shift 2
            ;;
        --home)
            CRON_HOME="$2"
            shift 2
            ;;
        -n|--dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "❌ Unknown argument: '$1'" >&2
            print_usage
            exit 1
            ;;
    esac
done

# ------------------------------------------------------------------------------
# Main Dispatch
# ------------------------------------------------------------------------------
case "$ACTION" in
    install)
        do_install
        ;;
    show)
        do_show
        ;;
    remove)
        do_remove
        ;;
    check_daemon)
        check_daemon
        ;;
    ensure_daemon)
        ensure_daemon
        ;;
    *)
        echo "❌ Unknown action: '$ACTION'" >&2
        exit 1
        ;;
esac
