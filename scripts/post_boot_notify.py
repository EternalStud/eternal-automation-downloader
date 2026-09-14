#!/usr/bin/env python3
"""
Post-Boot Health Telemetry & Admin Telegram Dispatcher
======================================================
Autonomous health verification script designed for Termux:Boot on Android (Samsung Galaxy M31).
Gathers true OS cold-boot uptime, hardware metrics, tmux session statuses, network configuration,
and delivers a consolidated rich HTML status alert to the Admin (OWNER_ID).
"""

import os
import sys
import json
import time
import socket
import argparse
import subprocess

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from config import OWNER_ID, BOT_TOKEN
from telegram_api import send_msg, escape_html
from health import (
    get_server_metrics,
    get_system_uptime,
    get_system_uptime_seconds,
    format_uptime_seconds,
)

def get_network_ip(target_host="1.1.1.1", target_port=80, timeout=2.0):
    """Retrieves primary outbound local IP address without making actual network transmission."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.connect((target_host, target_port))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        # Fallback to local hostname lookup or loopback
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

def check_tmux_session(session_name, timeout=2):
    """Checks if a specific tmux session is currently active and alive."""
    try:
        p = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, Exception):
        return False

def check_process_running(pattern, timeout=2):
    """Checks if any process matching the given pattern is active using pgrep."""
    try:
        p = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, Exception):
        return False

def gather_full_telemetry(uptime_path="/proc/uptime"):
    """
    Collects complete phone server post-boot operational metrics:
    - True cold-boot OS uptime
    - Hardware metrics (CPU, RAM, Disk, Battery, Thermals)
    - Multi-bot tmux session states (bot, feed-bot, heartcast)
    - Daemon statuses (cloudflared, sshd, crond)
    - Network address
    """
    metrics = get_server_metrics(uptime_path=uptime_path)
    uptime_str = get_system_uptime(uptime_path=uptime_path)
    uptime_sec = get_system_uptime_seconds(uptime_path=uptime_path)
    local_ip = get_network_ip()

    sessions = {
        "bot": check_tmux_session("bot"),
        "feed-bot": check_tmux_session("feed-bot"),
        "heartcast": check_tmux_session("heartcast"),
    }

    services = {
        "cloudflared": check_process_running("cloudflared"),
        "sshd": check_process_running("sshd"),
        "crond": check_process_running("crond"),
    }

    all_sessions_ok = all(sessions.values())

    return {
        "timestamp": time.time(),
        "formatted_time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "system_uptime": uptime_str,
        "system_uptime_seconds": uptime_sec,
        "local_ip": local_ip,
        "metrics": metrics,
        "sessions": sessions,
        "services": services,
        "all_sessions_active": all_sessions_ok,
    }

def format_post_boot_html(telemetry):
    """
    Formats telemetry dictionary into a structured, elegant HTML message
    suitable for Telegram Bot API delivery.
    """
    m = telemetry.get("metrics", {})
    s = telemetry.get("sessions", {})
    srv = telemetry.get("services", {})

    # Battery
    bat_pct = m.get("battery_pct")
    bat_str = f"{bat_pct}%" if bat_pct is not None else "N/A"
    bat_stat = escape_html(str(m.get("battery_status", "Unknown")))
    bat_temp_val = m.get("battery_temp")
    bat_temp_str = f"{bat_temp_val}°C" if bat_temp_val is not None else "Normal"

    # Memory
    ram_used = m.get("ram_used_mb")
    ram_total = m.get("ram_total_mb")
    ram_pct = m.get("ram_pct")
    if ram_used is not None and ram_total is not None:
        ram_str = f"{ram_used} MB / {ram_total} MB ({ram_pct}%)"
    else:
        ram_str = "N/A"

    # Disk
    disk_free = m.get("disk_free_gb")
    disk_total = m.get("disk_total_gb")
    disk_free_pct = m.get("disk_free_pct")
    if disk_free is not None and disk_total is not None:
        disk_str = f"{disk_free} GB Free / {disk_total} GB ({disk_free_pct}% Free)"
    else:
        disk_str = "N/A"

    # CPU
    cpu_pct = m.get("cpu_pct")
    cpu_str = f" • CPU Load: <b>{cpu_pct}%</b>\n" if cpu_pct is not None else ""

    # Bot Sessions
    bot_icon = "🟢 Active" if s.get("bot") else "🔴 Inactive"
    feed_icon = "🟢 Active" if s.get("feed-bot") else "🔴 Inactive"
    heart_icon = "🟢 Active" if s.get("heartcast") else "🔴 Inactive"

    # Daemons
    cf_icon = "🟢 Active" if srv.get("cloudflared") else "⚠️ Inactive"
    ssh_icon = "🟢 Active" if srv.get("sshd") else "⚠️ Inactive"
    cron_icon = "🟢 Active" if srv.get("crond") else "⚠️ Inactive"

    uptime_display = escape_html(str(telemetry.get("system_uptime", "Unknown")))
    local_ip_display = escape_html(str(telemetry.get("local_ip", "127.0.0.1")))
    time_display = escape_html(str(telemetry.get("formatted_time", "")))

    html_message = (
        "🚀 <b>Samsung Galaxy M31 — Server Auto-Recovery Complete</b> 📱\n\n"
        "✨ <i>The phone server has rebooted and restored all bot operations.</i>\n\n"
        "🤖 <b>Multi-Bot Fleet Status:</b>\n"
        f"  • <code>eternal-automation-bot</code>: {bot_icon} (tmux: <code>bot</code>)\n"
        f"  • <code>eternal-feed-bot</code>: {feed_icon} (tmux: <code>feed-bot</code>)\n"
        f"  • <code>eternal-heartcast-bot</code>: {heart_icon} (tmux: <code>heartcast</code>)\n\n"
        "🔋 <b>Power & Battery Telemetry:</b>\n"
        f"  • Battery: <b>{bat_str}</b> ({bat_stat})\n"
        f"  • Temperature: <b>{bat_temp_str}</b>\n\n"
        "📊 <b>System Performance & Uptime:</b>\n"
        f"  • Phone Uptime: <b>{uptime_display}</b> (Cold Boot)\n"
        f"  • Memory (RAM): <b>{ram_str}</b>\n"
        f"  • Disk Space: <b>{disk_str}</b>\n"
        f"{cpu_str}\n"
        "🌐 <b>Network & Ingress:</b>\n"
        f"  • Local IP: <code>{local_ip_display}</code>\n"
        f"  • Cloudflare Tunnel: {cf_icon}\n"
        f"  • OpenSSH Server: {ssh_icon} (Port 8022)\n"
        f"  • Cron Supervisor: {cron_icon}\n\n"
        f"🕒 <i>Recovered at: {time_display}</i>"
    )

    return html_message

def send_post_boot_notification(chat_id=None, dry_run=False, as_json=False, quiet=False, uptime_path="/proc/uptime"):
    """
    Main orchestration function for gathering telemetry and delivering the alert.
    
    Args:
        chat_id: Target Telegram chat ID (defaults to OWNER_ID).
        dry_run: If True, prints message without sending over Telegram.
        as_json: If True, outputs JSON data to stdout.
        quiet: If True, suppresses stdout logging.
        uptime_path: File path to /proc/uptime.
        
    Returns:
        tuple (success: bool, telemetry: dict)
    """
    target_chat = str(chat_id).strip() if chat_id else str(OWNER_ID).strip()
    telemetry = gather_full_telemetry(uptime_path=uptime_path)

    if as_json:
        json_output = json.dumps(telemetry, indent=2)
        if not quiet:
            print(json_output)
        return True, telemetry

    message = format_post_boot_html(telemetry)

    if dry_run:
        if not quiet:
            print(f"📡 [DRY-RUN] Target Chat ID: {target_chat}")
            print("==================================================")
            print(message)
            print("==================================================")
        return True, telemetry

    # Production delivery via Telegram Bot API
    try:
        resp = send_msg(target_chat, message, parse_mode="HTML")
        if resp and getattr(resp, "ok", False):
            if not quiet:
                print(f"✅ Post-boot health notification delivered to Admin ({target_chat})")
            return True, telemetry
        else:
            err_desc = getattr(resp, "text", "Unknown error")
            if not quiet:
                print(f"⚠️ Telegram delivery failed: {err_desc}")
            return False, telemetry
    except Exception as e:
        if not quiet:
            print(f"❌ Exception during Telegram delivery: {e}")
        return False, telemetry

def main():
    parser = argparse.ArgumentParser(
        description="Samsung Galaxy M31 Post-Boot Health Telemetry & Telegram Dispatcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution and print message without dispatching to Telegram",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw telemetry metrics in JSON format",
    )
    parser.add_argument(
        "--chat-id",
        type=str,
        default=None,
        help=f"Override Telegram destination chat ID (default: OWNER_ID={OWNER_ID})",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress console output",
    )
    parser.add_argument(
        "--uptime-path",
        type=str,
        default="/proc/uptime",
        help="Custom path to /proc/uptime file (default: /proc/uptime)",
    )

    args = parser.parse_args()

    success, _ = send_post_boot_notification(
        chat_id=args.chat_id,
        dry_run=args.dry_run,
        as_json=args.json,
        quiet=args.quiet,
        uptime_path=args.uptime_path,
    )

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
