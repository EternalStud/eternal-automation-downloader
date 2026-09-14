import unittest
from telegram_api import escape_html
from handlers import check_rate_limit, USER_DOWNLOAD_HISTORY
import time
from config import MAX_DOWNLOADS_PER_HOUR

class TestTelegramAndSecurity(unittest.TestCase):
    def test_escape_html(self):
        raw_name = 'User_Name<With>&Special"Chars'
        escaped = escape_html(raw_name)
        self.assertIn("&lt;", escaped)
        self.assertIn("&gt;", escaped)
        self.assertIn("&amp;", escaped)

    def test_rate_limiter_allows_owner(self):
        USER_DOWNLOAD_HISTORY["owner_id"] = [time.time()] * (MAX_DOWNLOADS_PER_HOUR + 10)
        allowed, wait, _ = check_rate_limit("owner_id", is_owner=True)
        self.assertTrue(allowed)
        self.assertEqual(wait, 0)

    def test_rate_limiter_blocks_normal_user(self):
        USER_DOWNLOAD_HISTORY["user_x"] = [time.time()] * MAX_DOWNLOADS_PER_HOUR
        allowed, wait, reason = check_rate_limit("user_x", is_owner=False)
        self.assertFalse(allowed)
        self.assertGreater(wait, 0)

    def test_media_group_separation(self):
        files = [
            ("text_only", "<b>Tweet thread header</b>", False),
            ("/tmp/fake_photo1.jpg", "Photo 1", False),
            ("/tmp/fake_photo2.png", "Photo 2", False),
        ]
        text_items = [f for f in files if f[0] == "text_only"]
        media_items = [f for f in files if f[0] != "text_only"]
        self.assertEqual(len(text_items), 1)
        self.assertEqual(len(media_items), 2)
        self.assertEqual(text_items[0][1], "<b>Tweet thread header</b>")
        self.assertEqual(media_items[0][0], "/tmp/fake_photo1.jpg")
        self.assertEqual(media_items[1][0], "/tmp/fake_photo2.png")

if __name__ == '__main__':
    unittest.main()
