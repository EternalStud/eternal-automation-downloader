import unittest
from downloader import (
    clean_media_title, format_clean_audio_filename, format_caption,
    normalize_facebook_image_url, extract_facebook_attachment_media,
    extract_facebook_image_urls, parse_twitter_status_id, split_telegram_text,
    _facebook_media_dedupe_key
)

class TestDownloader(unittest.TestCase):
    def test_clean_media_title(self):
        self.assertEqual(clean_media_title("Arijit Singh - Kesariya | Official Music Video | Brahmastra"), "Arijit Singh - Kesariya")
        self.assertEqual(clean_media_title("Song Title (Official HD Video)"), "Song Title")
        self.assertEqual(clean_media_title("Track [Official Audio]"), "Track")
        self.assertEqual(clean_media_title("789607747213866651"), "Audio Track")
        self.assertEqual(clean_media_title("media"), "Audio Track")
        self.assertEqual(clean_media_title(""), "Audio Track")
        self.assertEqual(clean_media_title("179K views · 213K reactions | Beautiful Sunset", default="Video"), "Beautiful Sunset")
        self.assertEqual(clean_media_title("179K views · 213K reactions", default="Video"), "Video")
        self.assertEqual(clean_media_title("1.5M views, 50K likes", default="Reel"), "Reel")


    def test_format_clean_audio_filename(self):
        self.assertEqual(format_clean_audio_filename("Arijit Singh - Kesariya | Brahmastra", ".mp3"), "Arijit Singh - Kesariya - Brahmastra.mp3")
        self.assertEqual(format_clean_audio_filename("Day dreaming - Esha Gupta", ".mp3"), "Day dreaming - Esha Gupta.mp3")
        self.assertEqual(format_clean_audio_filename("789607747213866651", ".m4a"), "Audio_Track.m4a")

    def test_format_caption_scenarios(self):
        # 1. Normal video
        cap = format_caption("Amazing Breakout Charts", is_audio_fallback=False, is_explicit_audio=False)
        self.assertEqual(cap, "🎬 <b>Amazing Breakout Charts</b>")

        # 2. Explicit /mp3 audio request (NO warning note)
        cap_audio = format_caption("Kesariya - Arijit Singh", is_audio_fallback=False, is_explicit_audio=True)
        self.assertEqual(cap_audio, "🎵 <b>Kesariya - Arijit Singh</b>")
        self.assertNotIn("49MB", cap_audio)

        # 3. Automatic 49MB fallback audio
        cap_fallback = format_caption("2 Hour Podcast", is_audio_fallback=True, is_explicit_audio=False)
        self.assertEqual(cap_fallback, "🎵 <b>2 Hour Podcast</b>\n\n⚠️ <i>Note: Audio format delivered because video exceeded 49MB limit.</i>")

    def test_parse_twitter_status_id(self):
        self.assertEqual(
            parse_twitter_status_id("https://x.com/GloryOfMind/status/2092237722656407796?s=20"),
            "2092237722656407796",
        )
        self.assertEqual(
            parse_twitter_status_id("https://twitter.com/user/statuses/12345"),
            "12345",
        )
        self.assertIsNone(parse_twitter_status_id("https://x.com/home"))

    def test_split_telegram_text(self):
        self.assertEqual(split_telegram_text(""), [])
        self.assertEqual(split_telegram_text("short"), ["short"])
        long = ("word " * 900).strip()
        chunks = split_telegram_text(long, limit=100)
        self.assertTrue(len(chunks) > 1)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual(" ".join(chunks).replace("  ", " "), long.replace("  ", " "))

    def test_facebook_attachment_parser_ignores_ads(self):
        # Simulated post JSON: 2 real carousel photos + an ad CDN path elsewhere.
        html = r'''
        <title>Consistency tips | Facebook</title>
        "mediaset_token":"pcb.1079729951241814",
        "all_subattachments":{"count":2,"nodes":[
          {"deduplication_key":"aaa","media":{"__typename":"Photo","viewer_image":{"height":1350,"width":1080,"uri":"https:\/\/scontent.xx.fbcdn.net\/v\/t39.99422-6\/111_1_1_n.png?stp=dst-jpg_tt6&ctp=s1080x1350"},"id":"111"}},
          {"deduplication_key":"bbb","media":{"__typename":"Photo","viewer_image":{"height":1350,"width":1080,"uri":"https:\/\/scontent.xx.fbcdn.net\/v\/t39.99422-6\/222_2_2_n.png?stp=dst-jpg_tt6&ctp=s1080x1350"},"id":"222"}}
        ]}
        "uri":"https:\/\/scontent.xx.fbcdn.net\/v\/t45.1600-4\/999_ad_ad_n.jpg?_nc_cat=1"
        "uri":"https:\/\/scontent.xx.fbcdn.net\/v\/t15.5256-10\/888_vid_vid_n.jpg?_nc_cat=1"
        '''
        data = extract_facebook_attachment_media(html)
        self.assertEqual(data["expected_count"], 2)
        self.assertEqual(data["mediaset_token"], "pcb.1079729951241814")
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(all(it["kind"] == "photo" for it in data["items"]))
        self.assertEqual(data["items"][0]["id"], "111")
        self.assertEqual(data["items"][1]["id"], "222")

        # Fallback scraper must also reject ad CDN families.
        fallback = extract_facebook_image_urls(html)
        joined = " ".join(fallback)
        self.assertNotIn("t45.1600", joined)
        self.assertNotIn("t15.5256", joined)

    def test_normalize_facebook_image_url_filters(self):
        good = normalize_facebook_image_url(
            r"https:\/\/scontent.xx.fbcdn.net\/v\/t39.99422-6\/111_1_1_n.jpg?ctp=s1080x1350"
        )
        self.assertIsNotNone(good)
        self.assertIn("fbcdn.net", good)

        self.assertIsNone(normalize_facebook_image_url(
            "https://scontent.xx.fbcdn.net/v/t45.1600-4/ad.jpg"
        ))
        self.assertIsNone(normalize_facebook_image_url(
            "https://static.xx.fbcdn.net/rsrc.php/v1/img.png"
        ))
        self.assertEqual(
            _facebook_media_dedupe_key("https://x.fbcdn.net/v/t39.99422-6/111_2_3_n.jpg?a=1"),
            "111_2_3_n",
        )

if __name__ == '__main__':
    unittest.main()
