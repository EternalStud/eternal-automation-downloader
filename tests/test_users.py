import os
import time
import tempfile
import unittest
import urllib.parse
from datetime import datetime
from unittest.mock import patch
import users as users_mod
from users import (
    save_json, load_json, is_user_active,
    add_or_renew_subscription, get_upi_qr_url, save_users, load_users
)
from config import FREE_TRIAL_LIMIT

class TestUsers(unittest.TestCase):
    def setUp(self):
        # Create a temporary file for USERS_FILE to avoid overwriting real data
        self.temp_users_fd, self.temp_users_path = tempfile.mkstemp(suffix=".json")
        self.users_file_patcher = patch("users.USERS_FILE", self.temp_users_path)
        self.users_file_patcher.start()
        
        users_mod.invalidate_users_cache()

    def tearDown(self):
        self.users_file_patcher.stop()
        if os.path.exists(self.temp_users_path):
            os.remove(self.temp_users_path)
        try:
            os.close(self.temp_users_fd)
        except Exception:
            pass

    def test_atomic_save_and_load_json(self):
        test_file = os.path.join(tempfile.gettempdir(), "test_eternal_users.json")
        data = {"key": "value", "count": 42, "items": [1, 2, 3]}

        save_json(test_file, data)
        loaded = load_json(test_file)
        self.assertEqual(loaded, data)
        if os.path.exists(test_file):
            os.remove(test_file)

    def test_is_user_active_roles(self):
        now = int(time.time())
        current_month = datetime.now().strftime("%Y-%m")
        users = {
            "999999999": {"name": "Owner", "role": "owner", "expires_at": None},
            "1111111111": {"name": "ActiveSub", "role": "subscriber", "expires_at": now + 86400 * 15},
            "2222222222": {"name": "ExpiredSub", "role": "subscriber", "expires_at": now - 3600},
            "3333333333": {"name": "TrialActive", "role": "trial", "trial_used": 1, "free_month": current_month},
            "4444444444": {"name": "TrialExhausted", "role": "trial", "trial_used": FREE_TRIAL_LIMIT, "free_month": current_month}
        }

        # Owner
        act, rle, days, name, trials = is_user_active("999999999", users)
        self.assertTrue(act)
        self.assertEqual(rle, "owner")

        # Active Subscriber
        act, rle, days, name, trials = is_user_active("1111111111", users)
        self.assertTrue(act)
        self.assertEqual(rle, "subscriber")
        self.assertEqual(days, 15)

        # Expired Subscriber
        act, rle, days, name, trials = is_user_active("2222222222", users)
        self.assertFalse(act)
        self.assertEqual(rle, "subscriber")
        self.assertEqual(days, 0)

        # Trial Active (1 used -> FREE_TRIAL_LIMIT - 1 left)
        act, rle, days, name, trials = is_user_active("3333333333", users)
        self.assertTrue(act)
        self.assertEqual(rle, "trial")
        self.assertEqual(trials, FREE_TRIAL_LIMIT - 1)

        # Trial Exhausted (FREE_TRIAL_LIMIT used -> 0 left)
        act, rle, days, name, trials = is_user_active("4444444444", users)
        self.assertFalse(act)
        self.assertEqual(rle, "trial")
        self.assertEqual(trials, 0)

        # Brand new visitor
        act, rle, days, name, trials = is_user_active("9999999999", users)
        self.assertTrue(act)
        self.assertEqual(rle, "trial")
        self.assertEqual(trials, FREE_TRIAL_LIMIT)

    @patch("users.send_msg")
    def test_add_or_renew_subscription_and_referral(self, mock_send_msg):
        mock_send_msg.return_value.ok = True
        users = {
            "100": {"name": "Alice", "role": "subscriber", "expires_at": int(time.time()) + 86400 * 10, "referral_count": 0},
            "200": {"name": "Bob", "role": "trial", "trial_used": 3, "referred_by": "100"}
        }

        save_users(users)

        # Bob subscribes -> Bob gets 30 days, Alice gets +7 bonus days
        bob_sub = add_or_renew_subscription("200", days=30, name="Bob")
        self.assertEqual(bob_sub["role"], "subscriber")
        self.assertGreater(bob_sub["expires_at"], int(time.time()) + 86400 * 29)

        updated_users = load_users()
        alice = updated_users["100"]
        self.assertEqual(alice["referral_count"], 1)
        self.assertGreaterEqual(alice["expires_at"], int(time.time()) + 86400 * 16)
        self.assertTrue(mock_send_msg.called)

    def test_get_upi_qr_url(self):
        with patch("users.UPI_ID", "testuser@upi"):
            url, upi_id = get_upi_qr_url("123456", amount=20)
            self.assertTrue("upi%3A%2F%2Fpay" in url or "upi://" in urllib.parse.unquote(url))
            self.assertIn("20", url)
            self.assertEqual(upi_id, "testuser@upi")

if __name__ == '__main__':
    unittest.main()
