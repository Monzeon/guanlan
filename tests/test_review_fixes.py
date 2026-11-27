#!/usr/bin/env python
"""观澜技术评审兼容补丁的最小回归集。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cloud_sync  # noqa: E402
import server  # noqa: E402


class _TempStateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.saved = {
            "LAN_FILE": server.LAN_FILE,
            "LAN_SESSION_FILE": server.LAN_SESSION_FILE,
            "LAN_RATE_FILE": server.LAN_RATE_FILE,
            "APP_LOG": server.APP_LOG,
            "LAN_SESSIONS": dict(server.LAN_SESSIONS),
            "LAN_SESSIONS_LOADED": server.LAN_SESSIONS_LOADED,
            "LAN_FAIL_TIMES": list(server.LAN_FAIL_TIMES),
            "LAN_FAIL_BLOCKED_UNTIL": server.LAN_FAIL_BLOCKED_UNTIL,
            "LAN_RATE_LOADED": server.LAN_RATE_LOADED,
            "LAN_MODE": server.LAN_MODE,
            "LAN_OPEN": server.LAN_OPEN,
            "LAN_KEY": server.LAN_KEY,
            "LAN_IP": server.LAN_IP,
            "LAN_PORT": server.LAN_PORT,
            "AI_INFLIGHT": dict(server.AI_INFLIGHT),
        }
        server.LAN_FILE = self.data / "lan.json"
        server.LAN_SESSION_FILE = self.data / "lan-sessions.json"
        server.LAN_RATE_FILE = self.data / "lan-rate.json"
        server.APP_LOG = self.data / "app.log"
        server.LAN_SESSIONS = {}
        server.LAN_SESSIONS_LOADED = False
        server.LAN_FAIL_TIMES = []
        server.LAN_FAIL_BLOCKED_UNTIL = 0.0
        server.LAN_RATE_LOADED = False
        server.AI_INFLIGHT = {}
        old_logger = logging.getLogger("guanlan")
        for handler in list(old_logger.handlers):
            old_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        server._LOGGER = None

    def tearDown(self):
        server.LAN_FILE = self.saved["LAN_FILE"]
        server.LAN_SESSION_FILE = self.saved["LAN_SESSION_FILE"]
        server.LAN_RATE_FILE = self.saved["LAN_RATE_FILE"]
        server.APP_LOG = self.saved["APP_LOG"]
        server.LAN_SESSIONS = self.saved["LAN_SESSIONS"]
        server.LAN_SESSIONS_LOADED = self.saved["LAN_SESSIONS_LOADED"]
        server.LAN_FAIL_TIMES = self.saved["LAN_FAIL_TIMES"]
        server.LAN_FAIL_BLOCKED_UNTIL = self.saved["LAN_FAIL_BLOCKED_UNTIL"]
        server.LAN_RATE_LOADED = self.saved["LAN_RATE_LOADED"]
        server.LAN_MODE = self.saved["LAN_MODE"]
        server.LAN_OPEN = self.saved["LAN_OPEN"]
        server.LAN_KEY = self.saved["LAN_KEY"]
        server.LAN_IP = self.saved["LAN_IP"]
        server.LAN_PORT = self.saved["LAN_PORT"]
        server.AI_INFLIGHT = self.saved["AI_INFLIGHT"]
        old_logger = logging.getLogger("guanlan")
        for handler in list(old_logger.handlers):
            old_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        server._LOGGER = None


class CloudDefaultsTests(_TempStateTestCase):
    def test_new_config_is_off_by_default(self):
        cfg = cloud_sync.cloud_config_load(self.data, "hint")
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["explicit_choice"])
        self.assertTrue(cfg["secret"])

    def test_legacy_auto_enabled_config_is_migrated_off(self):
        (self.data / "cloud.json").write_text(json.dumps({
            "enabled": True,
            "endpoint": "https://example.invalid",
            "interval": 20,
        }), encoding="utf-8")
        cfg = cloud_sync.cloud_config_load(self.data, "hint")
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["explicit_choice"])
        saved = json.loads((self.data / "cloud.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["enabled"])

    def test_explicit_choice_is_preserved(self):
        (self.data / "cloud.json").write_text(json.dumps({
            "enabled": True, "explicit_choice": True,
            "endpoint": "https://example.invalid", "interval": 20,
        }), encoding="utf-8")
        cfg = cloud_sync.cloud_config_load(self.data, "hint")
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["explicit_choice"])


class LanDefaultsTests(_TempStateTestCase):
    def test_new_lan_config_is_off_by_default(self):
        cfg = server.lan_normalize_settings()
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["explicit_choice"])

    def test_legacy_auto_enabled_lan_config_is_migrated_off(self):
        server.LAN_FILE.write_text(json.dumps({"enabled": True, "key": "ABC234"}),
                                   encoding="utf-8")
        cfg = server.lan_normalize_settings()
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["explicit_choice"])
        self.assertEqual("ABC234", cfg["key"])

    def test_explicit_lan_choice_is_preserved(self):
        server.LAN_FILE.write_text(json.dumps({
            "enabled": True, "explicit_choice": True, "key": "ABC234",
        }), encoding="utf-8")
        cfg = server.lan_normalize_settings()
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["explicit_choice"])


class LibraryGateTests(unittest.TestCase):
    def test_shared_reads_coexist_and_exclusive_waits(self):
        gate = server.LibraryGate()
        self.assertTrue(gate.acquire(shared=True))
        self.assertTrue(gate.acquire(shared=True))
        entered = threading.Event()

        def writer():
            with gate:
                entered.set()

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        self.assertFalse(entered.wait(0.2))
        gate.release(shared=True)
        gate.release(shared=True)
        self.assertTrue(entered.wait(1.0))
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())

    def test_nested_shared_acquire_does_not_deadlock_on_waiting_writer(self):
        gate = server.LibraryGate()
        gate.acquire(shared=True)
        waiting_writer_started = threading.Event()

        def writer():
            waiting_writer_started.set()
            with gate:
                pass

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        self.assertTrue(waiting_writer_started.wait(0.5))
        gate.acquire(shared=True)
        gate.release(shared=True)
        gate.release(shared=True)
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())


class SecurityHelpersTests(_TempStateTestCase):
    def test_internal_error_payload_is_generic_and_logged(self):
        payload = server.internal_error_payload(
            RuntimeError("E:/private/path/secret.txt"), "unit test")
        self.assertEqual("内部错误", payload["error"])
        self.assertEqual(32, len(payload["ref"]))
        self.assertNotIn("secret.txt", json.dumps(payload, ensure_ascii=False))
        self.assertTrue(server.APP_LOG.is_file())

    def test_lan_session_token_is_random_and_stored_as_digest(self):
        token = server._lan_session_create()
        self.assertTrue(server._lan_session_valid(token))
        self.assertFalse(server._lan_session_valid("not-a-session"))
        saved = server.LAN_SESSION_FILE.read_text(encoding="utf-8")
        self.assertNotIn(token, saved)

    def test_global_failure_backoff_is_persisted(self):
        delay = 0
        for _ in range(5):
            delay = server._lan_rate_record_failure()
        self.assertGreater(delay, 0)
        self.assertTrue(server.LAN_RATE_FILE.is_file())
        server._lan_rate_reset()
        self.assertEqual(0, server._lan_rate_blocked())

    def test_slow_request_dedupe(self):
        key = server.slow_request_key("/api/ai/test", {}, {"x": 1}, "client")
        self.assertTrue(server.slow_request_enter(key))
        self.assertFalse(server.slow_request_enter(key))
        server.slow_request_leave(key)
        self.assertTrue(server.slow_request_enter(key))
        server.slow_request_leave(key)

    def test_api_lan_hides_key_from_non_local_request(self):
        server.LAN_FILE.write_text(json.dumps({
            "enabled": True, "explicit_choice": True, "key": "ABC234",
        }), encoding="utf-8")
        server.LAN_MODE = True
        server.LAN_OPEN = True
        server.LAN_KEY = "ABC234"
        server.LAN_IP = "192.168.1.7"
        server.LAN_PORT = 8790
        server.CURRENT_REQUEST.local = False
        hidden = server.api_lan(None, {})
        self.assertEqual("", hidden["key"])
        self.assertFalse(hidden["key_visible"])
        server.CURRENT_REQUEST.local = True
        visible = server.api_lan(None, {})
        self.assertEqual("ABC234", visible["key"])
        self.assertTrue(visible["key_visible"])


class FetchAndLoginTests(_TempStateTestCase):
    def test_validate_fetch_url_rejects_odd_ports_and_file_scheme(self):
        with patch.dict(os.environ, {
            "GL_ALLOW_ODD_PORTS": "",
            "GL_ALLOW_PRIVATE_FETCH": "",
        }, clear=False):
            with self.assertRaises(server.ValidationError):
                server.validate_fetch_url("http://93.184.216.34:8790/")
            with self.assertRaises(server.ValidationError):
                server.validate_fetch_url("file:///C:/Windows/win.ini")
            server.validate_fetch_url("http://93.184.216.34/")

    def test_login_form_uses_post_body_and_error_placeholder(self):
        self.assertIn('method="post" action="/api/lan/login"', server.LAN_LOGIN_HTML)
        self.assertIn('<!--ERROR-->', server.LAN_LOGIN_HTML)
        self.assertNotIn('method="get" action="/"', server.LAN_LOGIN_HTML)


class ClipImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_notes = server.NOTES_DIR
        server.NOTES_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(server, "NOTES_DIR", self.old_notes))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE clip_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id INTEGER NOT NULL,
            file TEXT NOT NULL, name TEXT DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(clip_id, file))""")
        self.conn.execute("""CREATE TABLE clip_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id INTEGER NOT NULL,
            text TEXT NOT NULL DEFAULT '', images TEXT NOT NULL DEFAULT '[]')""")
        self.addCleanup(self.conn.close)

    def test_only_bitmap_files_become_primary_images(self):
        (server.NOTES_DIR / "shot.png").write_bytes(b"png")
        (server.NOTES_DIR / "note.txt").write_text("x", encoding="utf-8")
        self.assertEqual(1, len(server.clean_clip_images([
            {"file": "shot.png", "name": "截图.png"},
            {"file": "note.txt", "name": "note.txt"},
        ])))
        self.assertEqual([], server.clean_clip_images([{"file": "note.txt"}]))

    def test_attach_preview_and_purge_primary_images(self):
        (server.NOTES_DIR / "shot.png").write_bytes(b"png")
        self.assertEqual(1, server.attach_clip_images(
            self.conn, 7, [{"file": "shot.png", "name": "截图.png"}]))
        imgs, total = server.clip_images_of(self.conn, 7, 4)
        self.assertEqual(1, total)
        self.assertEqual("/api/notes/img/shot.png", imgs[0]["url"])
        clip = {"id": 7, "preview_images": [], "preview_image_count": 0}
        server.clip_with_images(self.conn, clip, 4)
        self.assertEqual(1, clip["preview_image_count"])
        server.purge_clip_images(self.conn, 7)
        self.assertEqual([], server.clip_images_of(self.conn, 7, 4)[0])
        self.assertFalse((server.NOTES_DIR / "shot.png").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
