import unittest
import time
from handlers import normalize_channel_headline, check_and_record_channel_post, _channel_seen_posts

class TestChannelDeduplication(unittest.TestCase):
    def setUp(self):
        _channel_seen_posts.clear()

    def test_headline_normalization(self):
        t1 = "#Companies #BL\n\nACME Solar Holdings secures SECI contract for 300 MW capacity"
        t2 = "<a href='https://thehindu.com/article123'>ACME Solar Holdings secures SECI contract for 300 MW capacity</a>"
        t3 = "ACME Solar Holdings secures SECI contract for 300 MW capacity"

        n1 = normalize_channel_headline(t1)
        n2 = normalize_channel_headline(t2)
        n3 = normalize_channel_headline(t3)

        self.assertEqual(n1, "acme solar holdings secures seci contract for 300 mw capacity")
        self.assertEqual(n1, n2)
        self.assertEqual(n2, n3)

    def test_duplicate_detection(self):
        post1 = "#Market #ET\n\nWhere to invest as market headwinds ease: Motilal Oswal backs midcaps"
        post2 = "<a href='https://economictimes.com/123'>Where to invest as market headwinds ease: Motilal Oswal backs midcaps</a>"

        # First post is fresh
        is_dup_1 = check_and_record_channel_post(chat_id="-1001327480374", msg_id=101, text=post1)
        self.assertFalse(is_dup_1)

        # Same message ID is not a duplicate
        is_dup_same = check_and_record_channel_post(chat_id="-1001327480374", msg_id=101, text=post1)
        self.assertFalse(is_dup_same)

        # Different message ID with same headline is DUPLICATE
        is_dup_2 = check_and_record_channel_post(chat_id="-1001327480374", msg_id=102, text=post2)
        self.assertTrue(is_dup_2)

if __name__ == "__main__":
    unittest.main()
