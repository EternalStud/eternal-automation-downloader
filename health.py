import os
import json
import time
import shutil
import subprocess
from config import OWNER_ID, DOWNLOADS_DIR
from telegram_api import send_msg
from downloader import cleanup_old_downloads

BOT_START_TIME = time.time()
LAST_HEALTH_ALERTS = {"battery": 0, "disk": 0, "ram": 0, "temp": 0}

def format_uptime_seconds(uptime_sec):
    """Formats seconds into human-readable duration (e.g. '2d 4h 15m', '1h 30m', '5m 20s')."""
    if uptime_sec is None or uptime_sec < 0:
        return "Unknown"
    uptime_sec = int(uptime_sec)
    mins = uptime_sec // 60
    hours = mins // 60
    days = hours // 24
    if days > 0:
        return f"{days}d {hours % 24}h {mins % 60}m"
    elif hours > 0:
        return f"{hours}h {mins % 60}m"
    else:
        return f"{mins}m {int(uptime_sec % 60)}s"

def get_system_uptime_seconds(uptime_path="/proc/uptime"):
    """Reads raw OS kernel uptime in seconds from /proc/uptime with cross-platform fallback."""
    try:
        if os.path.exists(uptime_path):
            with open(uptime_path, "r") as f:
                content = f.read().strip()
                if content:
                    return float(content.split()[0])
    except PermissionError:
        if uptime_path == "/proc/uptime" and hasattr(time, "clock_gettime"):
            try:
                clk = getattr(time, "CLOCK_BOOTTIME", getattr(time, "CLOCK_MONOTONIC", None))
                if clk is not None:
                    return float(time.clock_gettime(clk))
            except Exception:
                pass
    except Exception:
        pass

    # Non-Linux fallback (e.g., macOS / BSD sysctl)
    try:
        p = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=1)
        if p.returncode == 0:
            import re
            m = re.search(r"sec\s*=\s*(\d+)", p.stdout)
            if m:
                boot_sec = int(m.group(1))
                return max(0.0, time.time() - boot_sec)
    except Exception:
        pass

    return None

def get_system_uptime(uptime_path="/proc/uptime"):
    """Returns human-readable OS kernel uptime from /proc/uptime or bot fallback."""
    sec = get_system_uptime_seconds(uptime_path=uptime_path)
    if sec is not None:
        return format_uptime_seconds(sec)
    
    # Fallback to bot uptime
    bot_sec = time.time() - BOT_START_TIME
    return format_uptime_seconds(bot_sec)

def get_server_metrics(uptime_path="/proc/uptime"):
    """Inspects live server hardware metrics: CPU, RAM, Disk, Battery, Downloads cache, and Uptime."""
    metrics = {
        "cpu_pct": None,
        "ram_total_mb": None,
        "ram_used_mb": None,
        "ram_pct": None,
        "disk_total_gb": None,
        "disk_used_gb": None,
        "disk_free_gb": None,
        "disk_free_pct": None,
        "battery_pct": None,
        "battery_status": "Unknown",
        "battery_temp": None,
        "downloads_size_mb": 0.0,
        "uptime_str": "0h 0m",
        "system_uptime_str": "0m 0s",
        "system_uptime_seconds": None,
    }

    # 1. Disk usage
    try:
        disk = shutil.disk_usage(os.path.expanduser("~"))
        metrics["disk_total_gb"] = round(disk.total / (1024**3), 2)
        metrics["disk_used_gb"] = round(disk.used / (1024**3), 2)
        metrics["disk_free_gb"] = round(disk.free / (1024**3), 2)
        metrics["disk_free_pct"] = round((disk.free / disk.total) * 100, 1)
    except Exception:
        pass

    # 2. RAM from /proc/meminfo
    try:
        mem = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                p = line.split(":")
                if len(p) == 2:
                    mem[p[0].strip()] = int(p[1].strip().split()[0])
        total_ram = mem.get("MemTotal", 0) / 1024
        avail_ram = mem.get("MemAvailable", 0) / 1024
        used_ram = total_ram - avail_ram
        metrics["ram_total_mb"] = int(total_ram)
        metrics["ram_used_mb"] = int(used_ram)
        metrics["ram_pct"] = round((used_ram / total_ram) * 100, 1) if total_ram else 0
    except Exception:
        pass

    # 3. CPU Percentage from process table
    try:
        p = subprocess.run(["ps", "-eo", "%cpu"], capture_output=True, text=True, timeout=2)
        if p.returncode == 0:
            lines = p.stdout.strip().split("\n")[1:]
            total_cpu = sum(float(x.strip()) for x in lines if x.strip().replace(".", "", 1).isdigit())
            metrics["cpu_pct"] = round(min(100.0, total_cpu), 1)
    except Exception:
        pass

    # 4. Battery from termux-battery-status or sysfs nodes
    try:
        p = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=1)
        if p.returncode == 0:
            bdata = json.loads(p.stdout)
            metrics["battery_pct"] = bdata.get("percentage")
            metrics["battery_status"] = bdata.get("status", "Unknown")
            metrics["battery_temp"] = bdata.get("temperature")
    except Exception:
        pass

    if metrics["battery_pct"] is None:
        for path in ["/sys/class/power_supply/battery/capacity", "/sys/class/power_supply/bms/capacity"]:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        metrics["battery_pct"] = int(f.read().strip())
                    break
                except Exception:
                    pass
        for path in ["/sys/class/power_supply/battery/status", "/sys/class/power_supply/bms/status"]:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        metrics["battery_status"] = f.read().strip()
                    break
                except Exception:
                    pass

    # 5. Downloads dir size
    try:
        if os.path.exists(DOWNLOADS_DIR):
            tot = sum(
                os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(DOWNLOADS_DIR)
                for f in files
            )
            metrics["downloads_size_mb"] = round(tot / (1024*1024), 2)
    except Exception:
        pass

    # 6. Bot Process Uptime string
    uptime_sec = int(time.time() - BOT_START_TIME)
    days = uptime_sec // 86400
    hours = (uptime_sec % 86400) // 3600
    mins = (uptime_sec % 3600) // 60
    if days > 0:
        metrics["uptime_str"] = f"{days}d {hours}h {mins}m"
    else:
        metrics["uptime_str"] = f"{hours}h {mins}m"

    # 7. True OS System Uptime
    sys_uptime_sec = get_system_uptime_seconds(uptime_path=uptime_path)
    metrics["system_uptime_seconds"] = sys_uptime_sec
    metrics["system_uptime_str"] = format_uptime_seconds(sys_uptime_sec) if sys_uptime_sec is not None else metrics["uptime_str"]

    return metrics

def check_hardware_health_and_alert():
    """Periodically inspects server hardware and alerts Owner if battery, disk, or RAM is critical."""
    global LAST_HEALTH_ALERTS
    now = time.time()
    ALERT_COOLDOWN = 7200  # 2 hours between repeated alerts of the same type

    try:
        metrics = get_server_metrics()

        # 1. Critical Low Battery (< 15% and not charging)
        bat_pct = metrics.get("battery_pct")
        bat_stat = str(metrics.get("battery_status", "")).upper()
        is_charging = ("CHARG" in bat_stat) and ("DISCHARG" not in bat_stat) and ("NOT" not in bat_stat)

        if bat_pct is not None and bat_pct <= 15 and not is_charging:
            if now - LAST_HEALTH_ALERTS.get("battery", 0) >= ALERT_COOLDOWN:
                send_msg(
                    OWNER_ID,
                    f"🚨 <b>CRITICAL SERVER ALERT: Low Battery ({bat_pct}%)</b> 🚨\n\n"
                    f"Your Samsung M31 phone server battery is at <b>*{bat_pct}%*</b> and is <b>*NOT CHARGING*</b>!\n"
                    f"🔌 Please connect the charger immediately to prevent bot shutdown.\n\n"
                    f"📊 Server Status: {metrics.get('uptime_str')} Uptime",
                    parse_mode="HTML"
                )
                LAST_HEALTH_ALERTS["battery"] = now
                print(f"🚨 Dispatched Low Battery Critical Alert ({bat_pct}%) to Owner")

        # 2. Critical Low Storage (< 2.0 GB free)
        disk_free = metrics.get("disk_free_gb")
        if disk_free is not None and disk_free < 2.0:
            if now - LAST_HEALTH_ALERTS.get("disk", 0) >= ALERT_COOLDOWN:
                cleanup_old_downloads(max_age_seconds=60)
                send_msg(
                    OWNER_ID,
                    f"🚨 <b>CRITICAL SERVER ALERT: Low Storage Space</b> 🚨\n\n"
                    f"Available Disk Space is critically low: <b>*{disk_free} GB Free*</b> ({metrics.get('disk_free_pct')}%).\n"
                    f"🧹 Cleaned temporary downloads folder automatically.\n\n"
                    f"💾 Total Disk: {metrics.get('disk_total_gb')} GB",
                    parse_mode="HTML"
                )
                LAST_HEALTH_ALERTS["disk"] = now
                print(f"🚨 Dispatched Low Disk Space Alert ({disk_free} GB) to Owner")

        # 3. High RAM Usage (> 92%)
        ram_pct = metrics.get("ram_pct")
        if ram_pct is not None and ram_pct >= 92.0:
            if now - LAST_HEALTH_ALERTS.get("ram", 0) >= ALERT_COOLDOWN:
                send_msg(
                    OWNER_ID,
                    f"⚠️ <b>SERVER ALERT: High Memory Usage ({ram_pct}%)</b> ⚠️\n\n"
                    f"RAM Usage is at <b>*{ram_pct}%*</b> ({metrics.get('ram_used_mb')} MB / {metrics.get('ram_total_mb')} MB).\n"
                    f"⚡ Bot will continue operating with thread pool recycling.",
                    parse_mode="HTML"
                )
                LAST_HEALTH_ALERTS["ram"] = now
                print(f"⚠️ Dispatched High RAM Alert ({ram_pct}%) to Owner")

        # 4. Critical High Temperature (> 42.0°C)
        bat_temp = metrics.get("battery_temp")
        if bat_temp is not None and bat_temp >= 42.0:
            if now - LAST_HEALTH_ALERTS.get("temp", 0) >= ALERT_COOLDOWN:
                send_msg(
                    OWNER_ID,
                    f"🌡️ <b>CRITICAL SERVER ALERT: High Phone Temperature ({bat_temp}°C)</b> 🌡️\n\n"
                    f"Your Samsung M31 server temperature is at <b>*{bat_temp}°C*</b>!\n"
                    f"❄️ Move phone to a cooler spot or remove phone case to protect battery lifespan.",
                    parse_mode="HTML"
                )
                LAST_HEALTH_ALERTS["temp"] = now
                print(f"🌡️ Dispatched High Temperature Alert ({bat_temp}°C) to Owner")

    except Exception as e:
        print(f"⚠️ Error checking hardware health: {e}")
