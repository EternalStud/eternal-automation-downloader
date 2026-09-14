import os
import sys
import time
import logging
import threading
import asyncio
from pathlib import Path

# Python 3.14 compatibility: pre-initialize event loop for Pyrogram sync wrapper
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

try:
    from pyrogram import Client
    from pyrogram.errors import FloodWait, RPCError
    HAS_PYROGRAM = True
except ImportError:
    Client = None
    FloodWait = Exception
    RPCError = Exception
    HAS_PYROGRAM = False

from config import BOT_TOKEN, API_ID, API_HASH, USER_SESSION_STRING, OWNER_ID


log = logging.getLogger('mtproto_uploader')

class MTProtoUploader:
    def __init__(self):
        self._loop = None
        self._thread = None
        self._bot_client = None
        self._user_client = None
        self._is_premium = False
        self._ready = threading.Event()
        self._heavy_lock = asyncio.Lock()  # Serializes heavy uploads to 1 at a time

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name='MTProtoWorker')
        self._thread.start()
        self._ready.wait(timeout=10)
        print('⚡ MTProto High-Speed Engine initialized.')

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init_clients())
        self._ready.set()
        self._loop.run_forever()

    async def _init_clients(self):
        # 1. Initialize Bot Client (up to 2GB support)
        # Using in_memory=True eliminates SQLite file locking issues on Termux/mobile hosts
        if API_ID and API_HASH and BOT_TOKEN:
            try:
                self._bot_client = Client(
                    'bot_mtproto',
                    api_id=API_ID,
                    api_hash=API_HASH,
                    bot_token=BOT_TOKEN,
                    in_memory=True,
                )
                await self._bot_client.start()
                print('🤖 MTProto Bot Client connected successfully (up to 2GB support).')
            except FloodWait as fw:
                wait_sec = int(fw.value) + 2
                print(f'⏳ Telegram MTProto FloodWait: auto-retrying in {wait_sec}s...')
                self._bot_client = None
            except Exception as e:
                print(f'⚠️ Warning initializing MTProto Bot Client: {e}')
                self._bot_client = None
        else:
            print('ℹ️ MTProto Bot Client not configured (missing API_ID, API_HASH, or BOT_TOKEN).')

        # 2. Initialize Telegram Premium User Client (up to 4GB support)
        # Using in_memory=True allows session string to operate purely in RAM without SQLite locks
        if API_ID and API_HASH and USER_SESSION_STRING:
            try:
                self._user_client = Client(
                    'premium_mtproto',
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=USER_SESSION_STRING,
                    in_memory=True,
                )
                await self._user_client.start()
                me = await self._user_client.get_me()
                is_prem = getattr(me, 'is_premium', False)
                self._is_premium = bool(is_prem)
                username = f"@{me.username}" if getattr(me, 'username', None) else f"ID:{me.id}"
                if is_prem:
                    print(f'⭐ MTProto Telegram Premium User Client connected: {me.first_name} ({username}) — 4GB upload capability ACTIVE.')
                else:
                    print(f'⚠️ MTProto User Client connected: {me.first_name} ({username}) — Note: Account does not have Telegram Premium (limited to 2GB).')
            except Exception as e:
                err_str = str(e)
                err_type = type(e).__name__
                if "AUTH_KEY_UNREGISTERED" in err_str or "AuthKeyUnregistered" in err_type:
                    print(f'❌ Telegram Premium user session is EXPIRED or REVOKED ({err_type}). Please re-generate USER_SESSION_STRING to enable >2GB uploads.')
                elif "SESSION_REVOKED" in err_str or "SessionRevoked" in err_type:
                    print(f'❌ Telegram Premium user session has been REVOKED. Please re-generate USER_SESSION_STRING to enable >2GB uploads.')
                elif "USER_DEACTIVATED" in err_str or "UserDeactivated" in err_type:
                    print(f'❌ Telegram Premium user account is DEACTIVATED. Please check USER_SESSION_STRING.')
                elif "SESSION_PASSWORD_NEEDED" in err_str or "SessionPasswordNeeded" in err_type:
                    print(f'❌ Telegram Premium user session requires 2FA password. Please re-generate USER_SESSION_STRING.')
                else:
                    print(f'⚠️ Telegram Premium user session INVALID or failed to connect ({err_type}: {err_str}). Uploads over 2GB will be disabled.')
                self._user_client = None
        else:
            if not USER_SESSION_STRING:
                print('ℹ️ Telegram Premium session (USER_SESSION_STRING) is not configured. MTProto uploads will be limited to 2GB via Bot client.')
            else:
                print('ℹ️ Telegram Premium session requires API_ID and API_HASH to be configured.')

    def is_available(self) -> bool:
        return bool(self._bot_client or self._user_client)

    def has_premium_user(self) -> bool:
        return bool(self._user_client and getattr(self._user_client, "is_connected", False) and getattr(self, "_is_premium", False))

    def _select_client(self, file_path: str):
        """
        Intelligently selects the appropriate MTProto client:
        - Files > 2GB (up to 4GB): MUST route through Telegram Premium User Client
        - Files <= 2GB: Prefer Bot Client so delivery originates from the bot, fallback to User Client
        """
        fsize = os.path.getsize(file_path) if (file_path and os.path.exists(file_path)) else 0
        TWO_GB = 2000 * 1024 * 1024
        FOUR_GB = 4000 * 1024 * 1024

        if fsize > FOUR_GB:
            mb = fsize // (1024 * 1024)
            print(f"❌ Cannot upload {mb}MB (>4GB): Exceeds Telegram's absolute maximum 4GB limit.")
            return None, True

        if fsize > TWO_GB:
            if not self._user_client or not getattr(self._user_client, "is_connected", False) or not getattr(self, "_is_premium", False):
                mb = fsize // (1024 * 1024)
                if self._user_client and getattr(self._user_client, "is_connected", False) and not getattr(self, "_is_premium", False):
                    print(f"❌ Cannot upload {mb}MB (>2GB): Telegram user account is connected but does NOT have an active Telegram Premium subscription.")
                else:
                    print(f"❌ Cannot upload {mb}MB (>2GB): Telegram Premium user client (USER_SESSION_STRING) is not available or inactive.")
                return None, True
            return self._user_client, False

        client = self._bot_client or self._user_client
        return client, False

    def send_video_sync(self, chat_id: int, file_path: str, caption: str = '', duration: int = None, width: int = None, height: int = None, thumb_path: str = None, progress_msg_id: int = None, tg_edit_func=None) -> bool:
        if not self.is_available() or not self._loop:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._send_video_async(chat_id, file_path, caption, duration, width, height, thumb_path, progress_msg_id, tg_edit_func),
            self._loop
        )
        return future.result(timeout=1800)

    def send_audio_sync(self, chat_id: int, file_path: str, caption: str = '', title: str = None, performer: str = None, duration: int = None, thumb_path: str = None, progress_msg_id: int = None, tg_edit_func=None) -> bool:
        if not self.is_available() or not self._loop:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._send_audio_async(chat_id, file_path, caption, title, performer, duration, thumb_path, progress_msg_id, tg_edit_func),
            self._loop
        )
        return future.result(timeout=1800)

    def send_document_sync(self, chat_id: int, file_path: str, caption: str = '', thumb_path: str = None, progress_msg_id: int = None, tg_edit_func=None) -> bool:
        if not self.is_available() or not self._loop:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._send_document_async(chat_id, file_path, caption, thumb_path, progress_msg_id, tg_edit_func),
            self._loop
        )
        return future.result(timeout=1800)

    async def _send_video_async(self, chat_id: int, file_path: str, caption: str, duration: int, width: int, height: int, thumb_path: str, progress_msg_id: int, tg_edit_func) -> bool:
        client, exceeds_limit = self._select_client(file_path)
        if not client:
            if exceeds_limit and tg_edit_func and progress_msg_id:
                try:
                    mb = os.path.getsize(file_path) // (1024 * 1024) if (file_path and os.path.exists(file_path)) else 0
                    tg_edit_func(
                        chat_id, progress_msg_id,
                        f"⚠️ <b>Upload Notice ({mb} MB)</b>\n\n"
                        f"This file exceeds the 2 GB Bot limit and requires a valid Telegram Premium user session.\n"
                        f"Contact {ADMIN_HANDLE} for assistance."
                    )
                except Exception:
                    pass
            return False

        last_edit_time = 0
        async def progress_cb(current, total):
            nonlocal last_edit_time
            now = time.time()
            if tg_edit_func and progress_msg_id and now - last_edit_time > 3.0:
                last_edit_time = now
                pct = int((current / total) * 100) if total else 0
                mb_cur = current / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                bar = '█' * (pct // 10) + '░' * (10 - (pct // 10))
                text = f"⏳ <b>Uploading to Telegram:</b>\n<code>[{bar}]</code> <b>{pct}%</b> ({mb_cur:.1f} / {mb_tot:.1f} MB)"

                try:
                    tg_edit_func(chat_id, progress_msg_id, text)
                except Exception:
                    pass

        kwargs = {
            "chat_id": int(chat_id),
            "supports_streaming": True,
            "progress": progress_cb,
        }
        if caption:
            kwargs["caption"] = caption
        if duration:
            kwargs["duration"] = int(duration)
        if width:
            kwargs["width"] = int(width)
        if height:
            kwargs["height"] = int(height)
        if thumb_path and os.path.exists(thumb_path):
            kwargs["thumb"] = str(thumb_path)

        try:
            await client.send_video(
                video=str(file_path),
                **kwargs
            )
            return True
        except FloodWait as fw:
            log.warning('FloodWait encountered: %ds', fw.value)
            await asyncio.sleep(fw.value + 1)
            return await self._send_video_async(chat_id, file_path, caption, duration, width, height, thumb_path, progress_msg_id, tg_edit_func)
        except Exception as e:
            import traceback
            print(f"❌ MTProto send_video error: {e}")
            traceback.print_exc()
            log.error('MTProto send_video error: %s', e)
            return False

    async def _send_audio_async(self, chat_id: int, file_path: str, caption: str, title: str, performer: str, duration: int, thumb_path: str, progress_msg_id: int = None, tg_edit_func = None) -> bool:
        client, exceeds_limit = self._select_client(file_path)
        if not client:
            if exceeds_limit and tg_edit_func and progress_msg_id:
                try:
                    mb = os.path.getsize(file_path) // (1024 * 1024) if (file_path and os.path.exists(file_path)) else 0
                    tg_edit_func(
                        chat_id, progress_msg_id,
                        f"⚠️ <b>Upload Notice ({mb} MB)</b>\n\n"
                        f"This file exceeds the 2 GB Bot limit and requires a valid Telegram Premium user session.\n"
                        f"Contact {ADMIN_HANDLE} for assistance."
                    )
                except Exception:
                    pass
            return False

        last_edit_time = 0
        async def progress_cb(current, total):
            nonlocal last_edit_time
            now = time.time()
            if tg_edit_func and progress_msg_id and now - last_edit_time > 3.0:
                last_edit_time = now
                pct = int((current / total) * 100) if total else 0
                mb_cur = current / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                bar = '█' * (pct // 10) + '░' * (10 - (pct // 10))
                text = f"⏳ <b>Uploading Audio to Telegram:</b>\n<code>[{bar}]</code> <b>{pct}%</b> ({mb_cur:.1f} / {mb_tot:.1f} MB)"
                try:
                    tg_edit_func(chat_id, progress_msg_id, text)
                except Exception:
                    pass

        kwargs = {
            "chat_id": int(chat_id),
            "progress": progress_cb,
        }
        if caption:
            kwargs["caption"] = caption
        if title:
            kwargs["title"] = title
        if performer:
            kwargs["performer"] = performer
        if duration:
            kwargs["duration"] = int(duration)
        if thumb_path and os.path.exists(thumb_path):
            kwargs["thumb"] = str(thumb_path)

        try:
            await client.send_audio(
                audio=str(file_path),
                **kwargs
            )
            return True
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
            return await self._send_audio_async(chat_id, file_path, caption, title, performer, duration, thumb_path, progress_msg_id, tg_edit_func)
        except Exception as e:
            log.error('MTProto send_audio error: %s', e)
            return False


    async def _send_document_async(self, chat_id: int, file_path: str, caption: str, thumb_path: str, progress_msg_id: int, tg_edit_func) -> bool:
        client, exceeds_limit = self._select_client(file_path)
        if not client:
            if exceeds_limit and tg_edit_func and progress_msg_id:
                try:
                    mb = os.path.getsize(file_path) // (1024 * 1024) if (file_path and os.path.exists(file_path)) else 0
                    tg_edit_func(
                        chat_id, progress_msg_id,
                        f"⚠️ <b>Upload Notice ({mb} MB)</b>\n\n"
                        f"This file exceeds the 2 GB Bot limit and requires a valid Telegram Premium user session.\n"
                        f"Contact {ADMIN_HANDLE} for assistance."
                    )
                except Exception:
                    pass
            return False

        last_edit_time = 0
        async def progress_cb(current, total):
            nonlocal last_edit_time
            now = time.time()
            if tg_edit_func and progress_msg_id and now - last_edit_time > 3.0:
                last_edit_time = now
                pct = int((current / total) * 100) if total else 0
                mb_cur = current / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                bar = '█' * (pct // 10) + '░' * (10 - (pct // 10))
                text = f"⏳ <b>Uploading to Telegram:</b>\n<code>[{bar}]</code> <b>{pct}%</b> ({mb_cur:.1f} / {mb_tot:.1f} MB)"

                try:
                    tg_edit_func(chat_id, progress_msg_id, text)
                except Exception:
                    pass

        kwargs = {
            "chat_id": int(chat_id),
            "progress": progress_cb,
        }
        if caption:
            kwargs["caption"] = caption
        if thumb_path and os.path.exists(thumb_path):
            kwargs["thumb"] = str(thumb_path)

        try:
            await client.send_document(
                document=str(file_path),
                **kwargs
            )
            return True
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
            return await self._send_document_async(chat_id, file_path, caption, thumb_path, progress_msg_id, tg_edit_func)
        except Exception as e:
            log.error('MTProto send_document error: %s', e)
            return False


# Global Singleton Instance
mtproto_uploader = MTProtoUploader()
