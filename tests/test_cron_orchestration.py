#!/usr/bin/env python3
"""
Unit and integration tests for Scheduled Maintenance & Pre-Reboot Cron Orchestration.

Tests:
- Argument parsing, usage display, and validation of time/lead inputs.
- Calculation of cron expressions across all hour/minute boundaries including midnight rollover.
- Termux environment header injection (SHELL, PATH, HOME).
- Crontab installation, atomic update, and uninstallation preserving existing user cron jobs.
- Crond daemon checking and auto-supervision.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_CRON_SCRIPT = REPO_ROOT / "scripts" / "setup_cron.sh"


class TestCronOrchestration(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            SETUP_CRON_SCRIPT.exists(),
            f"setup_cron.sh must exist at {SETUP_CRON_SCRIPT}",
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        
        # Create mock bin directory for mocking crontab and crond
        self.mock_bin = self.test_dir / "bin"
        self.mock_bin.mkdir(parents=True, exist_ok=True)
        self.crontab_store = self.test_dir / "crontab.txt"
        self.crond_state = self.test_dir / "crond_running.txt"

        # Write mock crontab binary
        self.mock_crontab = self.mock_bin / "crontab"
        mock_crontab_code = f"""#!/usr/bin/env bash
STORE="{self.crontab_store}"
if [[ "$#" -eq 0 ]]; then
    cat > "$STORE"
    exit 0
elif [[ "$1" == "-l" ]]; then
    if [[ -f "$STORE" ]]; then
        cat "$STORE"
        exit 0
    else
        echo "crontab: no crontab for user" >&2
        exit 1
    fi
elif [[ "$1" == "-r" ]]; then
    rm -f "$STORE"
    exit 0
elif [[ -f "$1" ]]; then
    cat "$1" > "$STORE"
    exit 0
elif [[ "$1" == "/dev/null" ]]; then
    rm -f "$STORE"
    exit 0
else
    echo "Unknown option: $1" >&2
    exit 1
fi
"""
        self.mock_crontab.write_text(mock_crontab_code)
        self.mock_crontab.chmod(self.mock_crontab.stat().st_mode | stat.S_IEXEC)

        # Write mock crond binary
        self.mock_crond = self.mock_bin / "crond"
        mock_crond_code = f"""#!/usr/bin/env bash
STATE="{self.crond_state}"
if [[ "$1" == "-b" || "$#" -eq 0 ]]; then
    echo "1" > "$STATE"
    exit 0
elif [[ "$1" == "--stop" ]]; then
    rm -f "$STATE"
    exit 0
fi
"""
        self.mock_crond.write_text(mock_crond_code)
        self.mock_crond.chmod(self.mock_crond.stat().st_mode | stat.S_IEXEC)

        # Write mock pgrep binary to support daemon checks
        self.mock_pgrep = self.mock_bin / "pgrep"
        mock_pgrep_code = f"""#!/usr/bin/env bash
STATE="{self.crond_state}"
if [[ -f "$STATE" ]]; then
    echo "1234"
    exit 0
else
    exit 1
fi
"""
        self.mock_pgrep.write_text(mock_pgrep_code)
        self.mock_pgrep.chmod(self.mock_pgrep.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_setup_cron(self, args, env_overrides=None):
        """Helper to invoke setup_cron.sh with custom args and environment."""
        env = os.environ.copy()
        # Prepend mock_bin to PATH
        env["PATH"] = f"{self.mock_bin}:{env.get('PATH', '')}"
        env["CRONTAB_BIN"] = str(self.mock_crontab)
        env["CROND_BIN"] = str(self.mock_crond)
        if env_overrides:
            env.update(env_overrides)

        cmd = [str(SETUP_CRON_SCRIPT)] + args
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        return proc

    # --------------------------------------------------------------------------
    # 1. CLI Usage, Help, and Validation Tests
    # --------------------------------------------------------------------------
    def test_script_is_executable(self):
        st = SETUP_CRON_SCRIPT.stat()
        self.assertTrue(
            bool(st.st_mode & stat.S_IEXEC),
            f"{SETUP_CRON_SCRIPT} must have executable permissions",
        )

    def test_help_flag(self):
        res = self.run_setup_cron(["--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage:", res.stdout)
        self.assertIn("--install", res.stdout)
        self.assertIn("--reboot-time", res.stdout)
        self.assertIn("--lead-minutes", res.stdout)

    def test_unknown_option_fails(self):
        res = self.run_setup_cron(["--non-existent-flag"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Unknown argument", res.stderr)

    def test_invalid_time_format_fails(self):
        invalid_times = ["25:00", "12:60", "invalid", "-1:00", "12:", ":30", "12:34:56"]
        for t in invalid_times:
            res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", t])
            self.assertNotEqual(
                res.returncode,
                0,
                f"Expected failure for invalid reboot time: {t}",
            )

    def test_invalid_lead_minutes_fails(self):
        invalid_leads = ["0", "-5", "1440", "abc"]
        for lead in invalid_leads:
            res = self.run_setup_cron(["--install", "--dry-run", "--lead-minutes", lead])
            self.assertNotEqual(
                res.returncode,
                0,
                f"Expected failure for invalid lead minutes: {lead}",
            )

    # --------------------------------------------------------------------------
    # 2. Timing and Cron Math Calculation Tests
    # --------------------------------------------------------------------------
    def test_default_timing_calculation(self):
        # Default: 03:00 with 5 min lead -> 02:55 -> 55 2 * * *
        res = self.run_setup_cron(["--install", "--dry-run"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("55 2 * * *", res.stdout)
        self.assertIn("03:00 (5 min lead time -> 02:55)", res.stdout)

    def test_midnight_rollover_calculation(self):
        # 00:00 with 5 min lead -> 23:55 -> 55 23 * * *
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", "00:00", "--lead-minutes", "5"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("55 23 * * *", res.stdout)
        self.assertIn("00:00 (5 min lead time -> 23:55)", res.stdout)

    def test_single_digit_hour_and_minute_calculation(self):
        # 00:03 with 5 min lead -> 23:58 -> 58 23 * * *
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", "0:3", "--lead-minutes", "5"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("58 23 * * *", res.stdout)

        # 04:05 with 5 min lead -> 04:00 -> 0 4 * * *
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", "04:05", "--lead-minutes", "5"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("0 4 * * *", res.stdout)

    def test_large_lead_time_cross_hour(self):
        # 01:10 with 25 min lead -> 00:45 -> 45 0 * * *
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", "01:10", "--lead-minutes", "25"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("45 0 * * *", res.stdout)

    def test_custom_days_of_week(self):
        # Sunday only (0)
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", "03:00", "--days-of-week", "0"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("55 2 * * 0", res.stdout)

        # Mon-Fri (1-5)
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-time", "03:00", "--days-of-week", "1-5"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("55 2 * * 1-5", res.stdout)

    def test_reboot_hour_minute_flags(self):
        res = self.run_setup_cron(["--install", "--dry-run", "--reboot-hour", "5", "--reboot-minute", "30", "--lead-minutes", "10"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("20 5 * * *", res.stdout)

    # --------------------------------------------------------------------------
    # 3. Environment Headers and Termux Detection Tests
    # --------------------------------------------------------------------------
    def test_termux_environment_headers_auto_detection(self):
        termux_env = {
            "PREFIX": "/data/data/com.termux/files/usr",
            "HOME": "/data/data/com.termux/files/home",
        }
        res = self.run_setup_cron(["--install", "--dry-run"], env_overrides=termux_env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("SHELL=/data/data/com.termux/files/usr/bin/bash", res.stdout)
        self.assertIn("HOME=/data/data/com.termux/files/home", res.stdout)
        self.assertIn("PATH=/data/data/com.termux/files/usr/bin:", res.stdout)
        self.assertIn("/data/data/com.termux/files/home/eternal-automation-bot/scripts/pre_reboot_shutdown.sh", res.stdout)
        self.assertIn("/data/data/com.termux/files/home/pre_reboot.log", res.stdout)

    def test_custom_header_and_target_flags(self):
        res = self.run_setup_cron([
            "--install",
            "--dry-run",
            "--shell", "/custom/bin/bash",
            "--path", "/custom/bin:/bin",
            "--home", "/custom/home",
            "--script-path", "/custom/scripts/shutdown.sh",
            "--log-file", "/custom/logs/shutdown.log",
        ])
        self.assertEqual(res.returncode, 0)
        self.assertIn("SHELL=/custom/bin/bash", res.stdout)
        self.assertIn("PATH=/custom/bin:/bin", res.stdout)
        self.assertIn("HOME=/custom/home", res.stdout)
        self.assertIn("/custom/scripts/shutdown.sh >> /custom/logs/shutdown.log 2>&1", res.stdout)

    # --------------------------------------------------------------------------
    # 4. Crontab Install, Update, Idempotency, and Removal Integration Tests
    # --------------------------------------------------------------------------
    def test_install_into_empty_crontab(self):
        self.assertFalse(self.crontab_store.exists())
        res = self.run_setup_cron(["--install", "--reboot-time", "03:00", "--lead-minutes", "5"])
        self.assertEqual(res.returncode, 0)
        self.assertTrue(self.crontab_store.exists())

        content = self.crontab_store.read_text()
        self.assertIn("# >>> eternal-automation-bot pre-reboot maintenance >>>", content)
        self.assertIn("55 2 * * *", content)
        self.assertIn("# <<< eternal-automation-bot pre-reboot maintenance <<<", content)

    def test_install_idempotency_and_update(self):
        # Run install first time
        self.run_setup_cron(["--install", "--reboot-time", "03:00"])
        content_1 = self.crontab_store.read_text()
        self.assertEqual(content_1.count("# >>> eternal-automation-bot pre-reboot maintenance >>>"), 1)
        self.assertIn("55 2 * * *", content_1)

        # Run install second time with updated time
        self.run_setup_cron(["--install", "--reboot-time", "04:00"])
        content_2 = self.crontab_store.read_text()
        self.assertEqual(content_2.count("# >>> eternal-automation-bot pre-reboot maintenance >>>"), 1)
        self.assertIn("55 3 * * *", content_2)
        self.assertNotIn("55 2 * * *", content_2)

    def test_install_preserves_existing_user_cron_entries(self):
        # Pre-populate crontab with user jobs
        initial_crontab = (
            "# My user backup\n"
            "0 1 * * * /home/user/backup.sh\n\n"
            "# Health check\n"
            "*/10 * * * * /home/user/check.sh\n"
        )
        self.crontab_store.write_text(initial_crontab)

        # Install bot maintenance schedule
        res = self.run_setup_cron(["--install", "--reboot-time", "03:00"])
        self.assertEqual(res.returncode, 0)

        content = self.crontab_store.read_text()
        self.assertIn("0 1 * * * /home/user/backup.sh", content)
        self.assertIn("*/10 * * * * /home/user/check.sh", content)
        self.assertIn("# >>> eternal-automation-bot pre-reboot maintenance >>>", content)
        self.assertIn("55 2 * * *", content)

    def test_show_status(self):
        # Before install
        res_before = self.run_setup_cron(["--show"])
        self.assertEqual(res_before.returncode, 0)
        self.assertIn("No crontab entries found", res_before.stdout)

        # After install
        self.run_setup_cron(["--install", "--reboot-time", "03:00"])
        res_after = self.run_setup_cron(["--show"])
        self.assertEqual(res_after.returncode, 0)
        self.assertIn("Pre-reboot maintenance schedule is ACTIVE", res_after.stdout)
        self.assertIn("55 2 * * *", res_after.stdout)

    def test_remove_cleans_only_managed_block(self):
        # Setup crontab with both user entry and managed block
        initial_crontab = (
            "0 1 * * * /home/user/backup.sh\n\n"
            "# >>> eternal-automation-bot pre-reboot maintenance >>>\n"
            "SHELL=/bin/bash\n"
            "55 2 * * * /path/to/script.sh >> /log.log 2>&1\n"
            "# <<< eternal-automation-bot pre-reboot maintenance <<<\n\n"
            "30 5 * * * /home/user/other.sh\n"
        )
        self.crontab_store.write_text(initial_crontab)

        res = self.run_setup_cron(["--remove"])
        self.assertEqual(res.returncode, 0)

        content = self.crontab_store.read_text()
        self.assertNotIn("eternal-automation-bot pre-reboot maintenance", content)
        self.assertIn("0 1 * * * /home/user/backup.sh", content)
        self.assertIn("30 5 * * * /home/user/other.sh", content)

    def test_remove_when_crontab_has_only_managed_block(self):
        self.run_setup_cron(["--install", "--reboot-time", "03:00"])
        self.assertTrue(self.crontab_store.exists())

        res = self.run_setup_cron(["--remove"])
        self.assertEqual(res.returncode, 0)

        # Crontab store should either be removed or empty
        if self.crontab_store.exists():
            self.assertEqual(self.crontab_store.read_text().strip(), "")

    def test_remove_when_no_managed_block_exists(self):
        self.crontab_store.write_text("0 1 * * * /home/user/backup.sh\n")
        res = self.run_setup_cron(["--remove"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Nothing to remove", res.stdout)
        self.assertEqual(self.crontab_store.read_text(), "0 1 * * * /home/user/backup.sh\n")

    # --------------------------------------------------------------------------
    # 5. Daemon Supervision & Auto-Start Tests
    # --------------------------------------------------------------------------
    def test_check_daemon_when_not_running(self):
        if self.crond_state.exists():
            self.crond_state.unlink()
        res = self.run_setup_cron(["--check-daemon"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("NOT running", res.stdout)

    def test_check_daemon_when_running(self):
        self.crond_state.write_text("1")
        res = self.run_setup_cron(["--check-daemon"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("is RUNNING", res.stdout)

    def test_ensure_daemon_starts_crond(self):
        if self.crond_state.exists():
            self.crond_state.unlink()
        res = self.run_setup_cron(["--ensure-daemon"])
        self.assertEqual(res.returncode, 0)
        self.assertTrue(self.crond_state.exists())
        self.assertIn("successfully started", res.stdout)

    def test_ensure_daemon_when_already_running(self):
        self.crond_state.write_text("1")
        res = self.run_setup_cron(["--ensure-daemon"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("already running", res.stdout)


if __name__ == "__main__":
    unittest.main()
