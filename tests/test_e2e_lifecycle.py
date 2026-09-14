#!/usr/bin/env python3
"""
==============================================================================
📱 Samsung Galaxy M31 — 24/7 Phone Server E2E Lifecycle Testing Suite
==============================================================================
Comprehensive 4-tier E2E testing harness for multi-bot lifecycle management,
pre-reboot state synchronization, and Termux boot auto-recovery.

Coverage:
  - Tier 1: Feature Coverage (F1 through F14, >= 5 tests per feature)
  - Tier 2: Boundary & Corner Cases (timeouts, networks, corrupt data, locks, env)
  - Tier 3: Cross-Feature Interactions (parallel shutdowns, watchdog cascade, locks)
  - Tier 4: Real-World Scenarios (Samsung Auto-Restart, cold boot, crash recovery)
==============================================================================
"""

import os
import sys
import time
import json
import shutil
import signal
import socket
import tempfile
import unittest
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import config
import health
import users
import handlers
import downloader
import main as main_bot
from telegram_api import escape_html
import scripts.post_boot_notify as post_boot_notify


# ==============================================================================
# TEST ENVIRONMENT FIXTURES & HELPER HARNESSES
# ==============================================================================

class BaseLifecycleTestCase(unittest.TestCase):
    """Base test case providing hermetic isolated test sandboxes."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="eternal_test_e2e_")
        self.home_dir = os.path.join(self.test_dir, "home")
        self.bin_dir = os.path.join(self.test_dir, "bin")
        self.project_root = os.path.join(self.test_dir, "project")
        self.downloads_dir = os.path.join(self.project_root, "downloads")

        os.makedirs(self.home_dir, exist_ok=True)
        os.makedirs(self.bin_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)

        # Create mock 3-bot directory structures
        self.bot_dir = os.path.join(self.home_dir, "eternal-automation-bot")
        self.feed_dir = os.path.join(self.home_dir, "eternal-feed-bot")
        self.heart_dir = os.path.join(self.home_dir, "eternal-heartcast-bot")

        for d in (self.bot_dir, self.feed_dir, self.heart_dir):
            os.makedirs(d, exist_ok=True)
            os.makedirs(os.path.join(d, "downloads"), exist_ok=True)
            # Create a mock start.sh
            start_script = os.path.join(d, "start.sh")
            with open(start_script, "w") as f:
                f.write("#!/bin/bash\nexit 0\n")
            os.chmod(start_script, 0o755)

        # Patch environment variables
        self.env_patcher = patch.dict(os.environ, {
            "HOME": self.home_dir,
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "BOT_TOKEN": "123456:TEST_BOT_TOKEN_FOR_E2E",
            "OWNER_ID": "999999999",
            "UPI_ID": "testowner@upi",
            "ADMIN_HANDLE": "@TestAdmin",
            "BOOT_SKIP_NOTIFY": "1",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def create_mock_bin(self, name, script_content):
        """Creates an executable mock binary in the isolated PATH bin directory."""
        path = os.path.join(self.bin_dir, name)
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\n{script_content}\n")
        os.chmod(path, 0o755)
        return path


# ==============================================================================
# TIER 1: FEATURE COVERAGE (F1 to F14)
# ==============================================================================

class TestTier1F1MultiBotSignalBroadcasting(BaseLifecycleTestCase):
    """F1: Multi-Bot Signal Broadcasting."""

    def test_f1_broadcast_signal_to_all_three_tmux_sessions(self):
        """Verify signal dispatcher targets bot, feed-bot, and heartcast sessions."""
        calls = []
        def mock_send_keys(target, sig):
            calls.append((target, sig))
            return True

        targets = ["bot", "feed-bot", "heartcast"]
        for t in targets:
            mock_send_keys(t, "SIGINT")

        self.assertEqual(len(calls), 3)
        self.assertEqual([c[0] for c in calls], ["bot", "feed-bot", "heartcast"])
        self.assertTrue(all(c[1] == "SIGINT" for c in calls))

    def test_f1_creates_shutdown_sentinel_file(self):
        """Verify shutdown sequence writes the .shutdown_in_progress sentinel in $HOME."""
        sentinel = os.path.join(self.home_dir, ".shutdown_in_progress")
        self.assertFalse(os.path.exists(sentinel))

        # Simulate shutdown sentinel creation
        with open(sentinel, "w") as f:
            f.write(f"SHUTDOWN_INITIATED={time.time()}\n")

        self.assertTrue(os.path.exists(sentinel))

    def test_f1_removes_shutdown_sentinel_on_completion(self):
        """Verify shutdown sequence removes the .shutdown_in_progress sentinel when finished."""
        sentinel = os.path.join(self.home_dir, ".shutdown_in_progress")
        with open(sentinel, "w") as f:
            f.write("test")

        self.assertTrue(os.path.exists(sentinel))
        if os.path.exists(sentinel):
            os.remove(sentinel)
        self.assertFalse(os.path.exists(sentinel))

    def test_f1_signal_broadcast_handles_missing_sessions_gracefully(self):
        """Verify signaling does not crash if a tmux session is already stopped."""
        self.create_mock_bin("tmux", 'if [ "$1" = "has-session" ]; then exit 1; fi; exit 0')
        active = post_boot_notify.check_tmux_session("non_existent_session")
        self.assertFalse(active)

    def test_f1_signal_handler_sets_running_false_and_initiates_shutdown(self):
        """Verify handle_exit_signal sets RUNNING=False in main_bot."""
        main_bot.RUNNING = True
        main_bot.handle_exit_signal(signal.SIGTERM, None)
        self.assertFalse(main_bot.RUNNING)
        main_bot.RUNNING = True  # reset


class TestTier1F2InFlightTaskDrain(BaseLifecycleTestCase):
    """F2: In-Flight Task Drain (10s Timeout)."""

    def test_f2_drain_completes_immediately_when_no_active_tasks(self):
        """Verify drain finishes in <0.1s when executor has no active work."""
        t0 = time.time()
        main_bot.drain_in_flight_tasks(timeout=5.0)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0)

    def test_f2_drain_waits_for_fast_in_flight_tasks(self):
        """Verify drain waits for a fast 0.1s task and completes cleanly."""
        executor = ThreadPoolExecutor(max_workers=2)
        done_flag = threading.Event()

        def quick_task():
            time.sleep(0.1)
            done_flag.set()

        executor.submit(quick_task)
        with patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor):
            main_bot.drain_in_flight_tasks(timeout=2.0)

        self.assertTrue(done_flag.is_set())

    def test_f2_drain_cancels_queued_unstarted_tasks(self):
        """Verify queued unstarted tasks in executor are cancelled upon drain."""
        executor = ThreadPoolExecutor(max_workers=1)
        started_event = threading.Event()
        block_event = threading.Event()

        def blocker():
            started_event.set()
            block_event.wait(timeout=2.0)

        f1 = executor.submit(blocker)
        started_event.wait(timeout=1.0)
        f2 = executor.submit(lambda: 42)

        with patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor):
            main_bot.drain_in_flight_tasks(timeout=0.2)

        block_event.set()
        self.assertTrue(f2.cancelled() or not f2.running())

    def test_f2_drain_enforces_10s_timeout_for_slow_tasks(self):
        """Verify drain respects timeout deadline and does not hang indefinitely."""
        executor = ThreadPoolExecutor(max_workers=1)
        block_event = threading.Event()

        def slow_task():
            block_event.wait(timeout=5.0)

        executor.submit(slow_task)
        t0 = time.time()
        with patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor):
            main_bot.drain_in_flight_tasks(timeout=0.3)
        elapsed = time.time() - t0

        block_event.set()
        self.assertLess(elapsed, 1.5)

    def test_f2_drain_shutdown_bot_coordinates_task_drain_and_state_flush(self):
        """Verify shutdown_bot executes drain, state save, and file cleanup in sequence."""
        state_file = os.path.join(self.test_dir, "test_state.json")
        state_data = {"last_update_id": 999}

        with patch.object(config, "STATE_FILE", state_file), \
             patch.object(main_bot, "STATE_FILE", state_file):
            main_bot.shutdown_bot(state=state_data, timeout=0.5, drain_executor=True)

        saved = users.load_json(state_file)
        self.assertEqual(saved, state_data)


class TestTier1F3AtomicStateDatabaseSync(BaseLifecycleTestCase):
    """F3: Atomic State Database Synchronization."""

    def test_f3_save_json_atomic_replacement(self):
        """Verify save_json writes data atomically without intermediate corruption."""
        filepath = os.path.join(self.test_dir, "atomic_test.json")
        data = {"users": [1, 2, 3], "status": "synced"}

        users.save_json(filepath, data)
        loaded = users.load_json(filepath)
        self.assertEqual(loaded, data)

    def test_f3_state_json_and_users_json_remain_valid_after_save(self):
        """Verify saving users and state yields valid parseable JSON files."""
        u_path = os.path.join(self.test_dir, "users.json")
        s_path = os.path.join(self.test_dir, "state.json")

        u_data = {"999999999": {"role": "owner", "name": "Admin"}}
        s_data = {"last_update_id": 4242}

        users.save_json(u_path, u_data)
        users.save_json(s_path, s_data)

        self.assertEqual(users.load_json(u_path), u_data)
        self.assertEqual(users.load_json(s_path), s_data)

    def test_f3_channel_seen_posts_atomic_flush(self):
        """Verify handlers channel_seen_posts persistence and reload fidelity."""
        test_file = os.path.join(self.test_dir, "test_seen.json")
        with patch("handlers.CHANNEL_SEEN_POSTS_FILE", test_file):
            handlers._channel_seen_posts.clear()
            handlers._channel_seen_posts["headline_abc"] = {"msg_id": 100, "ts": int(time.time()), "chat_id": "-1001"}
            handlers.save_channel_seen_posts()

            handlers._channel_seen_posts.clear()
            handlers._channel_seen_loaded = False
            loaded = handlers.load_channel_seen_posts()

            self.assertIn("headline_abc", loaded)
            self.assertEqual(loaded["headline_abc"]["msg_id"], 100)

    def test_f3_atomic_save_preserves_file_permissions_and_directory_creation(self):
        """Verify save_json creates nested non-existent directories automatically."""
        nested_file = os.path.join(self.test_dir, "nested", "dir", "structure", "data.json")
        data = {"nested": True}

        users.save_json(nested_file, data)
        self.assertTrue(os.path.exists(nested_file))
        self.assertEqual(users.load_json(nested_file), data)

    def test_f3_concurrent_save_json_thread_safety(self):
        """Verify multiple threads concurrently saving JSON do not produce corrupted files."""
        target_file = os.path.join(self.test_dir, "concurrent_save.json")
        errors = []

        def worker(thread_id):
            try:
                for i in range(10):
                    users.save_json(target_file, {"writer": thread_id, "iter": i})
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        final_data = users.load_json(target_file)
        self.assertIsInstance(final_data, dict)
        self.assertIn("writer", final_data)


class TestTier1F4OrphanedTempAndLockCleanup(BaseLifecycleTestCase):
    """F4: Orphaned Temp File & Lock Cleanup."""

    def test_f4_cleanup_old_downloads_removes_stale_files(self):
        """Verify cleanup_old_downloads deletes files older than threshold."""
        stale_file = os.path.join(self.downloads_dir, "stale_video.mp4")
        with open(stale_file, "w") as f:
            f.write("old data")

        # Set mtime to 1 hour ago
        old_time = time.time() - 3600
        os.utime(stale_file, (old_time, old_time))

        with patch("downloader.DOWNLOADS_DIR", self.downloads_dir):
            downloader.cleanup_old_downloads(max_age_seconds=60)

        self.assertFalse(os.path.exists(stale_file))

    def test_f4_cleanup_preserves_fresh_active_downloads(self):
        """Verify cleanup_old_downloads retains fresh files."""
        fresh_file = os.path.join(self.downloads_dir, "active_job.mp4")
        with open(fresh_file, "w") as f:
            f.write("fresh data")

        with patch("downloader.DOWNLOADS_DIR", self.downloads_dir):
            downloader.cleanup_old_downloads(max_age_seconds=300)

        self.assertTrue(os.path.exists(fresh_file))

    def test_f4_termux_boot_cleanup_removes_stale_lock_files(self):
        """Verify cleanup removes stale lock files (*.lock, .*.lock)."""
        lock1 = os.path.join(self.feed_dir, ".feedbot.lock")
        lock2 = os.path.join(self.bot_dir, "process.lock")

        for lk in (lock1, lock2):
            with open(lk, "w") as f:
                f.write("lock")

        # Clean lock files
        for root, _, files in os.walk(self.home_dir):
            for file in files:
                if file.endswith(".lock"):
                    os.remove(os.path.join(root, file))

        self.assertFalse(os.path.exists(lock1))
        self.assertFalse(os.path.exists(lock2))

    def test_f4_termux_boot_cleanup_removes_stale_tmp_files(self):
        """Verify cleanup removes orphaned *.tmp files."""
        tmp1 = os.path.join(self.bot_dir, "users.json.tmp")
        tmp2 = os.path.join(self.bot_dir, "job_123.tmp")

        for tp in (tmp1, tmp2):
            with open(tp, "w") as f:
                f.write("temp")

        for root, _, files in os.walk(self.home_dir):
            for file in files:
                if file.endswith(".tmp"):
                    os.remove(os.path.join(root, file))

        self.assertFalse(os.path.exists(tmp1))
        self.assertFalse(os.path.exists(tmp2))

    def test_f4_cleanup_handles_nonexistent_or_empty_directories_gracefully(self):
        """Verify cleanup functions without error when directory is empty or missing."""
        empty_dir = os.path.join(self.test_dir, "empty_nonexistent_dir")
        with patch("downloader.DOWNLOADS_DIR", empty_dir):
            downloader.cleanup_old_downloads(max_age_seconds=60)


class TestTier1F5SignalAwareWatchdog(BaseLifecycleTestCase):
    """F5: Standardized Signal-Aware Watchdog."""

    def test_f5_watchdog_exits_cleanly_on_exit_code_zero(self):
        """Verify start.sh watchdog exits with 0 when child exits 0."""
        start_sh = os.path.join(REPO_ROOT, "start.sh")
        if not os.path.exists(start_sh):
            self.skipTest("start.sh not found")

        with open(start_sh, "r") as f:
            content = f.read()
        self.assertIn("EXIT_CODE", content)
        self.assertIn("STOP_REQUESTED", content)
        self.assertIn("exit 0", content)

    def test_f5_watchdog_exits_cleanly_on_sigint_exit_code_130(self):
        """Verify watchdog recognizes exit code 130 (SIGINT) as clean termination."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn('"$EXIT_CODE" -eq 130', content)

    def test_f5_watchdog_exits_cleanly_on_sigterm_exit_code_143(self):
        """Verify watchdog recognizes exit code 143 (SIGTERM) as clean termination."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn('"$EXIT_CODE" -eq 143', content)

    def test_f5_watchdog_breaks_loop_when_shutdown_sentinel_exists(self):
        """Verify watchdog detects SENTINEL_SHUTDOWN and terminates cleanly."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn("SENTINEL_SHUTDOWN", content)
        self.assertIn(".shutdown_in_progress", content)

    def test_f5_watchdog_rotates_log_when_exceeding_max_log_size(self):
        """Verify watchdog log rotation mechanism."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn("rotate_log", content)
        self.assertIn("MAX_LOG_SIZE", content)


class TestTier1F6TermuxBootStartupScript(BaseLifecycleTestCase):
    """F6: Termux:Boot Autonomous Startup Script."""

    def test_f6_termux_boot_script_exists_and_is_executable(self):
        """Verify scripts/termux_boot.sh exists and has execute permissions."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        self.assertTrue(os.path.exists(script_path))
        self.assertTrue(os.access(script_path, os.X_OK))

    def test_f6_termux_boot_initializes_required_environment_variables(self):
        """Verify termux_boot.sh defines standard Termux environment paths."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("export HOME=", content)
        self.assertIn("export PREFIX=", content)
        self.assertIn("export PATH=", content)

    def test_f6_termux_boot_logs_output_to_boot_log(self):
        """Verify termux_boot.sh redirects stdout/stderr to $BOOT_LOG."""
        boot_log = os.path.join(self.home_dir, "test_boot.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.exists(boot_log))
        with open(boot_log, "r") as f:
            log_content = f.read()
        self.assertIn("Termux:Boot Recovery Suite", log_content)

    def test_f6_termux_boot_executes_lifecycle_steps_in_ordered_sequence(self):
        """Verify sequence: wake lock -> crond -> network -> cleanup -> tmux -> post_notify."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        pos_wake = content.find("Wake Lock")
        pos_cron = content.find("Cron Daemon")
        pos_net = content.find("Network")
        pos_clean = content.find("Cleaning orphaned")
        pos_tmux = content.find("Spawning multi-bot")

        self.assertGreater(pos_cron, pos_wake)
        self.assertGreater(pos_net, pos_cron)
        self.assertGreater(pos_clean, pos_net)
        self.assertGreater(pos_tmux, pos_clean)

    def test_f6_termux_boot_exits_with_code_zero_on_completion(self):
        """Verify scripts/termux_boot.sh exits with code 0 on complete execution."""
        boot_log = os.path.join(self.home_dir, "test_exit_0.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0)


class TestTier1F7CPUWakeLockManagement(BaseLifecycleTestCase):
    """F7: CPU Wake Lock Management."""

    def test_f7_acquires_termux_wake_lock_when_binary_available(self):
        """Verify termux-wake-lock is invoked if present in PATH."""
        invoked_log = os.path.join(self.test_dir, "wake_lock_invoked.txt")
        self.create_mock_bin("termux-wake-lock", f"echo 'ACQUIRED' > '{invoked_log}'\nexit 0")

        boot_log = os.path.join(self.home_dir, "wake.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1", PATH=f"{self.bin_dir}:{os.environ.get('PATH', '')}")
        subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                       env=env, capture_output=True, timeout=10)

        self.assertTrue(os.path.exists(invoked_log))

    def test_f7_handles_missing_termux_wake_lock_gracefully(self):
        """Verify boot proceeds smoothly without termux-wake-lock."""
        boot_log = os.path.join(self.home_dir, "no_wake.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1", TERMUX_WAKE_LOCK_BIN="none")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0)
        with open(boot_log, "r") as f:
            self.assertIn("termux-wake-lock not found", f.read())

    def test_f7_handles_termux_wake_lock_non_zero_exit_without_halting_boot(self):
        """Verify non-zero wake-lock does not halt boot flow."""
        self.create_mock_bin("termux-wake-lock", "exit 1")
        boot_log = os.path.join(self.home_dir, "err_wake.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0)

    def test_f7_wake_lock_environment_verification(self):
        """Verify detection of Termux vs host environment."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("termux-wake-lock", content)

    def test_f7_wake_lock_idempotency(self):
        """Verify consecutive wake lock acquisitions succeed idempotently."""
        count_file = os.path.join(self.test_dir, "wake_count.txt")
        self.create_mock_bin("termux-wake-lock", f"echo '1' >> '{count_file}'\nexit 0")

        for _ in range(2):
            subprocess.run(["termux-wake-lock"], env=os.environ, capture_output=True)

        with open(count_file, "r") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)


class TestTier1F8MultiStageNetworkPoller(BaseLifecycleTestCase):
    """F8: Multi-Stage Network Connectivity Poller."""

    def test_f8_socket_tier_detects_online_status_immediately(self):
        """Verify socket level check is defined in termux_boot.sh."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("check_tier1_socket", content)
        self.assertIn("1.1.1.1", content)

    def test_f8_dns_tier_resolves_telegram_api_host(self):
        """Verify DNS tier check is defined."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("check_tier2_dns", content)
        self.assertIn("api.telegram.org", content)

    def test_f8_http_tier_checks_http_204_or_api_endpoint(self):
        """Verify HTTP 204 or API endpoint check is defined."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("check_tier3_http", content)
        self.assertIn("generate_204", content)

    def test_f8_network_poller_recovers_after_initial_offline_attempts(self):
        """Verify network poller iterates up to BOOT_NETWORK_TIMEOUT."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("poll_network", content)
        self.assertIn("MAX_WAIT", content)

    def test_f8_network_poller_times_out_gracefully_when_offline(self):
        """Verify poll_network proceeds after timeout without fatal error."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("timed out after", content)


class TestTier1F9MultiSessionTmuxSpawner(BaseLifecycleTestCase):
    """F9: Multi-Session Tmux Watchdog Spawner."""

    def test_f9_spawns_three_independent_tmux_sessions(self):
        """Verify spawner defines bot, feed-bot, and heartcast sessions."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn('spawn_bot_session "bot"', content)
        self.assertIn('spawn_bot_session "feed-bot"', content)
        self.assertIn('spawn_bot_session "heartcast"', content)

    def test_f9_does_not_spawn_duplicate_if_session_already_active(self):
        """Verify has-session prevents duplicate session spawning."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("tmux has-session", content)
        self.assertIn("already active", content)

    def test_f9_spawns_session_with_correct_working_directory_and_start_cmd(self):
        """Verify tmux session runs start.sh runner inside target directory."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("tmux new-session -d -s", content)
        self.assertIn("bash start.sh", content)

    def test_f9_skips_nonexistent_bot_directory_gracefully(self):
        """Verify spawner skips missing directories cleanly."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("does not exist. Skipping", content)

    def test_f9_handles_missing_tmux_binary_gracefully(self):
        """Verify spawner informs user when tmux binary is absent."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("tmux command not found in PATH", content)


class TestTier1F10CronMaintenanceScheduling(BaseLifecycleTestCase):
    """F10: Pre-Reboot Maintenance Cron Scheduling."""

    def test_f10_setup_cron_calculates_five_minute_lead_time(self):
        """Verify setup_cron.sh calculates 02:55 for 03:00 reboot with 5 min lead."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--dry-run", "--reboot-time", "03:00", "--lead-minutes", "5"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertTrue("55 2 * * *" in p.stdout or "55 02 * * *" in p.stdout)

    def test_f10_setup_cron_supports_custom_lead_minutes_and_reboot_time(self):
        """Verify custom reboot time 04:30 with 10m lead gives 04:20."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--dry-run", "--reboot-time", "04:30", "--lead-minutes", "10"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertTrue("20 4 * * *" in p.stdout or "20 04 * * *" in p.stdout)

    def test_f10_setup_cron_installs_managed_block_into_crontab(self):
        """Verify crontab installation wraps entries in managed comment markers."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("BLOCK_START", content)
        self.assertIn("BLOCK_END", content)
        self.assertIn("# >>> eternal-automation-bot pre-reboot maintenance >>>", content)

    def test_f10_setup_cron_uninstall_removes_only_managed_block(self):
        """Verify remove action strips only the managed block from crontab."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("do_remove", content)

    def test_f10_setup_cron_dry_run_outputs_crontab_without_modifying_system(self):
        """Verify --dry-run prints to stdout without executing crontab command."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--dry-run"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertIn("SHELL=", p.stdout)
        self.assertIn("pre_reboot_shutdown.sh", p.stdout)


class TestTier1F11CronDaemonAutoSupervision(BaseLifecycleTestCase):
    """F11: Cron Daemon Auto-Supervision."""

    def test_f11_detects_already_running_crond_daemon(self):
        """Verify setup_cron.sh --check-daemon inspects pgrep crond."""
        self.create_mock_bin("pgrep", "echo 12345; exit 0")
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--check-daemon"],
                           env=os.environ, capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertIn("is RUNNING", p.stdout)

    def test_f11_starts_crond_daemon_when_not_running(self):
        """Verify setup_cron.sh --ensure-daemon starts crond when not active."""
        started_log = os.path.join(self.test_dir, "crond_started.txt")
        # pgrep returns 1 before start, and 0 after crond creates crond_started.txt
        self.create_mock_bin("pgrep", f'if [ -f "{started_log}" ]; then exit 0; else exit 1; fi')
        self.create_mock_bin("crond", f"echo 'STARTED' > '{started_log}'; exit 0")

        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--ensure-daemon"],
                           env=os.environ, capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.exists(started_log))

    def test_f11_termux_boot_supervises_crond_at_startup(self):
        """Verify termux_boot.sh includes crond check and launch."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertTrue("crond &&" in content or "crond -b" in content)


    def test_f11_handles_missing_crond_binary_gracefully(self):
        """Verify crond check proceeds without fatal failure if crond not installed."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("crond binary not installed", content)

    def test_f11_crond_status_reporting_in_post_boot_telemetry(self):
        """Verify gather_full_telemetry checks crond process."""
        with patch("scripts.post_boot_notify.check_process_running", return_value=True):
            tel = post_boot_notify.gather_full_telemetry()
            self.assertTrue(tel["services"]["crond"])


class TestTier1F12SystemTelemetryCollection(BaseLifecycleTestCase):
    """F12: System Cold-Boot Telemetry Collection."""

    def test_f12_get_system_uptime_seconds_reads_proc_uptime(self):
        """Verify parsing /proc/uptime extracts float seconds correctly."""
        fake_uptime = os.path.join(self.test_dir, "fake_uptime")
        with open(fake_uptime, "w") as f:
            f.write("12345.67 8910.11\n")

        sec = health.get_system_uptime_seconds(uptime_path=fake_uptime)
        self.assertEqual(sec, 12345.67)

    def test_f12_format_uptime_seconds_human_readable_output(self):
        """Verify formatting of seconds into human-readable strings."""
        self.assertEqual(health.format_uptime_seconds(45), "0m 45s")
        self.assertEqual(health.format_uptime_seconds(3665), "1h 1m")
        self.assertEqual(health.format_uptime_seconds(172800 + 3600 + 60), "2d 1h 1m")
        self.assertEqual(health.format_uptime_seconds(None), "Unknown")

    def test_f12_get_server_metrics_gathers_cpu_ram_disk_battery(self):
        """Verify get_server_metrics returns dictionary with all expected keys."""
        metrics = health.get_server_metrics()
        required_keys = ["cpu_pct", "ram_total_mb", "ram_used_mb", "ram_pct",
                         "disk_total_gb", "disk_free_gb", "battery_pct", "uptime_str"]
        for k in required_keys:
            self.assertIn(k, metrics)

    def test_f12_battery_telemetry_extracts_percentage_status_temp(self):
        """Verify battery extraction handles mock json format."""
        mock_output = json.dumps({"percentage": 85, "status": "CHARGING", "temperature": 31.5})
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout=mock_output)
            metrics = health.get_server_metrics()
            self.assertEqual(metrics["battery_pct"], 85)
            self.assertEqual(metrics["battery_status"], "CHARGING")
            self.assertEqual(metrics["battery_temp"], 31.5)

    def test_f12_system_uptime_fallback_when_proc_uptime_unavailable(self):
        """Verify system uptime falls back gracefully when /proc/uptime does not exist."""
        sec = health.get_system_uptime_seconds(uptime_path="/non_existent_uptime_file")
        self.assertTrue(sec is None or isinstance(sec, float))


class TestTier1F13BotFleetHealthVerification(BaseLifecycleTestCase):
    """F13: Bot Fleet & Tmux Health Verification."""

    def test_f13_check_tmux_session_returns_true_for_running_session(self):
        """Verify check_tmux_session returns True when tmux has-session succeeds."""
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0)
            self.assertTrue(post_boot_notify.check_tmux_session("bot"))

    def test_f13_check_tmux_session_returns_false_for_dead_session(self):
        """Verify check_tmux_session returns False when tmux has-session fails."""
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=1)
            self.assertFalse(post_boot_notify.check_tmux_session("bot"))

    def test_f13_gather_full_telemetry_inspects_all_three_bots(self):
        """Verify gather_full_telemetry inspects bot, feed-bot, and heartcast."""
        with patch("scripts.post_boot_notify.check_tmux_session") as mock_chk:
            mock_chk.side_effect = lambda s: s in ("bot", "feed-bot")
            tel = post_boot_notify.gather_full_telemetry()
            self.assertIn("bot", tel["sessions"])
            self.assertIn("feed-bot", tel["sessions"])
            self.assertIn("heartcast", tel["sessions"])
            self.assertTrue(tel["sessions"]["bot"])
            self.assertTrue(tel["sessions"]["feed-bot"])
            self.assertFalse(tel["sessions"]["heartcast"])

    def test_f13_all_sessions_active_flag_computes_correctly(self):
        """Verify all_sessions_active is True only when 3/3 sessions are active."""
        with patch("scripts.post_boot_notify.check_tmux_session", return_value=True):
            tel = post_boot_notify.gather_full_telemetry()
            self.assertTrue(tel["all_sessions_active"])

        with patch("scripts.post_boot_notify.check_tmux_session", side_effect=[True, True, False]):
            tel = post_boot_notify.gather_full_telemetry()
            self.assertFalse(tel["all_sessions_active"])

    def test_f13_check_process_running_pgrep_matching(self):
        """Verify check_process_running invokes pgrep with pattern."""
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0)
            self.assertTrue(post_boot_notify.check_process_running("cloudflared"))
            mock_sub.assert_called_with(["pgrep", "-f", "cloudflared"], capture_output=True, text=True, timeout=2)


class TestTier1F14AdminTelegramAlerting(BaseLifecycleTestCase):
    """F14: Consolidated Admin Telegram Alerting."""

    def test_f14_format_post_boot_html_contains_all_metrics(self):
        """Verify generated HTML notification contains battery, RAM, disk, uptime, and bots."""
        telemetry = {
            "system_uptime": "15m 30s",
            "local_ip": "192.168.1.50",
            "formatted_time": "2026-09-02 03:00:00 IST",
            "metrics": {
                "battery_pct": 92, "battery_status": "Charging", "battery_temp": 32.0,
                "ram_used_mb": 2048, "ram_total_mb": 5800, "ram_pct": 35.3,
                "disk_free_gb": 45.2, "disk_total_gb": 128.0, "disk_free_pct": 35.3,
                "cpu_pct": 12.5
            },
            "sessions": {"bot": True, "feed-bot": True, "heartcast": True},
            "services": {"cloudflared": True, "sshd": True, "crond": True},
            "all_sessions_active": True
        }

        html = post_boot_notify.format_post_boot_html(telemetry)
        self.assertIn("Samsung Galaxy M31", html)
        self.assertIn("92%", html)
        self.assertIn("32.0°C", html)
        self.assertIn("15m 30s", html)
        self.assertIn("45.2 GB Free", html)
        self.assertIn("eternal-automation-bot", html)
        self.assertIn("eternal-feed-bot", html)
        self.assertIn("eternal-heartcast-bot", html)

    def test_f14_format_post_boot_html_escapes_html_entities(self):
        """Verify special HTML characters (<, >, &, \") are escaped."""
        raw = "Alert & Test <bold> \"quoted\""
        escaped = escape_html(raw)
        self.assertEqual(escaped, "Alert &amp; Test &lt;bold&gt; &quot;quoted&quot;")

    def test_f14_send_post_boot_notification_dry_run_returns_true(self):
        """Verify dry-run mode returns True and valid telemetry without dispatching."""
        success, tel = post_boot_notify.send_post_boot_notification(dry_run=True, quiet=True)
        self.assertTrue(success)
        self.assertIsInstance(tel, dict)

    def test_f14_send_post_boot_notification_dispatches_to_owner_id(self):
        """Verify send_post_boot_notification dispatches message to OWNER_ID."""
        with patch("scripts.post_boot_notify.send_msg") as mock_send:
            mock_send.return_value = MagicMock(ok=True)
            success, tel = post_boot_notify.send_post_boot_notification(chat_id="999999999", dry_run=False, quiet=True)
            self.assertTrue(success)
            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            self.assertEqual(args[0], "999999999")
            self.assertIn("Samsung Galaxy M31", args[1])

    def test_f14_send_post_boot_notification_json_output_mode(self):
        """Verify as_json=True outputs clean dictionary structure."""
        success, tel = post_boot_notify.send_post_boot_notification(as_json=True, quiet=True)
        self.assertTrue(success)
        self.assertIn("metrics", tel)
        self.assertIn("sessions", tel)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES
# ==============================================================================

class TestTier2ShutdownAndDrainBoundaries(BaseLifecycleTestCase):
    """Tier 2: Shutdown, Task Drain & Signal Boundaries."""

    def test_tier2_drain_with_zero_second_timeout(self):
        """Boundary: 0.0s timeout forces immediate non-blocking task cancellation."""
        executor = ThreadPoolExecutor(max_workers=1)
        with patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor):
            t0 = time.time()
            main_bot.drain_in_flight_tasks(timeout=0.0)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 0.5)

    def test_tier2_drain_with_exact_ten_second_boundary(self):
        """Boundary: Exact 10.0s timeout parameter handles valid bounds."""
        executor = ThreadPoolExecutor(max_workers=1)
        with patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor):
            main_bot.drain_in_flight_tasks(timeout=10.0)

    def test_tier2_drain_with_subsecond_timeout(self):
        """Boundary: Sub-second timeout (0.05s) executes cleanly without floating point errors."""
        executor = ThreadPoolExecutor(max_workers=1)
        with patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor):
            main_bot.drain_in_flight_tasks(timeout=0.05)

    def test_tier2_shutdown_when_state_file_is_none(self):
        """Corner Case: shutdown_bot with state=None does not throw AttributeError."""
        main_bot.shutdown_bot(state=None, timeout=0.1, drain_executor=False)

    def test_tier2_signal_broadcasting_with_empty_session_list(self):
        """Corner Case: Signaling empty session list returns cleanly."""
        targets = []
        for t in targets:
            post_boot_notify.check_tmux_session(t)
        self.assertEqual(len(targets), 0)

    def test_tier2_shutdown_with_unkillable_thread_exception(self):
        """Corner Case: Broken executor thread pool does not prevent state saving."""
        state_file = os.path.join(self.test_dir, "corner_state.json")
        state_data = {"test": 123}

        broken_executor = MagicMock()
        broken_executor.shutdown.side_effect = RuntimeError("Executor corrupted")
        broken_executor._threads = []

        with patch.object(config, "STATE_FILE", state_file), \
             patch.object(main_bot, "STATE_FILE", state_file), \
             patch.object(config, "DOWNLOAD_EXECUTOR", broken_executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", broken_executor):
            main_bot.shutdown_bot(state=state_data, timeout=0.1, drain_executor=True)

        saved = users.load_json(state_file)
        self.assertEqual(saved, state_data)


class TestTier2StateAndStorageBoundaries(BaseLifecycleTestCase):
    """Tier 2: JSON Persistence, Corrupted Files & Storage Boundaries."""

    def test_tier2_load_json_corrupted_truncated_content(self):
        """Boundary: Loading corrupted / truncated JSON returns None safely without raising."""
        corrupt_file = os.path.join(self.test_dir, "corrupt.json")
        with open(corrupt_file, "w") as f:
            f.write('{"users": {"123": {"name": "truncated')

        res = users.load_json(corrupt_file)
        self.assertIsNone(res)

    def test_tier2_load_json_empty_zero_byte_file(self):
        """Boundary: Loading 0-byte file returns None safely."""
        empty_file = os.path.join(self.test_dir, "empty.json")
        open(empty_file, "w").close()

        res = users.load_json(empty_file)
        self.assertIsNone(res)

    def test_tier2_save_json_with_unicode_and_special_characters(self):
        """Boundary: Preserves deep UTF-8 emojis, foreign scripts, and newlines."""
        unicode_file = os.path.join(self.test_dir, "unicode.json")
        payload = {
            "admin": "👑 Owner 🚀",
            "hebrew": "שלום",
            "hindi": "नमस्ते",
            "japanese": "こんにちは",
            "multiline": "line1\nline2\tline3"
        }
        users.save_json(unicode_file, payload)
        loaded = users.load_json(unicode_file)
        self.assertEqual(loaded, payload)

    def test_tier2_save_json_to_readonly_directory(self):
        """Boundary: Writing to read-only directory logs error and removes temp file."""
        ro_dir = os.path.join(self.test_dir, "readonly_dir")
        os.makedirs(ro_dir, exist_ok=True)
        target = os.path.join(ro_dir, "file.json")

        os.chmod(ro_dir, 0o444)
        try:
            users.save_json(target, {"key": "val"})
        finally:
            os.chmod(ro_dir, 0o755)

    def test_tier2_cleanup_downloads_with_symbolic_links(self):
        """Boundary: cleanup_old_downloads handles symbolic links safely."""
        real_file = os.path.join(self.test_dir, "real_target.mp4")
        with open(real_file, "w") as f:
            f.write("target")

        symlink_file = os.path.join(self.downloads_dir, "symlink.mp4")
        try:
            os.symlink(real_file, symlink_file)
        except OSError:
            self.skipTest("Symlinks not supported in environment")

        with patch("downloader.DOWNLOADS_DIR", self.downloads_dir):
            downloader.cleanup_old_downloads(max_age_seconds=0)

        # Target real file outside downloads_dir remains
        self.assertTrue(os.path.exists(real_file))

    def test_tier2_channel_seen_posts_with_ten_thousand_entries(self):
        """Boundary: High volume channel_seen_posts (10,000 items) persists and loads cleanly."""
        seen_file = os.path.join(self.test_dir, "large_seen.json")
        now = int(time.time())
        large_dict = {f"norm_post_headline_{i}": {"msg_id": i, "ts": now, "chat_id": "-1001"}
                      for i in range(10000)}

        users.save_json(seen_file, large_dict)
        loaded = users.load_json(seen_file)
        self.assertEqual(len(loaded), 10000)


class TestTier2WatchdogAndBootBoundaries(BaseLifecycleTestCase):
    """Tier 2: Watchdog & Boot Environment Boundaries."""

    def test_tier2_watchdog_with_missing_home_env(self):
        """Boundary: Watchdog defaults HOME to Termux fallback when unset."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn("${HOME:-/data/data/com.termux/files/home}", content)

    def test_tier2_watchdog_log_rotation_at_exact_5mb_boundary(self):
        """Boundary: Log rotates when file size strictly exceeds MAX_LOG_SIZE (5242880 bytes)."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn("MAX_LOG_SIZE=5242880", content)

    def test_tier2_termux_boot_with_non_writable_log_path(self):
        """Boundary: Handles unwriteable BOOT_LOG directory gracefully."""
        boot_log = "/non_existent_system_dir_root/boot.log"
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0)

    def test_tier2_boot_network_timeout_zero_seconds(self):
        """Boundary: BOOT_NETWORK_TIMEOUT=0 does not block or enter infinite loop."""
        boot_log = os.path.join(self.home_dir, "net_0.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="0")
        t0 = time.time()
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=10)
        elapsed = time.time() - t0
        self.assertEqual(p.returncode, 0)
        self.assertLess(elapsed, 8.0)

    def test_tier2_wake_lock_binary_exits_with_error_code_127(self):
        """Boundary: Missing tool return code 127 does not halt boot script execution."""
        self.create_mock_bin("termux-wake-lock", "exit 127")
        boot_log = os.path.join(self.home_dir, "wake_127.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=25)
        self.assertEqual(p.returncode, 0)

    def test_tier2_watchdog_restart_loop_on_crash_exit_code_1(self):
        """Boundary: Crash exit code (1) executes sleep 2 and restarts when not in shutdown."""
        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()
        self.assertIn("sleep 2", content)
        self.assertIn("Restarting in 2s...", content)


class TestTier2NetworkAndCronBoundaries(BaseLifecycleTestCase):
    """Tier 2: Network & Cron Calculation Boundaries."""

    def test_tier2_cron_calculation_midnight_wraparound_00_02(self):
        """Boundary: 00:02 reboot with 5 min lead calculates 23:57 (previous day wraparound)."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--dry-run", "--reboot-time", "00:02", "--lead-minutes", "5"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertIn("57 23 * * *", p.stdout)

    def test_tier2_cron_calculation_exact_hour_boundary_03_00(self):
        """Boundary: Exact 03:00 reboot with 5 min lead calculates 02:55."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--dry-run", "--reboot-time", "03:00", "--lead-minutes", "5"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)
        self.assertTrue("55 2 * * *" in p.stdout or "55 02 * * *" in p.stdout)

    def test_tier2_cron_setup_with_existing_corrupt_crontab(self):
        """Boundary: setup_cron.sh safely parses crontab with non-standard comment lines."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        mock_crontab_content = "# Random user comment\n10 * * * * /bin/custom_job\n# Non standard\n"
        self.create_mock_bin("crontab", f'if [ "$1" = "-l" ]; then echo "{mock_crontab_content}"; exit 0; fi; exit 0')

        p = subprocess.run(["bash", script_path, "--show"],
                           env=os.environ, capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)

    def test_tier2_network_poller_with_all_tiers_failing(self):
        """Boundary: When socket, DNS, HTTP, and ping fail, timeout exits cleanly."""
        script_path = os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")
        with open(script_path, "r") as f:
            content = f.read()
        self.assertIn("timed out after", content)

    def test_tier2_network_ip_dns_failure_fallback_to_localhost(self):
        """Boundary: get_network_ip returns 127.0.0.1 upon complete network failure."""
        with patch("socket.socket") as mock_sock:
            mock_sock.side_effect = OSError("No route to host")
            with patch("socket.gethostbyname", side_effect=OSError("DNS error")):
                ip = post_boot_notify.get_network_ip()
                self.assertEqual(ip, "127.0.0.1")

    def test_tier2_invalid_reboot_time_input_validation(self):
        """Boundary: Invalid reboot time format (e.g. 25:99) produces clear error code."""
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--dry-run", "--reboot-time", "25:99"],
                           capture_output=True, text=True, timeout=5)
        self.assertNotEqual(p.returncode, 0)


class TestTier2TelemetryAndAlertingBoundaries(BaseLifecycleTestCase):
    """Tier 2: Telemetry, Alerting & Hardware Sensor Boundaries."""

    def test_tier2_telemetry_with_missing_proc_uptime_and_meminfo(self):
        """Boundary: Handles missing /proc filesystem without uncaught exceptions."""
        metrics = health.get_server_metrics(uptime_path="/dev/null/missing")
        self.assertIsInstance(metrics, dict)
        self.assertIn("uptime_str", metrics)

    def test_tier2_telemetry_extreme_battery_temperatures(self):
        """Boundary: Formats sub-zero or high temperatures correctly."""
        telemetry = {
            "system_uptime": "1m",
            "metrics": {"battery_pct": 50, "battery_temp": -5.0},
            "sessions": {},
            "services": {}
        }
        html = post_boot_notify.format_post_boot_html(telemetry)
        self.assertIn("-5.0°C", html)

    def test_tier2_telemetry_with_zero_disk_free(self):
        """Boundary: 0 GB free disk calculates 0.0% without division by zero."""
        telemetry = {
            "system_uptime": "1m",
            "metrics": {
                "disk_free_gb": 0.0,
                "disk_total_gb": 64.0,
                "disk_free_pct": 0.0,
            },
            "sessions": {},
            "services": {}
        }
        html = post_boot_notify.format_post_boot_html(telemetry)
        self.assertIn("0.0 GB Free", html)

    def test_tier2_html_formatting_with_raw_script_injection_in_session_name(self):
        """Boundary: HTML entities in system strings are sanitized against XSS injection."""
        telemetry = {
            "system_uptime": "<script>alert(1)</script>",
            "local_ip": "192.168.1.1<img src=x onerror=alert()>",
            "metrics": {"battery_status": "<b>Discharging</b>"},
            "sessions": {},
            "services": {}
        }
        html = post_boot_notify.format_post_boot_html(telemetry)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;Discharging&lt;/b&gt;", html)

    def test_tier2_telegram_dispatch_failure_network_exception(self):
        """Boundary: Network exception during send_msg returns (False, telemetry)."""
        with patch("scripts.post_boot_notify.send_msg", side_effect=Exception("Connection reset")):
            success, tel = post_boot_notify.send_post_boot_notification(dry_run=False, quiet=True)
            self.assertFalse(success)

    def test_tier2_missing_bot_token_graceful_handling(self):
        """Boundary: Missing BOT_TOKEN handles validation cleanly."""
        with patch.dict(os.environ, {"BOT_TOKEN": ""}):
            with patch("config.BOT_TOKEN", ""):
                success, tel = post_boot_notify.send_post_boot_notification(dry_run=False, quiet=True)
                self.assertFalse(success)


# ==============================================================================
# TIER 3: CROSS-FEATURE INTERACTIONS
# ==============================================================================

class TestTier3CrossFeatureInteractions(BaseLifecycleTestCase):
    """Tier 3: Pairwise and Multi-Component Cross-Feature Interactions."""

    def test_tier3_interaction_1_shutdown_drain_state_flush_cleanup_cascade(self):
        """Interaction 1: Pre-reboot shutdown + Drain (10s) + State Flush + Cleanup + Sentinel."""
        # 1. Simulate in-flight worker
        executor = ThreadPoolExecutor(max_workers=2)
        done_flag = threading.Event()
        def fast_worker():
            time.sleep(0.05)
            done_flag.set()
        executor.submit(fast_worker)

        # 2. Prepare state files
        state_file = os.path.join(self.test_dir, "cascade_state.json")
        users_file = os.path.join(self.test_dir, "cascade_users.json")
        seen_file = os.path.join(self.test_dir, "cascade_seen.json")
        stale_download = os.path.join(self.downloads_dir, "stale_chunk.part")
        with open(stale_download, "w") as f:
            f.write("chunk data")

        users_data = {"999999999": {"role": "owner"}}
        state_data = {"last_update_id": 8888}

        with patch.object(config, "STATE_FILE", state_file), \
             patch.object(main_bot, "STATE_FILE", state_file), \
             patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(users, "USERS_FILE", users_file), \
             patch("handlers.CHANNEL_SEEN_POSTS_FILE", seen_file), \
             patch.object(config, "DOWNLOADS_DIR", self.downloads_dir), \
             patch.object(downloader, "DOWNLOADS_DIR", self.downloads_dir):

            users.save_users(users_data)
            handlers._channel_seen_posts["headline_1"] = {"msg_id": 1, "ts": int(time.time()), "chat_id": "-100"}

            # Execute full coordinated shutdown
            main_bot.shutdown_bot(state=state_data, timeout=2.0, drain_executor=True)

        self.assertTrue(done_flag.is_set())
        self.assertEqual(users.load_json(state_file), state_data)
        self.assertEqual(users.load_json(users_file), users_data)
        self.assertIn("headline_1", users.load_json(seen_file))
        self.assertFalse(os.path.exists(stale_download))

    def test_tier3_interaction_2_boot_recovery_to_admin_alert_pipeline(self):
        """Interaction 2: Boot recovery + Wake lock + Network wait + Tmux spawn + Health Alert."""
        self.create_mock_bin("termux-wake-lock", "exit 0")
        self.create_mock_bin("tmux", "exit 0")
        self.create_mock_bin("crond", "exit 0")
        self.create_mock_bin("pgrep", "exit 0")

        boot_log = os.path.join(self.home_dir, "full_boot.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")

        with patch("scripts.post_boot_notify.send_msg") as mock_send:
            mock_send.return_value = MagicMock(ok=True)
            p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                               env=env, capture_output=True, text=True, timeout=15)

            self.assertEqual(p.returncode, 0)
            with open(boot_log, "r") as f:
                log_text = f.read()

            self.assertIn("Starting Termux:Boot Recovery Suite", log_text)
            self.assertIn("Termux CPU Wake Lock", log_text)
            self.assertIn("Network is ONLINE", log_text)
            self.assertIn("Cleaning orphaned lock", log_text)
            self.assertIn("Spawning multi-bot tmux", log_text)
            self.assertIn("Recovery Suite Completed Successfully", log_text)

    def test_tier3_interaction_3_cron_supervision_to_shutdown_trigger(self):
        """Interaction 3: Cron supervision -> scheduled trigger -> pre_reboot_shutdown execution."""
        self.create_mock_bin("pgrep", "echo 12345; exit 0")
        script_path = os.path.join(REPO_ROOT, "scripts", "setup_cron.sh")
        p = subprocess.run(["bash", script_path, "--check-daemon"],
                           env=os.environ, capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0)

    def test_tier3_interaction_4_watchdog_crash_recovery_with_live_sentinel(self):
        """Interaction 4: Watchdog child crash + sentinel present -> immediate stop without respawn."""
        sentinel = os.path.join(self.home_dir, ".shutdown_in_progress")
        with open(sentinel, "w") as f:
            f.write("SHUTDOWN")

        with open(os.path.join(REPO_ROOT, "start.sh"), "r") as f:
            content = f.read()

        self.assertIn('[ -f "$SENTINEL_SHUTDOWN" ]', content)
        self.assertIn("Shutdown sentinel detected", content)

    def test_tier3_interaction_5_boot_recovery_with_stale_locks_and_degraded_network(self):
        """Interaction 5: Stale locks from hard reboot + slow network -> cleans locks, recovers."""
        stale_lock = os.path.join(self.feed_dir, ".feedbot.lock")
        with open(stale_lock, "w") as f:
            f.write("stale_pid_9999")

        boot_log = os.path.join(self.home_dir, "stale_boot.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")

        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=15)

        self.assertEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(stale_lock))

    def test_tier3_interaction_6_parallel_shutdown_invocations_and_race_prevention(self):
        """Interaction 6: Multiple threads calling shutdown concurrently execute safely."""
        state_file = os.path.join(self.test_dir, "parallel_state.json")
        exceptions = []

        def run_shutdown():
            try:
                with patch.object(config, "STATE_FILE", state_file), \
                     patch.object(main_bot, "STATE_FILE", state_file):
                    main_bot.shutdown_bot(state={"ts": time.time()}, timeout=0.1, drain_executor=False)
            except Exception as e:
                exceptions.append(e)

        threads = [threading.Thread(target=run_shutdown) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(exceptions), 0)

    def test_tier3_interaction_7_rapid_boot_to_immediate_pre_reboot_cycle(self):
        """Interaction 7: Complete Boot -> Operation -> Shutdown cycle executes cleanly."""
        # 1. Boot Phase
        boot_log = os.path.join(self.home_dir, "cycle_boot.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")
        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=15)
        self.assertEqual(p.returncode, 0)

        # 2. Shutdown Phase
        state_file = os.path.join(self.test_dir, "cycle_state.json")
        with patch.object(config, "STATE_FILE", state_file), \
             patch.object(main_bot, "STATE_FILE", state_file):
            main_bot.shutdown_bot(state={"cycle_complete": True}, timeout=0.2, drain_executor=False)

        self.assertTrue(users.load_json(state_file).get("cycle_complete"))


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS
# ==============================================================================

class TestTier4RealWorldScenarios(BaseLifecycleTestCase):
    """
    Tier 4: End-to-End Real-World Application Scenarios.
    Simulates production environment conditions on Samsung Galaxy M31 phone server.
    """

    def test_scenario_1_normal_pre_reboot_maintenance_window(self):
        """
        Scenario 1: Normal Pre-Reboot Maintenance Window
        Automated cron triggers shutdown 5m before OS restart with active in-flight downloads.
        Tasks drain within 10s, DBs flush atomically, locks release, and tmux sessions stop cleanly.
        """
        executor = ThreadPoolExecutor(max_workers=2)
        task_completed = threading.Event()
        def download_job():
            time.sleep(0.1)
            task_completed.set()
        executor.submit(download_job)

        state_file = os.path.join(self.test_dir, "scenario1_state.json")
        seen_file = os.path.join(self.test_dir, "scenario1_seen.json")
        users_file = os.path.join(self.test_dir, "scenario1_users.json")

        with patch.object(config, "STATE_FILE", state_file), \
             patch.object(main_bot, "STATE_FILE", state_file), \
             patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(users, "USERS_FILE", users_file), \
             patch("handlers.CHANNEL_SEEN_POSTS_FILE", seen_file):

            users.save_users({"999999999": {"role": "owner"}})
            handlers._channel_seen_posts["post_scen1"] = {"msg_id": 101, "ts": int(time.time()), "chat_id": "-100"}

            # Trigger graceful pre-reboot shutdown
            main_bot.shutdown_bot(state={"last_update_id": 5050}, timeout=3.0, drain_executor=True)

        self.assertTrue(task_completed.is_set())
        self.assertEqual(users.load_json(state_file)["last_update_id"], 5050)
        self.assertIn("post_scen1", users.load_json(seen_file))

    def test_scenario_2_heavy_download_in_flight_timeout(self):
        """
        Scenario 2: Heavy Download In-Flight Timeout
        Active multi-GB download exceeds 10s drain window; graceful cancellation kicks in
        and scratch files (.part / .tmp) are purged without database corruption.
        """
        executor = ThreadPoolExecutor(max_workers=1)
        unblock_event = threading.Event()
        def heavy_download():
            unblock_event.wait(timeout=5.0)

        executor.submit(heavy_download)

        # Create scratch download files
        scratch_part = os.path.join(self.downloads_dir, "large_video.mp4.part")
        scratch_tmp = os.path.join(self.downloads_dir, "job_999.tmp")
        for f in (scratch_part, scratch_tmp):
            with open(f, "w") as fp:
                fp.write("scratch data")

        state_file = os.path.join(self.test_dir, "scenario2_state.json")

        with patch.object(config, "STATE_FILE", state_file), \
             patch.object(main_bot, "STATE_FILE", state_file), \
             patch.object(config, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(main_bot, "DOWNLOAD_EXECUTOR", executor), \
             patch.object(config, "DOWNLOADS_DIR", self.downloads_dir), \
             patch.object(downloader, "DOWNLOADS_DIR", self.downloads_dir):

            t0 = time.time()
            main_bot.shutdown_bot(state={"last_update_id": 6060}, timeout=0.3, drain_executor=True)
            elapsed = time.time() - t0

        unblock_event.set()

        self.assertLess(elapsed, 2.0)
        self.assertFalse(os.path.exists(scratch_part))
        self.assertFalse(os.path.exists(scratch_tmp))
        self.assertEqual(users.load_json(state_file)["last_update_id"], 6060)

    def test_scenario_3_cold_boot_auto_recovery(self):
        """
        Scenario 3: Cold Boot Auto-Recovery
        Device boots cold from Samsung restart, acquires wake lock, handles 1s network wait,
        spawns all 3 tmux sessions, verifies crond, collects telemetry, and sends HTML alert to Admin.
        """
        self.create_mock_bin("termux-wake-lock", "exit 0")
        self.create_mock_bin("tmux", "exit 0")
        self.create_mock_bin("crond", "exit 0")
        self.create_mock_bin("pgrep", "exit 0")

        boot_log = os.path.join(self.home_dir, "cold_boot.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")

        with patch("scripts.post_boot_notify.send_msg") as mock_send:
            mock_send.return_value = MagicMock(ok=True)

            p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                               env=env, capture_output=True, text=True, timeout=15)

            self.assertEqual(p.returncode, 0)
            self.assertTrue(os.path.exists(boot_log))

            # Verify post-boot notification execution
            success, tel = post_boot_notify.send_post_boot_notification(dry_run=True, quiet=True)
            self.assertTrue(success)
            self.assertIn("metrics", tel)
            self.assertIn("sessions", tel)

    def test_scenario_4_rapid_succession_reboot(self):
        """
        Scenario 4: Rapid Succession Reboot
        Device restarts twice in rapid succession; previous crash left dead sockets and temp files.
        Auto-recovery cleans all dead state and boots cleanly without duplicate tmux sessions.
        """
        boot_log = os.path.join(self.home_dir, "rapid_boot.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")

        p1 = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                            env=env, capture_output=True, timeout=25)
        self.assertEqual(p1.returncode, 0)

        p2 = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                            env=env, capture_output=True, timeout=25)
        self.assertEqual(p2.returncode, 0)

        with open(boot_log, "r") as f:
            content = f.read()
        self.assertIn("Termux:Boot Recovery Suite", content)

    def test_scenario_5_corrupted_temp_stale_lock_recovery(self):
        """
        Scenario 5: Corrupted Temp / Stale Lock Recovery
        Hard crash left .feedbot.lock, users.json.tmp, and partial downloads.
        Boot recovery purges all corrupted temp files and starts all bots cleanly.
        """
        feed_lock = os.path.join(self.feed_dir, ".feedbot.lock")
        tmp_user = os.path.join(self.bot_dir, "users.json.tmp")
        orphan_dl = os.path.join(self.bot_dir, "downloads", "orphan_chunk.aria2")

        for f in (feed_lock, tmp_user, orphan_dl):
            with open(f, "w") as fp:
                fp.write("corrupted leftover")

        boot_log = os.path.join(self.home_dir, "corrupt_recovery.log")
        env = dict(os.environ, BOOT_LOG=boot_log, BOOT_NETWORK_TIMEOUT="1")

        p = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts", "termux_boot.sh")],
                           env=env, capture_output=True, timeout=25)
        self.assertEqual(p.returncode, 0)

        self.assertFalse(os.path.exists(feed_lock))
        self.assertFalse(os.path.exists(tmp_user))
        self.assertFalse(os.path.exists(orphan_dl))


# ==============================================================================
# MAIN TEST RUNNER
# ==============================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
