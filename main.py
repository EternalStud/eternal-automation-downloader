import sys
import time
import signal
import threading
import config
from config import (
    BOT_TOKEN, OWNER_ID, UPI_ID, STATE_FILE, IS_GITHUB_ACTIONS,
    DOWNLOAD_EXECUTOR, validate_runtime_environment
)
from telegram_api import TG_SESSION, set_bot_commands
from users import (
    load_users, load_json, save_json,
    check_and_send_expiry_reminders, send_backup_to_owner
)
from downloader import cleanup_old_downloads
from health import check_hardware_health_and_alert
from handlers import run_bot_cycle
from mtproto_uploader import mtproto_uploader

# -----------------------
# SIGNAL & LIFECYCLE MANAGEMENT
# -----------------------

RUNNING = True

def handle_exit_signal(sig, frame):
    global RUNNING
    print(f"\n🛑 Received signal {sig}. Initiating graceful bot shutdown...")
    RUNNING = False

signal.signal(signal.SIGINT, handle_exit_signal)
signal.signal(signal.SIGTERM, handle_exit_signal)

def drain_in_flight_tasks(timeout=10.0):
    """
    Drains active tasks in DOWNLOAD_EXECUTOR for up to timeout seconds.
    Cancels queued tasks and waits for active worker threads to finish.
    """
    start_time = time.time()
    print(f"⏳ Draining in-flight tasks (timeout: {timeout:.1f}s)...")

    # 1. Cancel queued/unstarted tasks
    executor = getattr(config, "DOWNLOAD_EXECUTOR", DOWNLOAD_EXECUTOR)
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    except Exception as e:
        print(f"⚠️ Warning shutting down executor queue: {e}")

    # 2. Wait for active worker threads to finish
    threads = list(getattr(executor, "_threads", []))
    for t in threads:
        elapsed = time.time() - start_time
        remaining = max(0.0, timeout - elapsed)
        if remaining <= 0:
            break
        if t.is_alive() and t != threading.current_thread():
            t.join(timeout=remaining)

    alive_threads = [t for t in getattr(executor, "_threads", []) if t.is_alive() and t != threading.current_thread()]
    if alive_threads:
        print(f"⚠️ Timeout ({timeout:.1f}s) expired with {len(alive_threads)} active task(s). Forcing teardown.")
    else:
        print("✅ All in-flight tasks drained successfully.")

def shutdown_bot(state=None, timeout=10.0, drain_executor=True):
    """
    Coordinated graceful shutdown procedure:
    1. Sets RUNNING = False
    2. Drains in-flight tasks up to timeout seconds
    3. Stops MTProto engine and closes Pyrogram sessions
    4. Atomically persists state.json and channel_seen_posts.json
    5. Cleans residual temporary files in downloads directory
    """
    global RUNNING
    RUNNING = False
    print("🛑 Executing graceful bot shutdown sequence...")

    # 1. Drain in-flight worker tasks
    if drain_executor:
        try:
            drain_in_flight_tasks(timeout=timeout)
        except Exception as e:
            print(f"⚠️ Error draining in-flight tasks: {e}")

    # 2. Teardown MTProto uploader and Pyrogram clients
    try:
        if hasattr(mtproto_uploader, "_loop") and mtproto_uploader._loop and mtproto_uploader._loop.is_running():
            import asyncio
            async def _close_mtproto_clients():
                for client in (getattr(mtproto_uploader, "_bot_client", None), getattr(mtproto_uploader, "_user_client", None)):
                    if client and getattr(client, "is_connected", False):
                        try:
                            await client.stop()
                        except Exception:
                            pass
            try:
                future = asyncio.run_coroutine_threadsafe(_close_mtproto_clients(), mtproto_uploader._loop)
                future.result(timeout=2.0)
            except Exception:
                pass
            try:
                mtproto_uploader._loop.call_soon_threadsafe(mtproto_uploader._loop.stop)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Warning during MTProto teardown: {e}")

    # 3. Flush state and persistent databases
    try:
        if state is not None:
            target_state_file = getattr(config, "STATE_FILE", STATE_FILE)
            save_json(target_state_file, state)
            print("💾 Saved state.json")
    except Exception as e:
        print(f"⚠️ Error saving state.json during shutdown: {e}")

    try:
        from handlers import save_channel_seen_posts
        save_channel_seen_posts()
        print("💾 Flushed channel_seen_posts.json")
    except Exception as e:
        print(f"⚠️ Error flushing channel seen posts during shutdown: {e}")

    # 4. Final sweep of transient files
    try:
        cleanup_old_downloads(max_age_seconds=0)
        print("🧹 Cleaned temporary downloads directory")
    except Exception as e:
        print(f"⚠️ Error during downloads cleanup: {e}")

    print("👋 Bot stopped cleanly. Goodbye!")

# -----------------------
# MAIN BOT ENTRY POINT
# -----------------------

def main():
    global RUNNING
    print("=" * 60)
    print("🚀 ETERNAL AUTOMATION BOT - PRODUCTION ENGINE")
    print("=" * 60)

    # 1. Runtime Environment Diagnostics
    validate_runtime_environment()

    # 2. MTProto High-Speed Engine Initialization
    mtproto_uploader.start()

    # 3. Database & State Initialization
    state = load_json(STATE_FILE)
    if not isinstance(state, dict):
        state = {}

    users = load_users()
    last_update_id = state.get("last_update_id", 0)
    print(f"👥 Database users: {len(users)} (UPI: {UPI_ID})")
    print(f"📡 Tracking updates from offset: {last_update_id}")

    # 4. Register Commands Menu with Telegram
    set_bot_commands()

    # 5. Mode Execution
    try:
        if IS_GITHUB_ACTIONS:
            print("🤖 Running in single-cycle mode (GitHub Actions runner)")
            max_id = run_bot_cycle(last_update_id)
            state["last_update_id"] = max_id
            save_json(STATE_FILE, state)
            print(f"Saved update id: {max_id}")
        else:
            print("🟢 Running in 24/7 Real-Time Dedicated Server Mode...")

            # Startup: clean stale temp downloads
            cleanup_old_downloads(max_age_seconds=60)

            last_cleanup_time = time.time()
            last_reminder_time = time.time()
            last_backup_time = time.time()
            last_health_check_time = time.time()

            while RUNNING:
                try:
                    new_max_id = run_bot_cycle(last_update_id)
                    if new_max_id != last_update_id:
                        last_update_id = new_max_id
                        state["last_update_id"] = last_update_id
                        save_json(STATE_FILE, state)

                    now = time.time()
                    # 1. Periodic cleanup sweep: clean orphaned files every 5 minutes
                    if now - last_cleanup_time >= 300:
                        cleanup_old_downloads(max_age_seconds=900)
                        last_cleanup_time = now

                    # 2. Periodic hardware health & alert check: every 5 minutes
                    if now - last_health_check_time >= 300:
                        check_hardware_health_and_alert()
                        last_health_check_time = now

                    # 3. Periodic expiry reminder sweep: check once per hour
                    if now - last_reminder_time >= 3600:
                        check_and_send_expiry_reminders()
                        last_reminder_time = now

                    # 4. Periodic automated database backup to Owner: every 24 hours
                    if now - last_backup_time >= 86400:
                        send_backup_to_owner()
                        last_backup_time = now

                    # 5. Periodic autonomous token refresh: every 12 hours
                    if now - getattr(main, "_last_cookie_refresh", 0) >= 43200:
                        try:
                            from cookie_refresher import refresh_visitor_tokens
                            refresh_visitor_tokens()
                        except Exception:
                            pass
                        main._last_cookie_refresh = now

                    # 6. Periodic platform cookie health check & alert: every 24 hours
                    if now - getattr(main, "_last_cookie_health_check", 0) >= 86400:
                        try:
                            from cookie_refresher import check_cookies_health_and_alert
                            check_cookies_health_and_alert()
                        except Exception:
                            pass
                        main._last_cookie_health_check = now

                except Exception as e:
                    print(f"⚠️ Error in polling loop: {e}")
                    time.sleep(1)
    finally:
        shutdown_bot(state=state, timeout=10.0)

if __name__ == "__main__":
    main()
