import unittest

import transcript_server as ts


class DownloadSpeedSettingsTests(unittest.TestCase):
    """Le débit YouTube est plafonné par connexion derrière le proxy : ces réglages évitent ~10 Kio/s."""

    def test_light_audio_formats_are_tried_first(self):
        formats = ts.YDL_DOWNLOAD["format"].split("/")
        self.assertEqual(formats[:2], ["233", "234"])
        self.assertLess(formats.index("wa[acodec^=opus]"), formats.index("bestaudio"))

    def test_downloads_use_parallel_fragments(self):
        self.assertGreaterEqual(ts.YDL_DOWNLOAD["concurrent_fragment_downloads"], 4)
        self.assertIn("dashy", ts.YDL_DOWNLOAD["extractor_args"]["youtube"]["formats"])


class YtDlpPlatformBinaryTests(unittest.TestCase):
    def test_selects_binary_for_supported_platforms(self):
        self.assertEqual(ts.bundled_ytdlp_name("Darwin", "arm64"), "yt-dlp_macos")
        self.assertEqual(ts.bundled_ytdlp_name("Windows", "AMD64"), "yt-dlp.exe")
        self.assertEqual(ts.bundled_ytdlp_name("Windows", "ARM64"), "yt-dlp_arm64.exe")
        self.assertEqual(ts.bundled_ytdlp_name("Linux", "x86_64"), "yt-dlp_linux")
        self.assertEqual(ts.bundled_ytdlp_name("Linux", "aarch64"), "yt-dlp_linux_aarch64")


if __name__ == "__main__":
    unittest.main()
