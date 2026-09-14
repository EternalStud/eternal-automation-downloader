import os
import sys
import time
import json
import sqlite3
import shutil
import tempfile
import signal
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import config
import main
import handlers
import users

class TestShutdownSync(unittest.TestCase):
    """
    Comprehensive Unit & Integration Test Suite for:
    1. Pre-reboot graceful shutdown orchestrator (scripts/pre_reboot_shutdown.sh)
    2. Atomic state synchronization & channel dedup persistence (handlers.py -> users.py)
    3. In-flight task drain & bot lifecycle management (main.py)
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_shutdown_sync_")
        self.script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "pre_reboot_shutdown.sh"
        )
        # Ensure a clean executor instance for each test
        config.DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=8)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)
        # Re-initialize clean executor for subsequent tests
        config.DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=8)

    # ----------------------------------------------------------------------
    # 1. Atomic Persistence & Channel Seen Posts Tests (handlers.py)
    # ----------------------------------------------------------------------

    def test_save_channel_seen_posts_atomic_write(self):
        """Verify save_channel_seen_posts uses atomic save_json with thread safety."""
        test_file = os.path.join(self.test_dir, "test_channel_seen.json")
        orig_file = handlers.CHANNEL_SEEN_POSTS_FILE
        try:
            handlers.CHANNEL_SEEN_POSTS_FILE = test_file
            handlers._channel_seen_posts = {
                "headline 1": {"msg_id": 100, "ts": time.time(), "chat_id": "-100123"}
            }
            handlers.save_channel_seen_posts()

            self.assertTrue(os.path.exists(test_file))
            with open(test_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
            self.assertIn("headline 1", saved_data)
            self.assertEqual(saved_data["headline 1"]["msg_id"], 100)
        finally:
            handlers.CHANNEL_SEEN_POSTS_FILE = orig_file

    def test_load_channel_seen_posts_handles_corrupt_file(self):
        """Verify load_channel_seen_posts safely recovers from broken JSON."""
        test_file = os.path.join(self.test_dir, "corrupt_channel_seen.json")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("{invalid json syntax !!!")

        orig_file = handlers.CHANNEL_SEEN_POSTS_FILE
        orig_loaded = handlers._channel_seen_loaded
        try:
            handlers.CHANNEL_SEEN_POSTS_FILE = test_file
            handlers._channel_seen_loaded = False
            posts = handlers.load_channel_seen_posts()
            self.assertIsInstance(posts, dict)
            self.assertEqual(len(posts), 0)
        finally:
            handlers.CHANNEL_SEEN_POSTS_FILE = orig_file
            handlers._channel_seen_loaded = orig_loaded

    def test_channel_seen_posts_lifecycle_and_pruning(self):
        """Verify check_and_record_channel_post records, deduplicates, and saves."""
        test_file = os.path.join(self.test_dir, "lifecycle_channel_seen.json")
        orig_file = handlers.CHANNEL_SEEN_POSTS_FILE
        orig_loaded = handlers._channel_seen_loaded
        try:
            handlers.CHANNEL_SEEN_POSTS_FILE = test_file
            handlers._channel_seen_loaded = False
            handlers._channel_seen_posts = {}

            post_text = "Breaking News: Major contract awarded for 500 MW renewable power plant in Gujarat"
            is_dup1 = handlers.check_and_record_channel_post(chat_id="-1001", msg_id=1, text=post_text)
            self.assertFalse(is_dup1)

            # Duplicate detection with different msg_id
            is_dup2 = handlers.check_and_record_channel_post(chat_id="-1001", msg_id=2, text=post_text)
            self.assertTrue(is_dup2)

            # Verify saved to disk atomically
            self.assertTrue(os.path.exists(test_file))
            loaded = users.load_json(test_file)
            self.assertIsNotNone(loaded)
            self.assertGreater(len(loaded), 0)
        finally:
            handlers.CHANNEL_SEEN_POSTS_FILE = orig_file
            handlers._channel_seen_loaded = orig_loaded

    # ----------------------------------------------------------------------
    # 2. In-Flight Task Drain & Shutdown Bot Lifecycle (main.py)
    # ----------------------------------------------------------------------

    def test_handle_exit_signal_sets_running_false(self):
        """Verify signal handler transitions RUNNING state to False."""
        main.RUNNING = True
        main.handle_exit_signal(signal.SIGTERM, None)
        self.assertFalse(main.RUNNING)

    def test_drain_in_flight_tasks_natural_completion(self):
        """Verify drain_in_flight_tasks waits for fast tasks to finish."""
        task_finished = threading.Event()

        def fast_worker():
            time.sleep(0.05)
            task_finished.set()

        # Submit task to executor
        future = config.DOWNLOAD_EXECUTOR.submit(fast_worker)
        main.drain_in_flight_tasks(timeout=2.0)
        self.assertTrue(task_finished.is_set())

    def test_drain_in_flight_tasks_timeout_boundary(self):
        """Verify drain_in_flight_tasks respects timeout limit on long tasks."""
        never_finished = threading.Event()

        def slow_worker():
            time.sleep(1.0)
            never_finished.set()

        config.DOWNLOAD_EXECUTOR.submit(slow_worker)
        start = time.time()
        main.drain_in_flight_tasks(timeout=0.2)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0)

    def test_shutdown_bot_full_sequence(self):
        """Verify shutdown_bot flushes state, channel posts, and cleans downloads."""
        test_state_file = os.path.join(self.test_dir, "test_state.json")
        test_seen_file = os.path.join(self.test_dir, "test_seen.json")
        test_downloads = os.path.join(self.test_dir, "downloads")
        os.makedirs(test_downloads, exist_ok=True)

        # Create a dummy download file
        dummy_file = os.path.join(test_downloads, "test_orphan.mp4")
        with open(dummy_file, "w") as f:
            f.write("dummy media data")

        orig_state_file = config.STATE_FILE
        orig_seen_file = handlers.CHANNEL_SEEN_POSTS_FILE
        orig_downloads_dir = config.DOWNLOADS_DIR
        try:
            config.STATE_FILE = test_state_file
            handlers.CHANNEL_SEEN_POSTS_FILE = test_seen_file
            config.DOWNLOADS_DIR = test_downloads

            main.RUNNING = True
            mock_state = {"last_update_id": 999988}

            main.shutdown_bot(state=mock_state, timeout=0.5, drain_executor=False)

            self.assertFalse(main.RUNNING)
            # Verify state.json was written
            self.assertTrue(os.path.exists(test_state_file))
            saved_state = users.load_json(test_state_file)
            self.assertEqual(saved_state.get("last_update_id"), 999988)

            # Verify channel_seen_posts.json was flushed
            self.assertTrue(os.path.exists(test_seen_file))
        finally:
            config.STATE_FILE = orig_state_file
            handlers.CHANNEL_SEEN_POSTS_FILE = orig_seen_file
            config.DOWNLOADS_DIR = orig_downloads_dir

    # ----------------------------------------------------------------------
    # 3. Pre-Reboot Shutdown Script Integration Tests (scripts/pre_reboot_shutdown.sh)
    # ----------------------------------------------------------------------

    def test_script_help_flag(self):
        """Verify pre_reboot_shutdown.sh -h/--help exits with code 0."""
        res = subprocess.run(["bash", self.script_path, "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage:", res.stdout)
        self.assertIn("--timeout", res.stdout)

    def test_script_dry_run_mode(self):
        """Verify --dry-run produces simulation logs and exits code 0 without modifying files."""
        sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
        env = dict(os.environ, SHUTDOWN_SENTINEL=sentinel)
        res = subprocess.run(["bash", self.script_path, "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("[Dry Run]", res.stdout)
        self.assertFalse(os.path.exists(sentinel))

    def test_script_clean_execution_and_sentinel_cleanup(self):
        """Verify standard run creates and cleans sentinel, validates DBs, and exits code 0."""
        sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
        log_file = os.path.join(self.test_dir, "shutdown.log")
        env = dict(os.environ, SHUTDOWN_SENTINEL=sentinel, SHUTDOWN_LOG=log_file)

        res = subprocess.run(["bash", self.script_path, "--timeout", "1"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("PRE-REBOOT SHUTDOWN COMPLETED SUCCESSFULLY", res.stdout)
        # Sentinel must be cleanly removed upon exit
        self.assertFalse(os.path.exists(sentinel))
        # Log file should have been written
        self.assertTrue(os.path.exists(log_file))

    def test_script_purges_temporary_and_lock_files(self):
        """Verify shutdown script cleans *.tmp, *.lock, and downloads/* in bot directories."""
        mock_bot_dir = os.path.join(self.test_dir, "mock-automation-bot")
        downloads_dir = os.path.join(mock_bot_dir, "downloads")
        os.makedirs(downloads_dir, exist_ok=True)

        # Create transient artifacts
        tmp_file = os.path.join(mock_bot_dir, "users.json.tmp")
        lock_file = os.path.join(mock_bot_dir, ".feedbot.lock")
        orphan_dl = os.path.join(downloads_dir, "job_12345.mp4")

        with open(tmp_file, "w") as f: f.write("{}")
        with open(lock_file, "w") as f: f.write("1234")
        with open(orphan_dl, "w") as f: f.write("data")

        # Create valid users.json so validation passes
        with open(os.path.join(mock_bot_dir, "users.json"), "w") as f:
            json.dump({"999999999": {"role": "owner"}}, f)

        sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
        env = dict(
            os.environ,
            SHUTDOWN_SENTINEL=sentinel,
            DEFAULT_AUTO_DIR=mock_bot_dir,
            HOME=self.test_dir
        )

        res = subprocess.run(["bash", self.script_path, "--timeout", "1"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        # Assert transient files were purged
        self.assertFalse(os.path.exists(tmp_file), "Temporary .tmp file was not purged")
        self.assertFalse(os.path.exists(lock_file), "Lock file was not purged")
        self.assertFalse(os.path.exists(orphan_dl), "Orphan download file was not purged")

    def test_script_detects_corrupted_json_database(self):
        """Verify shutdown script flags corrupt JSON databases with exit code 1."""
        mock_bot_dir = os.path.join(self.test_dir, "corrupt-bot")
        os.makedirs(mock_bot_dir, exist_ok=True)

        # Write corrupt users.json
        corrupt_db = os.path.join(mock_bot_dir, "users.json")
        with open(corrupt_db, "w") as f:
            f.write('{"unclosed": "json')

        sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
        env = dict(
            os.environ,
            SHUTDOWN_SENTINEL=sentinel,
            HOME=self.test_dir,
            DEFAULT_AUTO_DIR=mock_bot_dir
        )

        res = subprocess.run(["bash", self.script_path, "--timeout", "1"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("Corrupt JSON DB", res.stdout)
        self.assertIn("PRE-REBOOT SHUTDOWN COMPLETED WITH ISSUES", res.stdout)

    def test_script_signals_and_drains_running_bot_process(self):
        """Verify shutdown script sends SIGTERM to running mock bot process and waits for clean exit."""
        mock_script_path = os.path.join(self.test_dir, "eternal-automation-bot-main.py")
        with open(mock_script_path, "w") as f:
            f.write("""
import signal, time, sys
def on_sig(sig, frame):
    sys.exit(0)
signal.signal(signal.SIGTERM, on_sig)
signal.signal(signal.SIGINT, on_sig)
while True:
    time.sleep(0.1)
""")

        proc = subprocess.Popen([sys.executable, mock_script_path])
        try:
            time.sleep(0.2)
            self.assertIsNone(proc.poll(), "Mock process should be running")

            sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
            env = dict(os.environ, SHUTDOWN_SENTINEL=sentinel)
            res = subprocess.run(["bash", self.script_path, "--timeout", "3"], env=env, capture_output=True, text=True)

            self.assertEqual(res.returncode, 0)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("Mock bot process was not stopped by shutdown script")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_script_forces_sigkill_on_stubborn_process(self):
        """Verify shutdown script falls back to SIGKILL when process ignores SIGTERM, exiting code 1."""
        stubborn_script = os.path.join(self.test_dir, "eternal-feed-bot-main.py")
        with open(stubborn_script, "w") as f:
            f.write("""
import signal, time, sys
# Ignore termination signals
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    time.sleep(0.1)
""")

        proc = subprocess.Popen([sys.executable, stubborn_script])
        try:
            time.sleep(0.2)
            self.assertIsNone(proc.poll(), "Stubborn process should be running")

            sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
            env = dict(os.environ, SHUTDOWN_SENTINEL=sentinel)
            # Run with short 1s timeout
            res = subprocess.run(["bash", self.script_path, "--timeout", "1"], env=env, capture_output=True, text=True)

            self.assertEqual(res.returncode, 1)
            self.assertIn("Force killing lingering processes (SIGKILL)", res.stdout)
            # Process must have been killed by SIGKILL
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("Stubborn bot process was not terminated with SIGKILL")
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_script_sqlite_wal_checkpoint(self):
        """Verify SQLite WAL checkpoint PRAGMA is executed on feedbot.db."""
        mock_feed_dir = os.path.join(self.test_dir, "eternal-feed-bot")
        os.makedirs(mock_feed_dir, exist_ok=True)
        db_path = os.path.join(mock_feed_dir, "feedbot.db")

        # Initialize SQLite database with WAL mode
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, msg TEXT);")
        conn.execute("INSERT INTO test_data (msg) VALUES ('hello world');")
        conn.commit()
        conn.close()

        sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
        env = dict(
            os.environ,
            SHUTDOWN_SENTINEL=sentinel,
            HOME=self.test_dir,
            DEFAULT_AUTO_DIR=mock_feed_dir
        )

        res = subprocess.run(["bash", self.script_path, "--timeout", "1"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Executing SQLite WAL checkpoint", res.stdout)

    def test_script_tmux_broadcasting_with_mock_tmux(self):
        """Verify signals are broadcast to tmux sessions and sessions are terminated when tmux is present."""
        bin_dir = os.path.join(self.test_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        mock_tmux = os.path.join(bin_dir, "tmux")
        tmux_log = os.path.join(self.test_dir, "tmux_calls.log")

        # Mock tmux command that records calls and simulates active sessions
        with open(mock_tmux, "w") as f:
            f.write(f"""#!/usr/bin/env bash
echo "$@" >> "{tmux_log}"
if [[ "$1" == "has-session" ]]; then
    exit 0
fi
exit 0
""")
        os.chmod(mock_tmux, 0o755)

        sentinel = os.path.join(self.test_dir, ".shutdown_in_progress")
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
            SHUTDOWN_SENTINEL=sentinel,
            HOME=self.test_dir
        )

        res = subprocess.run(["bash", self.script_path, "--timeout", "1"], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertTrue(os.path.exists(tmux_log))
        with open(tmux_log, "r") as f:
            calls = f.read()
        self.assertIn("send-keys -t bot C-c", calls)
        self.assertIn("send-keys -t feed-bot C-c", calls)
        self.assertIn("send-keys -t heartcast C-c", calls)
        self.assertIn("kill-session -t bot", calls)
        self.assertIn("kill-session -t feed-bot", calls)
        self.assertIn("kill-session -t heartcast", calls)


if __name__ == "__main__":
    unittest.main()
