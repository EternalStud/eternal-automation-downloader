import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import health
from scripts.post_boot_notify import (
    get_network_ip,
    check_tmux_session,
    check_process_running,
    gather_full_telemetry,
    format_post_boot_html,
    send_post_boot_notification,
    main,
)
from config import OWNER_ID

class TestHealthUptime(unittest.TestCase):
    """Unit tests for /proc/uptime parser and helper functions in health.py."""

    def test_format_uptime_seconds(self):
        self.assertEqual(health.format_uptime_seconds(None), "Unknown")
        self.assertEqual(health.format_uptime_seconds(-5), "Unknown")
        self.assertEqual(health.format_uptime_seconds(0), "0m 0s")
        self.assertEqual(health.format_uptime_seconds(45), "0m 45s")
        self.assertEqual(health.format_uptime_seconds(125), "2m 5s")
        self.assertEqual(health.format_uptime_seconds(3600), "1h 0m")
        self.assertEqual(health.format_uptime_seconds(3665), "1h 1m")
        self.assertEqual(health.format_uptime_seconds(7384), "2h 3m")
        # 1 day, 2 hours, 15 mins = 86400 + 7200 + 900 = 94500
        self.assertEqual(health.format_uptime_seconds(94500), "1d 2h 15m")
        # 3 days, 0 hours, 45 mins = 3*86400 + 45*60 = 261900
        self.assertEqual(health.format_uptime_seconds(261900), "3d 0h 45m")

    def test_get_system_uptime_seconds_from_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("12345.67 89012.34\n")
            temp_uptime = f.name

        try:
            sec = health.get_system_uptime_seconds(uptime_path=temp_uptime)
            self.assertAlmostEqual(sec, 12345.67, places=2)
        finally:
            if os.path.exists(temp_uptime):
                os.unlink(temp_uptime)

    def test_get_system_uptime_seconds_missing_and_corrupt_file(self):
        # Non-existent file
        sec = health.get_system_uptime_seconds(uptime_path="/nonexistent/path/proc/uptime")
        # In non-Linux environments it may fallback to sysctl or None
        self.assertTrue(sec is None or isinstance(sec, float))

        # Corrupted / empty file
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("corrupted string not numbers\n")
            temp_corrupt = f.name
        try:
            sec = health.get_system_uptime_seconds(uptime_path=temp_corrupt)
            self.assertTrue(sec is None or isinstance(sec, float))
        finally:
            if os.path.exists(temp_corrupt):
                os.unlink(temp_corrupt)

    def test_get_system_uptime_with_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("3665.12 7000.00\n")
            temp_uptime = f.name

        try:
            uptime_str = health.get_system_uptime(uptime_path=temp_uptime)
            self.assertEqual(uptime_str, "1h 1m")
        finally:
            if os.path.exists(temp_uptime):
                os.unlink(temp_uptime)

    def test_get_server_metrics_includes_system_uptime(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("7384.50 14000.00\n")
            temp_uptime = f.name

        try:
            metrics = health.get_server_metrics(uptime_path=temp_uptime)
            self.assertIn("system_uptime_str", metrics)
            self.assertIn("system_uptime_seconds", metrics)
            self.assertEqual(metrics["system_uptime_str"], "2h 3m")
            self.assertAlmostEqual(metrics["system_uptime_seconds"], 7384.50, places=2)
            # Verify backward compatibility
            self.assertIn("uptime_str", metrics)
            self.assertIn("cpu_pct", metrics)
            self.assertIn("ram_total_mb", metrics)
            self.assertIn("disk_free_gb", metrics)
            self.assertIn("battery_pct", metrics)
        finally:
            if os.path.exists(temp_uptime):
                os.unlink(temp_uptime)


class TestPostBootNotify(unittest.TestCase):
    """Unit tests for post_boot_notify.py telemetry collection, formatting, and dispatch."""

    @patch("socket.socket")
    def test_get_network_ip_success(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ["192.168.1.50", 54321]
        mock_socket_cls.return_value = mock_sock

        ip = get_network_ip()
        self.assertEqual(ip, "192.168.1.50")
        mock_sock.connect.assert_called_once_with(("1.1.1.1", 80))
        mock_sock.close.assert_called_once()

    @patch("socket.socket")
    def test_get_network_ip_fallback(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = OSError("Network unreachable")
        mock_socket_cls.return_value = mock_sock

        ip = get_network_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(len(ip) > 0)

    @patch("subprocess.run")
    def test_check_tmux_session_active(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(check_tmux_session("bot"))
        mock_run.assert_called_once_with(
            ["tmux", "has-session", "-t", "bot"],
            capture_output=True,
            text=True,
            timeout=2,
        )

    @patch("subprocess.run")
    def test_check_tmux_session_inactive(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(check_tmux_session("heartcast"))

    @patch("subprocess.run")
    def test_check_tmux_session_exception(self, mock_run):
        mock_run.side_effect = FileNotFoundError("tmux not found")
        self.assertFalse(check_tmux_session("feed-bot"))

    @patch("subprocess.run")
    def test_check_process_running(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(check_process_running("cloudflared"))

        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(check_process_running("nonexistent_service"))

        mock_run.side_effect = Exception("pgrep error")
        self.assertFalse(check_process_running("sshd"))

    @patch("scripts.post_boot_notify.check_process_running")
    @patch("scripts.post_boot_notify.check_tmux_session")
    @patch("scripts.post_boot_notify.get_network_ip")
    @patch("scripts.post_boot_notify.get_server_metrics")
    def test_gather_full_telemetry(self, mock_metrics, mock_ip, mock_tmux, mock_proc):
        mock_metrics.return_value = {
            "battery_pct": 85,
            "battery_status": "CHARGING",
            "battery_temp": 31.5,
            "ram_used_mb": 1400,
            "ram_total_mb": 5800,
            "ram_pct": 24.1,
            "disk_free_gb": 30.5,
            "disk_total_gb": 64.0,
            "disk_free_pct": 47.7,
            "cpu_pct": 8.5,
            "uptime_str": "0h 5m",
            "system_uptime_str": "5m 20s",
            "system_uptime_seconds": 320.0,
        }
        mock_ip.return_value = "192.168.1.100"
        mock_tmux.side_effect = lambda name: name in ["bot", "feed-bot", "heartcast"]
        mock_proc.side_effect = lambda name: name in ["cloudflared", "sshd", "crond"]

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("320.0 500.0\n")
            temp_uptime = f.name

        try:
            t = gather_full_telemetry(uptime_path=temp_uptime)
            self.assertEqual(t["local_ip"], "192.168.1.100")
            self.assertEqual(t["system_uptime"], "5m 20s")
            self.assertTrue(t["all_sessions_active"])
            self.assertTrue(t["sessions"]["bot"])
            self.assertTrue(t["sessions"]["feed-bot"])
            self.assertTrue(t["sessions"]["heartcast"])
            self.assertTrue(t["services"]["cloudflared"])
            self.assertTrue(t["services"]["sshd"])
            self.assertTrue(t["services"]["crond"])
        finally:
            if os.path.exists(temp_uptime):
                os.unlink(temp_uptime)

    def test_format_post_boot_html(self):
        telemetry = {
            "timestamp": 1725240000,
            "formatted_time": "2026-09-02 02:30:00 UTC",
            "system_uptime": "12m 45s",
            "system_uptime_seconds": 765.0,
            "local_ip": "192.168.1.42",
            "metrics": {
                "battery_pct": 92,
                "battery_status": "PLUGGED_AC (CHARGING)",
                "battery_temp": 32.1,
                "ram_used_mb": 1500,
                "ram_total_mb": 6000,
                "ram_pct": 25.0,
                "disk_free_gb": 40.0,
                "disk_total_gb": 64.0,
                "disk_free_pct": 62.5,
                "cpu_pct": 14.2,
                "uptime_str": "0h 10m",
            },
            "sessions": {
                "bot": True,
                "feed-bot": True,
                "heartcast": False,
            },
            "services": {
                "cloudflared": True,
                "sshd": True,
                "crond": False,
            },
            "all_sessions_active": False,
        }

        html = format_post_boot_html(telemetry)

        # Assert key content present
        self.assertIn("Samsung Galaxy M31", html)
        self.assertIn("12m 45s", html)
        self.assertIn("92%", html)
        self.assertIn("32.1°C", html)
        self.assertIn("1500 MB / 6000 MB (25.0%)", html)
        self.assertIn("40.0 GB Free / 64.0 GB (62.5% Free)", html)
        self.assertIn("14.2%", html)
        self.assertIn("192.168.1.42", html)
        self.assertIn("🟢 Active", html)  # bot & feed-bot
        self.assertIn("🔴 Inactive", html)  # heartcast
        self.assertIn("tmux: <code>bot</code>", html)
        self.assertIn("tmux: <code>feed-bot</code>", html)
        self.assertIn("tmux: <code>heartcast</code>", html)
        self.assertIn("2026-09-02 02:30:00 UTC", html)

    def test_format_post_boot_html_with_none_values(self):
        telemetry = {
            "timestamp": 1725240000,
            "formatted_time": "2026-09-02 02:30:00 UTC",
            "system_uptime": "Unknown",
            "local_ip": "127.0.0.1",
            "metrics": {
                "battery_pct": None,
                "battery_status": None,
                "battery_temp": None,
                "ram_used_mb": None,
                "ram_total_mb": None,
                "disk_free_gb": None,
                "disk_total_gb": None,
                "cpu_pct": None,
            },
            "sessions": {"bot": False, "feed-bot": False, "heartcast": False},
            "services": {"cloudflared": False, "sshd": False, "crond": False},
        }

        html = format_post_boot_html(telemetry)
        self.assertIn("Samsung Galaxy M31", html)
        self.assertIn("Battery: <b>N/A</b>", html)
        self.assertIn("Temperature: <b>Normal</b>", html)
        self.assertIn("Memory (RAM): <b>N/A</b>", html)
        self.assertIn("Disk Space: <b>N/A</b>", html)

    @patch("scripts.post_boot_notify.send_msg")
    def test_send_post_boot_notification_dry_run(self, mock_send_msg):
        success, telemetry = send_post_boot_notification(dry_run=True, quiet=True)
        self.assertTrue(success)
        self.assertIsInstance(telemetry, dict)
        mock_send_msg.assert_not_called()

    @patch("scripts.post_boot_notify.send_msg")
    def test_send_post_boot_notification_json(self, mock_send_msg):
        success, telemetry = send_post_boot_notification(as_json=True, quiet=True)
        self.assertTrue(success)
        self.assertIsInstance(telemetry, dict)
        mock_send_msg.assert_not_called()

    @patch("scripts.post_boot_notify.send_msg")
    def test_send_post_boot_notification_success(self, mock_send_msg):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_send_msg.return_value = mock_resp

        success, telemetry = send_post_boot_notification(chat_id="999999999", quiet=True)
        self.assertTrue(success)
        mock_send_msg.assert_called_once()
        args, kwargs = mock_send_msg.call_args
        self.assertEqual(args[0], "999999999")
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertIn("Samsung Galaxy M31", args[1])

    @patch("scripts.post_boot_notify.send_msg")
    def test_send_post_boot_notification_failure(self, mock_send_msg):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.text = "Bad Request: chat not found"
        mock_send_msg.return_value = mock_resp

        success, telemetry = send_post_boot_notification(chat_id="999999999", quiet=True)
        self.assertFalse(success)

    @patch("scripts.post_boot_notify.send_msg")
    def test_send_post_boot_notification_exception(self, mock_send_msg):
        mock_send_msg.side_effect = ConnectionError("Network unreachable")

        success, telemetry = send_post_boot_notification(chat_id="999999999", quiet=True)
        self.assertFalse(success)

    @patch("scripts.post_boot_notify.send_post_boot_notification")
    def test_cli_main_dry_run(self, mock_send):
        mock_send.return_value = (True, {})
        with patch.object(sys, "argv", ["post_boot_notify.py", "--dry-run", "--quiet"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_send.assert_called_once_with(
                chat_id=None,
                dry_run=True,
                as_json=False,
                quiet=True,
                uptime_path="/proc/uptime",
            )

    @patch("scripts.post_boot_notify.send_post_boot_notification")
    def test_cli_main_custom_chat_id(self, mock_send):
        mock_send.return_value = (True, {})
        with patch.object(sys, "argv", ["post_boot_notify.py", "--chat-id", "987654321", "--quiet"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_send.assert_called_once_with(
                chat_id="987654321",
                dry_run=False,
                as_json=False,
                quiet=True,
                uptime_path="/proc/uptime",
            )

    @patch("scripts.post_boot_notify.send_post_boot_notification")
    def test_cli_main_json(self, mock_send):
        mock_send.return_value = (True, {})
        with patch.object(sys, "argv", ["post_boot_notify.py", "--json", "--quiet"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_send.assert_called_once_with(
                chat_id=None,
                dry_run=False,
                as_json=True,
                quiet=True,
                uptime_path="/proc/uptime",
            )

    @patch("scripts.post_boot_notify.send_post_boot_notification")
    def test_cli_main_failure_exits_1(self, mock_send):
        mock_send.return_value = (False, {})
        with patch.object(sys, "argv", ["post_boot_notify.py", "--quiet"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

    @patch("scripts.post_boot_notify.send_post_boot_notification")
    def test_cli_main_custom_uptime_path(self, mock_send):
        mock_send.return_value = (True, {})
        with patch.object(sys, "argv", ["post_boot_notify.py", "--uptime-path", "/custom/uptime", "--quiet"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_send.assert_called_once_with(
                chat_id=None,
                dry_run=False,
                as_json=False,
                quiet=True,
                uptime_path="/custom/uptime",
            )


if __name__ == "__main__":
    unittest.main()
