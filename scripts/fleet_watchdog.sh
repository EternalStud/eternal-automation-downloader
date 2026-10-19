#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Fleet Self-Healing Watchdog (cron: */5 * * * *)
# Restores sshd, cloudflared and all 5 tmux sessions if any died — e.g. when the
# network was not ready during the Sunday 03:00 AM auto-restart.
# ==============================================================================
export HOME="${HOME:-/data/data/com.termux/files/home}"
export PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
export PATH="$PREFIX/bin:$PATH"
LOG="$HOME/watchdog.log"

# Never fight the pre-reboot shutdown or the boot script
[ -f "$HOME/.shutdown_in_progress" ] && exit 0
# Skip Sunday 02:50–03:20 maintenance window
DOW=$(date +%u); HM=$(date +%H%M)
if [ "$DOW" = "7" ] && [ "$HM" -ge 0250 ] && [ "$HM" -le 0320 ]; then exit 0; fi
# Skip while the boot script is still running
pgrep -f "phone-server-boot.sh" >/dev/null 2>&1 && exit 0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

pgrep sshd >/dev/null 2>&1 || { sshd && log "🔑 Restarted sshd"; }

if ! pgrep cloudflared >/dev/null 2>&1 && [ -f "$HOME/.cloudflared/token" ]; then
    nohup cloudflared tunnel run --token-file "$HOME/.cloudflared/token" >> "$HOME/cloudflared.log" 2>&1 &
    log "☁️ Restarted cloudflared tunnel"
fi

ensure() {
    local sess="$1" dir="$2" cmd="$3"
    [ -d "$dir" ] || return 0
    if ! tmux has-session -t "$sess" 2>/dev/null; then
        tmux new-session -d -s "$sess" "cd '$dir' && $cmd"
        log "🤖 Respawned tmux session '$sess'"
    fi
}

ensure bot       "$HOME/eternal-automation-bot" "bash start.sh"
ensure feed-bot  "$HOME/eternal-feed-bot"       "bash start.sh"
ensure heartcast "$HOME/eternal-heartcast-bot"  "bash start.sh"
tmux has-session -t server 2>/dev/null || ensure ugc-net "$HOME/eternal-ugc-net" "python3 server.py 8000"
ensure sentinel  "$HOME/PROJECT/eternal_sentinel-alpha-fno-trading" "bash scripts/start_sentinel.sh --daemon"

# Keep the log small
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 500 ] && tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
