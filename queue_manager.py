import os
import time
import shutil
import gc
import threading
from collections import deque
from config import (
    FAST_LANE_MAX_BYTES, PAID_HEAVY_MAX_BYTES, OWNER_MAX_BYTES, FREE_TRIAL_MAX_BYTES, OWNER_ID,
    DOWNLOADS_DIR
)

class IntelligentQueueManager:
    """
    Intelligent Multi-Tier Traffic & Server Health Guard for Samsung Galaxy Phone Server.
    
    1. Multi-Lane Scheduling:
       - Fast Lane (Single links): Non-blocking concurrency (up to 4 items in parallel).
       - Heavy Lane (> 50MB single items): Serialized (1 heavy upload at a time).
       - Playlist Lane (Batch downloads): Dedicated queue (1 active playlist at a time).
    
    2. Dynamic Resource & Thermal Throttling:
       - Regulates inter-item pacing based on phone battery temperature and RAM.
    
    3. Storage Protection:
       - Real-time disk space verification before downloading any media chunk.
    """
    def __init__(self):
        # Heavy lane (> 50MB)
        self._heavy_lock = threading.Lock()
        self._heavy_queue = deque()
        self._active_heavy_job = None
        self._heavy_cv = threading.Condition(self._heavy_lock)

        # Playlist lane (batch items)
        self._playlist_lock = threading.Lock()
        self._playlist_queue = deque()
        self._active_playlist_job = None
        self._playlist_cv = threading.Condition(self._playlist_lock)

    def get_user_quota_bytes(self, user_id: str, is_paid: bool) -> int:
        if str(user_id) == str(OWNER_ID):
            return OWNER_MAX_BYTES  # 4 GB
        if is_paid:
            return PAID_HEAVY_MAX_BYTES  # 2 GB
        return FREE_TRIAL_MAX_BYTES  # 50 MB

    def get_playlist_item_limit(self, user_id: str, role: str, trials_left: int) -> int:
        """
        Determines the safe item batch limit for a playlist based on subscription tier.
        - Admin / Owner: 100% Unrestricted (up to 250 items)
        - Paid Subscriber (₹20/mo): 10 items per batch
        - Free / Trial: Remaining monthly quota (max 5 items per batch)
        """
        if str(user_id) == str(OWNER_ID) or role == "owner":
            return 250
        if role == "subscriber":
            return 10
        return max(1, min(int(trials_left), 5))

    def is_heavy_job(self, estimated_bytes: int) -> bool:
        return estimated_bytes > FAST_LANE_MAX_BYTES

    def acquire_heavy_slot(self, user_id: str, job_id: str, notify_cb=None) -> int:
        """
        Enqueues a heavy job (> 50MB) to ensure only 1 heavy upload runs at a time.
        Owner gets instant execution with zero queue waiting.
        """
        if str(user_id) == str(OWNER_ID):
            return 0

        pos = 0
        with self._heavy_lock:
            if self._active_heavy_job is None:
                self._active_heavy_job = job_id
                return 0

            self._heavy_queue.append(job_id)
            pos = len(self._heavy_queue)

        # Call notify_cb outside lock to prevent blocking mutex during Telegram API I/O
        if notify_cb:
            try:
                notify_cb(pos)
            except Exception:
                pass

        with self._heavy_lock:
            while self._active_heavy_job != job_id:
                from downloader import is_job_cancelled
                if is_job_cancelled(job_id):
                    if job_id in self._heavy_queue:
                        try:
                            self._heavy_queue.remove(job_id)
                        except ValueError:
                            pass
                    return -1

                self._heavy_cv.wait(timeout=2.0)

            return 0

    def release_heavy_slot(self, job_id: str):
        with self._heavy_lock:
            # If job was still waiting in queue, remove it immediately
            if job_id in self._heavy_queue:
                try:
                    self._heavy_queue.remove(job_id)
                except ValueError:
                    pass

            if self._active_heavy_job == job_id:
                self._active_heavy_job = None
                from downloader import is_job_cancelled
                # Advance to next valid non-cancelled job
                while self._heavy_queue:
                    next_job = self._heavy_queue.popleft()
                    if not is_job_cancelled(next_job):
                        self._active_heavy_job = next_job
                        break
                self._heavy_cv.notify_all()

    def acquire_playlist_slot(self, user_id: str, job_id: str, notify_cb=None) -> int:
        """
        Enqueues a playlist job to ensure only 1 active playlist runs at a time.
        Owner gets instant execution with zero queue waiting.
        """
        if str(user_id) == str(OWNER_ID):
            return 0

        pos = 0
        with self._playlist_lock:
            if self._active_playlist_job is None:
                self._active_playlist_job = job_id
                return 0

            self._playlist_queue.append(job_id)
            pos = len(self._playlist_queue)

        # Call notify_cb outside lock to prevent blocking mutex during Telegram API I/O
        if notify_cb:
            try:
                notify_cb(pos)
            except Exception:
                pass

        with self._playlist_lock:
            while self._active_playlist_job != job_id:
                from downloader import is_job_cancelled
                if is_job_cancelled(job_id):
                    if job_id in self._playlist_queue:
                        try:
                            self._playlist_queue.remove(job_id)
                        except ValueError:
                            pass
                    return -1

                self._playlist_cv.wait(timeout=2.0)

            return 0


    def release_playlist_slot(self, job_id: str):
        with self._playlist_lock:
            if job_id in self._playlist_queue:
                try:
                    self._playlist_queue.remove(job_id)
                except ValueError:
                    pass

            if self._active_playlist_job == job_id:
                self._active_playlist_job = None
                from downloader import is_job_cancelled
                while self._playlist_queue:
                    next_job = self._playlist_queue.popleft()
                    if not is_job_cancelled(next_job):
                        self._active_playlist_job = next_job
                        break
                self._playlist_cv.notify_all()


    def get_adaptive_cooldown(self) -> float:
        """
        Calculates adaptive inter-item delay (seconds) based on device temperature and load.
        """
        try:
            from health import get_server_metrics
            metrics = get_server_metrics()
            temp = metrics.get("battery_temp") or 30.0
            if temp > 42.0:
                # Device is hot: insert 4.0s cooling pause and force garbage collection
                gc.collect()
                return 4.0
            elif temp > 38.0:
                # Device is warm: 2.0s pause
                return 2.0
            return 1.0  # Normal healthy condition: 1.0s pace
        except Exception:
            return 1.0

    def ensure_storage_available(self, min_free_mb: int = 500) -> bool:
        """
        Ensures phone storage has at least min_free_mb free space before writing new media.
        """
        try:
            disk = shutil.disk_usage(DOWNLOADS_DIR if os.path.exists(DOWNLOADS_DIR) else os.path.expanduser("~"))
            free_mb = disk.free / (1024 * 1024)
            if free_mb < min_free_mb:
                from downloader import cleanup_old_downloads
                cleanup_old_downloads(max_age_seconds=300)
                gc.collect()
                disk_after = shutil.disk_usage(DOWNLOADS_DIR if os.path.exists(DOWNLOADS_DIR) else os.path.expanduser("~"))
                return (disk_after.free / (1024 * 1024)) >= 200
            return True
        except Exception:
            return True

# Global Singleton
queue_manager = IntelligentQueueManager()

