import unittest
from downloader import extract_urls, is_safe_url, detect_platform

class TestUrls(unittest.TestCase):
    def test_extract_urls_various_platforms(self):
        text = (
            "Check this out: https://www.instagram.com/p/CefhFsOIoFz/?utm_source=ig_web_button_native_share "
            "and this video https://youtu.be/dQw4w9WgXcQ?si=123 "
            "plus pin https://pin.it/1aYQSlO0l!"
        )
        urls = extract_urls(text)
        self.assertEqual(len(urls), 3)
        self.assertIn("https://www.instagram.com/p/CefhFsOIoFz", urls[0])
        self.assertIn("https://youtu.be/dQw4w9WgXcQ", urls[1])
        self.assertIn("https://pin.it/1aYQSlO0l", urls[2])

    def test_extract_urls_with_intent_and_plain_domains(self):
        text = "intent://instagram.com/p/12345/ and www.youtube.com/watch?v=abc"
        urls = extract_urls(text)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://instagram.com/p/12345/")
        self.assertEqual(urls[1], "https://www.youtube.com/watch?v=abc")

    def test_is_safe_url_trusted_domains(self):
        self.assertTrue(is_safe_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(is_safe_url("https://instagram.com/p/CefhFsOIoFz/"))
        self.assertTrue(is_safe_url("https://pin.it/1aYQSlO0l"))
        self.assertTrue(is_safe_url("https://x.com/user/status/123"))
        self.assertTrue(is_safe_url("https://www.facebook.com/watch/?v=123"))

    def test_is_safe_url_ssrf_rejection(self):
        # Localhost & Loopback
        self.assertFalse(is_safe_url("http://localhost/admin"))
        self.assertFalse(is_safe_url("http://127.0.0.1:8080/secret"))
        self.assertFalse(is_safe_url("http://0.0.0.0:8000"))
        self.assertFalse(is_safe_url("http://[::1]/"))

        # Private IPv4 ranges
        self.assertFalse(is_safe_url("http://192.168.1.1/router"))
        self.assertFalse(is_safe_url("http://10.0.0.5/internal"))
        self.assertFalse(is_safe_url("http://172.16.0.1/db"))

        # Invalid schemes
        self.assertFalse(is_safe_url("ftp://example.com/file"))
        self.assertFalse(is_safe_url("file:///etc/passwd"))
        self.assertFalse(is_safe_url("javascript:alert(1)"))

    def test_is_safe_url_fails_closed_on_unresolvable(self):
        # Non-existent domain must fail closed (return False)
        self.assertFalse(is_safe_url("https://nonexistent-domain-xyz-123456789.invalid/path"))

    def test_detect_platform(self):
        self.assertEqual(detect_platform("https://www.youtube.com/watch?v=123"), "YouTube")
        self.assertEqual(detect_platform("https://youtu.be/123"), "YouTube")
        self.assertEqual(detect_platform("https://instagram.com/p/123"), "Instagram")
        self.assertEqual(detect_platform("https://pin.it/123"), "Pinterest")
        self.assertEqual(detect_platform("https://x.com/user/status/123"), "X (Twitter)")
        self.assertEqual(detect_platform("https://tiktok.com/@user/video/123"), "TikTok")
        self.assertEqual(detect_platform("https://facebook.com/reel/123"), "Facebook")
        self.assertEqual(detect_platform("https://example.com/video.mp4"), "Media")

if __name__ == '__main__':
    unittest.main()
