import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import arbitre_server
import community
import transcript_server as ts


class YoutubeUrlTests(unittest.TestCase):
    def test_only_youtube_links_are_downloaded(self):
        accepted = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10": "dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "http://m.youtube.com/shorts/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        }
        for raw, vid in accepted.items():
            self.assertEqual(ts.youtube_url(raw), f"https://www.youtube.com/watch?v={vid}")
        for raw in ("http://127.0.0.1:8766/arbitre.html?v=dQw4w9WgXcQ", "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
                    "file:///C:/Windows/win.ini", "https://evil.example/youtu.be/dQw4w9WgXcQ", "", None):
            self.assertEqual(ts.youtube_url(raw), "", raw)

    def test_youtube_job_rejects_internal_addresses(self):
        with self.assertRaises(ValueError):
            arbitre_server.youtube_work({"url": "http://169.254.169.254/latest/meta-data"}, ({}, "127.0.0.1"))


class ErrorAndLinkTests(unittest.TestCase):
    def test_public_error_hides_internal_paths(self):
        message = arbitre_server.public_error(RuntimeError(r"ffmpeg a échoué: C:\Users\x\AppData\Local\Temp\seg_0.mp3 introuvable" "\nTraceback"))
        self.assertNotIn("Users", message)
        self.assertNotIn("Traceback", message)
        self.assertIn("[fichier]", message)

    def test_channel_links_are_youtube_only(self):
        self.assertEqual(arbitre_server.channel_url("https://www.youtube.com/@chaine"), "https://www.youtube.com/@chaine")
        for raw in ("javascript:alert(1)", "https://evil.example/@chaine", "https://www.youtube.com/\"onmouseover=x"):
            self.assertEqual(arbitre_server.channel_url(raw), "")


class RateLimitTests(unittest.TestCase):
    def test_concurrent_attempts_cannot_exceed_limit(self):
        key = "test:concurrent"
        community._hits.pop(key, None)
        results, barrier = [], threading.Barrier(20)

        def attempt():
            barrier.wait()
            results.append(community._hit(key, 5, 60))

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(False), 5)

    def test_successful_attempt_is_released(self):
        key = "test:release"
        community._hits.pop(key, None)
        for _ in range(10):
            self.assertFalse(community._hit(key, 2, 60))
            community._release(key)


class AnalysisCleaningTests(unittest.TestCase):
    def test_malformed_analysis_is_normalized(self):
        cleaned = community._clean_analysis({"orateurs": [None, "x", {"nom": 3, "arguments": [None, {"these": "ok"}], "faits_a_verifier": [1, "fait"]}],
                                             "synthese": ["pas", "une chaîne"], "carte": {"arguments": "x", "these": "texte"}})
        self.assertEqual(len(cleaned["orateurs"]), 1)
        speaker = cleaned["orateurs"][0]
        self.assertEqual((speaker["nom"], speaker["arguments"], speaker["faits_a_verifier"]), ("Orateur", [{"these": "ok"}], ["fait"]))
        self.assertEqual((cleaned["synthese"], cleaned["carte"]["arguments"], cleaned["carte"]["these"]), ("", [], {}))
        self.assertIsNone(community._clean_analysis({"orateurs": [None]}))


class SecretEncryptionTests(unittest.TestCase):
    def test_secrets_are_encrypted_at_rest_and_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            key_file = os.path.join(folder, "keys", "secret.key")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            with patch.object(community, "SECRET_KEY_FILE", key_file), patch.object(community, "_cipher_cache", []):
                community.set_secret(conn, "groq", "gsk_exemple_secret_1234567890")
                stored = json.loads(conn.execute("SELECT value FROM settings").fetchone()["value"])
                self.assertTrue(stored.startswith(community.SECRET_PREFIX))
                self.assertNotIn("gsk_", stored)
                self.assertEqual(community.get_secret(conn, "groq"), "gsk_exemple_secret_1234567890")
                self.assertTrue(os.path.isfile(key_file))
            conn.close()


class JobOwnershipTests(unittest.TestCase):
    def test_job_is_visible_only_to_its_owner(self):
        arbitre_server.jobs["abc"] = {"owner": "user:1", "status": "done"}
        try:
            self.assertIsNotNone(arbitre_server.find_job("abc", "user:1"))
            self.assertIsNone(arbitre_server.find_job("abc", "user:2"))
            self.assertIsNone(arbitre_server.find_job("abc", "ip:127.0.0.1"))
        finally:
            arbitre_server.jobs.pop("abc", None)


class CspTests(unittest.TestCase):
    def test_csp_allows_exactly_the_inline_script(self):
        with open("arbitre.html", "rb") as f:
            scripts = arbitre_server.INLINE_SCRIPT_RE.findall(f.read())
        self.assertEqual(len(scripts), 1)
        policy = arbitre_server.page_csp("/arbitre.html")
        expected = base64.b64encode(hashlib.sha256(scripts[0].replace(b"\r\n", b"\n")).digest()).decode()
        self.assertIn(f"'sha256-{expected}'", policy)
        self.assertNotIn("'unsafe-inline' https://www.youtube.com", policy)
        self.assertIn("object-src 'none'", policy)


if __name__ == "__main__":
    unittest.main()
