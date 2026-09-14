"""
Unit and integration tests for Termux:Boot Multi-Bot Startup Suite and Watchdog Runner.
Tests scripts/termux_boot.sh and start.sh behavior.
"""

import os
import sys
import time
import signal
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
START_SH = REPO_ROOT / "start.sh"
TERMUX_BOOT_SH = SCRIPTS_DIR / "termux_boot.sh"


class TestWatchdogRunner(unittest.TestCase):
    """Tests for start.sh watchdog runner signal handling and sentinel detection."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="watchdog_test_")
        self.home_dir = tempfile.mkdtemp(prefix="fake_home_")
        self.test_start_sh = os.path.join(self.test_dir, "start.sh")
        shutil.copy2(START_SH, self.test_start_sh)
        os.chmod(self.test_start_sh, 0o755)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        shutil.rmtree(self.home_dir, ignore_errors=True)

    def test_watchdog_clean_exit_code_zero(self):
        """Watchdog should terminate cleanly without restarting if main.py exits with code 0."""
        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write("import sys; print('Running clean bot'); sys.exit(0)\n")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        proc = subprocess.run(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        self.assertEqual(proc.returncode, 0)
        log_file = os.path.join(self.test_dir, "bot.log")
        self.assertTrue(os.path.exists(log_file))
        with open(log_file, "r") as f:
            content = f.read()
        self.assertIn("Clean termination (Exit Code: 0)", content)

    def test_watchdog_clean_exit_code_130_and_143(self):
        """Watchdog should terminate cleanly on SIGINT (130) or SIGTERM (143) exit codes."""
        for code in (130, 143):
            main_py = os.path.join(self.test_dir, "main.py")
            with open(main_py, "w") as f:
                f.write(f"import sys; sys.exit({code})\n")

            env = os.environ.copy()
            env["HOME"] = self.home_dir

            proc = subprocess.run(
                ["bash", self.test_start_sh],
                cwd=self.test_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=10
            )
            self.assertEqual(proc.returncode, 0)
            log_file = os.path.join(self.test_dir, "bot.log")
            with open(log_file, "r") as f:
                content = f.read()
            self.assertIn(f"Clean termination (Exit Code: {code})", content)

    def test_watchdog_shutdown_sentinel_detection(self):
        """Watchdog should detect $HOME/.shutdown_in_progress and exit without starting/restarting."""
        sentinel = os.path.join(self.home_dir, ".shutdown_in_progress")
        with open(sentinel, "w") as f:
            f.write("shutdown requested\n")

        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write("import sys; sys.exit(1)\n")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        proc = subprocess.run(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        self.assertEqual(proc.returncode, 0)
        log_file = os.path.join(self.test_dir, "bot.log")
        with open(log_file, "r") as f:
            content = f.read()
        self.assertIn("Shutdown sentinel detected", content)

    def test_watchdog_restarts_on_unexpected_crash(self):
        """Watchdog should restart the process on unexpected crash (non-zero) until clean exit."""
        state_file = os.path.join(self.test_dir, "run_count.txt")
        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write(f"""
import os, sys
count_file = '{state_file}'
count = 0
if os.path.exists(count_file):
    with open(count_file, 'r') as f:
        count = int(f.read().strip())
count += 1
with open(count_file, 'w') as f:
    f.write(str(count))

if count == 1:
    sys.exit(42)
else:
    sys.exit(0)
""")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        proc = subprocess.run(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=15
        )
        self.assertEqual(proc.returncode, 0)
        with open(state_file, "r") as f:
            final_count = int(f.read().strip())
        self.assertEqual(final_count, 2)

        log_file = os.path.join(self.test_dir, "bot.log")
        with open(log_file, "r") as f:
            content = f.read()
        self.assertIn("Process exited (Code: 42). Restarting in 2s...", content)
        self.assertIn("Clean termination (Exit Code: 0)", content)

    def test_watchdog_signal_forwarding_sigterm(self):
        """Sending SIGTERM to watchdog process should forward SIGTERM to child and exit cleanly."""
        flag_file = os.path.join(self.test_dir, "child_received_sigterm.txt")
        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write(f"""
import signal, time, sys

def handler(sig, frame):
    with open('{flag_file}', 'w') as f:
        f.write('SIGTERM_RECEIVED')
    sys.exit(143)

signal.signal(signal.SIGTERM, handler)
time.sleep(10)
""")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        proc = subprocess.Popen(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env
        )

        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(flag_file), "Child should have received forwarded SIGTERM")
        with open(flag_file, "r") as f:
            self.assertEqual(f.read().strip(), "SIGTERM_RECEIVED")

    def test_watchdog_signal_forwarding_sigint(self):
        """Sending SIGINT to watchdog process should forward SIGINT to child and exit cleanly."""
        flag_file = os.path.join(self.test_dir, "child_received_sigint.txt")
        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write(f"""
import signal, time, sys

def handler(sig, frame):
    with open('{flag_file}', 'w') as f:
        f.write('SIGINT_RECEIVED')
    sys.exit(130)

signal.signal(signal.SIGINT, handler)
time.sleep(10)
""")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        proc = subprocess.Popen(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env
        )

        time.sleep(1.0)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(flag_file), "Child should have received forwarded SIGINT")
        with open(flag_file, "r") as f:
            self.assertEqual(f.read().strip(), "SIGINT_RECEIVED")

    def test_watchdog_signal_forwarding_sighup(self):
        """Sending SIGHUP to watchdog process should forward signal to child and exit cleanly."""
        flag_file = os.path.join(self.test_dir, "child_received_sighup.txt")
        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write(f"""
import signal, time, sys

def handler(sig, frame):
    with open('{flag_file}', 'w') as f:
        f.write('SIGHUP_RECEIVED')
    sys.exit(129)

signal.signal(signal.SIGHUP, handler)
time.sleep(10)
""")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        proc = subprocess.Popen(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env
        )

        time.sleep(1.0)
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=10)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(flag_file), "Child should have received forwarded SIGHUP")
        with open(flag_file, "r") as f:
            self.assertEqual(f.read().strip(), "SIGHUP_RECEIVED")

    def test_watchdog_log_rotation(self):
        """Watchdog should rotate bot.log to bot.log.old when size exceeds MAX_LOG_SIZE."""
        log_file = os.path.join(self.test_dir, "bot.log")
        with open(log_file, "wb") as f:
            f.write(b"A" * (5242880 + 1024))

        main_py = os.path.join(self.test_dir, "main.py")
        with open(main_py, "w") as f:
            f.write("import sys; sys.exit(0)\n")

        env = os.environ.copy()
        env["HOME"] = self.home_dir

        subprocess.run(
            ["bash", self.test_start_sh],
            cwd=self.test_dir,
            env=env,
            timeout=10
        )

        old_log = os.path.join(self.test_dir, "bot.log.old")
        self.assertTrue(os.path.exists(old_log), "bot.log.old should exist after rotation")
        self.assertGreaterEqual(os.path.getsize(old_log), 5242880)


class TestTermuxBootScript(unittest.TestCase):
    """Tests for scripts/termux_boot.sh startup sequence and subcomponents."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="boot_test_")
        self.home_dir = os.path.join(self.test_dir, "home")
        self.prefix_dir = os.path.join(self.test_dir, "usr")
        os.makedirs(self.home_dir, exist_ok=True)
        os.makedirs(os.path.join(self.prefix_dir, "bin"), exist_ok=True)
        os.makedirs(os.path.join(self.prefix_dir, "tmp"), exist_ok=True)

        self.test_boot_sh = os.path.join(self.test_dir, "termux_boot.sh")
        shutil.copy2(TERMUX_BOOT_SH, self.test_boot_sh)
        os.chmod(self.test_boot_sh, 0o755)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_script_exists_and_executable(self):
        """termux_boot.sh must exist and have executable permissions."""
        self.assertTrue(os.path.exists(TERMUX_BOOT_SH))
        self.assertTrue(os.access(TERMUX_BOOT_SH, os.X_OK))

    def test_bash_syntax_validity(self):
        """termux_boot.sh and start.sh must pass bash syntax check."""
        for script in [TERMUX_BOOT_SH, START_SH]:
            res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Syntax error in {script}: {res.stderr}")

    def test_environment_initialization(self):
        """termux_boot.sh initializes standard non-interactive Termux environment variables."""
        env_dump_file = os.path.join(self.test_dir, "env_dump.txt")
        with open(self.test_boot_sh, "r") as f:
            content = f.read()

        hooked = content.replace(
            'export LANG="${LANG:-en_US.UTF-8}"',
            f'export LANG="${{LANG:-en_US.UTF-8}}"\nenv > "{env_dump_file}"'
        )
        with open(self.test_boot_sh, "w") as f:
            f.write(hooked)

        env = os.environ.copy()
        env.pop("TMPDIR", None)
        env.pop("LANG", None)
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["BOOT_NETWORK_TIMEOUT"] = "1"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(env_dump_file))
        with open(env_dump_file, "r") as f:
            dump = f.read()
        self.assertIn(f"PREFIX={self.prefix_dir}", dump)
        self.assertIn(f"TMPDIR={self.prefix_dir}/tmp", dump)
        self.assertIn("LANG=en_US.UTF-8", dump)

    def test_stale_lock_and_tmp_cleanup(self):
        """termux_boot.sh should clean up stale .lock, .tmp, downloads, and .shutdown_in_progress."""
        bot_dir = os.path.join(self.home_dir, "eternal-automation-bot")
        feed_dir = os.path.join(self.home_dir, "eternal-feed-bot")
        heart_dir = os.path.join(self.home_dir, "eternal-heartcast-bot")

        for d in [bot_dir, feed_dir, heart_dir]:
            os.makedirs(os.path.join(d, "downloads"), exist_ok=True)
            Path(d, "test.lock").touch()
            Path(d, ".feedbot.lock").touch()
            Path(d, "test.tmp").touch()
            Path(d, ".users.tmp").touch()
            Path(d, "downloads", "partial_file.mp4").touch()

        sentinel = os.path.join(self.home_dir, ".shutdown_in_progress")
        Path(sentinel).touch()

        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["PROJECT_ROOT"] = self.home_dir
        env["BOOT_NETWORK_TIMEOUT"] = "1"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        proc = subprocess.run(
            ["bash", self.test_boot_sh],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        self.assertEqual(proc.returncode, 0)

        for d in [bot_dir, feed_dir, heart_dir]:
            self.assertFalse(os.path.exists(os.path.join(d, "test.lock")))
            self.assertFalse(os.path.exists(os.path.join(d, ".feedbot.lock")))
            self.assertFalse(os.path.exists(os.path.join(d, "test.tmp")))
            self.assertFalse(os.path.exists(os.path.join(d, ".users.tmp")))
            self.assertFalse(os.path.exists(os.path.join(d, "downloads", "partial_file.mp4")))

        self.assertFalse(os.path.exists(sentinel))

    def test_wake_lock_graceful_handling(self):
        """termux_boot.sh should invoke termux-wake-lock when present, or skip if not found."""
        wake_lock_called = os.path.join(self.test_dir, "wake_lock_called.txt")
        mock_bin = os.path.join(self.prefix_dir, "bin", "termux-wake-lock")
        with open(mock_bin, "w") as f:
            f.write(f"#!/bin/sh\necho WAKE_LOCK_OK > '{wake_lock_called}'\nexit 0\n")
        os.chmod(mock_bin, 0o755)

        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["PATH"] = f"{os.path.join(self.prefix_dir, 'bin')}:{env['PATH']}"
        env["BOOT_NETWORK_TIMEOUT"] = "1"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(wake_lock_called))

    def test_crond_auto_supervision(self):
        """termux_boot.sh should attempt to start crond -b if not running."""
        crond_called = os.path.join(self.test_dir, "crond_called.txt")
        mock_crond = os.path.join(self.prefix_dir, "bin", "crond")
        with open(mock_crond, "w") as f:
            f.write(f"#!/bin/sh\necho CROND_STARTED > '{crond_called}'\nexit 0\n")
        os.chmod(mock_crond, 0o755)

        mock_pgrep = os.path.join(self.prefix_dir, "bin", "pgrep")
        with open(mock_pgrep, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(mock_pgrep, 0o755)

        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["PATH"] = f"{os.path.join(self.prefix_dir, 'bin')}:{env['PATH']}"
        env["BOOT_NETWORK_TIMEOUT"] = "1"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(crond_called))

    def test_network_poller_online_detection(self):
        """Network poller should detect connectivity quickly when network is available."""
        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["BOOT_NETWORK_TIMEOUT"] = "5"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        start_time = time.time()
        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        elapsed = time.time() - start_time

        self.assertEqual(proc.returncode, 0)
        with open(os.path.join(self.home_dir, "boot.log"), "r") as f:
            log_content = f.read()
        self.assertIn("Network is ONLINE", log_content)

    def test_network_poller_timeout_resilience(self):
        """Network poller should time out after BOOT_NETWORK_TIMEOUT in mock offline mode."""
        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["BOOT_MOCK_NETWORK"] = "offline"
        env["BOOT_NETWORK_TIMEOUT"] = "2"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        start_time = time.time()
        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        elapsed = time.time() - start_time

        self.assertEqual(proc.returncode, 0)
        self.assertGreaterEqual(elapsed, 1.8)
        with open(os.path.join(self.home_dir, "boot.log"), "r") as f:
            log_content = f.read()
        self.assertIn("Network wait timed out", log_content)

    def test_tmux_multi_session_spawner(self):
        """termux_boot.sh should spawn independent tmux sessions for bot, feed-bot, and heartcast."""
        bot_dir = os.path.join(self.home_dir, "eternal-automation-bot")
        feed_dir = os.path.join(self.home_dir, "eternal-feed-bot")
        heart_dir = os.path.join(self.home_dir, "eternal-heartcast-bot")

        for d in [bot_dir, feed_dir, heart_dir]:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "start.sh"), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(d, "start.sh"), 0o755)

        tmux_log = os.path.join(self.test_dir, "tmux_calls.log")
        mock_tmux = os.path.join(self.prefix_dir, "bin", "tmux")
        with open(mock_tmux, "w") as f:
            f.write(f"""#!/bin/sh
echo "TMUX_ARGS: $@" >> '{tmux_log}'
if [ "$1" = "has-session" ]; then
    exit 1
fi
exit 0
""")
        os.chmod(mock_tmux, 0o755)

        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["PROJECT_ROOT"] = self.home_dir
        env["PATH"] = f"{os.path.join(self.prefix_dir, 'bin')}:{env['PATH']}"
        env["BOOT_NETWORK_TIMEOUT"] = "1"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(tmux_log))
        with open(tmux_log, "r") as f:
            calls = f.read()
        self.assertIn("new-session -d -s bot", calls)
        self.assertIn("new-session -d -s feed-bot", calls)
        self.assertIn("new-session -d -s heartcast", calls)

    def test_post_boot_notify_invocation(self):
        """termux_boot.sh should invoke post_boot_notify.py if found."""
        bot_dir = os.path.join(self.home_dir, "eternal-automation-bot")
        scripts_sub = os.path.join(bot_dir, "scripts")
        os.makedirs(scripts_sub, exist_ok=True)

        notify_flag = os.path.join(self.test_dir, "notify_ran.txt")
        mock_notify = os.path.join(scripts_sub, "post_boot_notify.py")
        with open(mock_notify, "w") as f:
            f.write(f"import os\nwith open('{notify_flag}', 'w') as f: f.write('NOTIFIED')\n")

        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env["PREFIX"] = self.prefix_dir
        env["PROJECT_ROOT"] = self.home_dir
        env["BOOT_NETWORK_TIMEOUT"] = "1"
        env["BOOT_LOG"] = os.path.join(self.home_dir, "boot.log")

        proc = subprocess.run(["bash", self.test_boot_sh], env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(notify_flag))
        with open(notify_flag, "r") as f:
            self.assertEqual(f.read().strip(), "NOTIFIED")


if __name__ == "__main__":
    unittest.main()
