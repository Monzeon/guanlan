#!/usr/bin/env python
"""观澜 · 2026-11-12 这轮 review 的回归集。

每条都对应一个已复现的 bug，断言的是「修好之后的行为」。
跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bible  # noqa: E402
import pdf_text  # noqa: E402
import reading  # noqa: E402


class BiblePartialImportTests(unittest.TestCase):
    """覆盖导入：只挡坏文件，别把整本经文清掉；merge 别抹掉已有英文。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "bible_schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        bible.ensure(self.conn)
        # _resolve_book 归一成小写 id（'gen'），夹具必须用同一个 bid 才能命中 upsert
        self.bid = bible._resolve_book("创世记")
        self.conn.executemany(
            """INSERT OR REPLACE INTO bible_verses(book,chapter,verse,text,text_en,version)
               VALUES (?,1,?,'中文','','demo')""",
            [(self.bid, v) for v in range(1, 11)])
        self.conn.execute(
            """UPDATE bible_verses SET text_en='In the beginning'
               WHERE book=? AND chapter=1 AND verse=1""", (self.bid,))
        self.conn.commit()

    def _import(self, verses, mode="replace"):
        return bible.api_bible_import(self.conn, {}, {"mode": mode, "verses": verses})

    def test_partially_valid_replace_does_not_wipe_bible(self):
        """BUG：31 行里只有 1 行合法，DELETE 照样执行，31101 节经文全没。"""
        before = self.conn.execute("SELECT COUNT(*) n FROM bible_verses").fetchone()["n"]
        bad = [{"book": "不存在的书", "chapter": 1, "verse": 1, "text": "t"}
               for _ in range(9)]
        r = self._import(bad + [{"book": "创世记", "chapter": 1, "verse": 1,
                                 "text": "起初 神创造天地"}], "replace")
        self.assertIn("error", r, "有跳过且合法数远少于现有，必须拒绝并保留")
        self.assertEqual(before, self.conn.execute(
            "SELECT COUNT(*) n FROM bible_verses").fetchone()["n"])

    def test_full_replace_still_works(self):
        """守卫不能把正常的完整覆盖挡掉（skipped=0 一律放行）。"""
        verses = [{"book": "创世记", "chapter": 1, "verse": v, "text": f"新译本 {v}"}
                  for v in range(1, 11)]
        r = self._import(verses, "replace")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(10, self.conn.execute(
            "SELECT COUNT(*) n FROM bible_verses").fetchone()["n"])
        self.assertEqual("新译本 1", self.conn.execute(
            "SELECT text FROM bible_verses WHERE book=? AND chapter=1 AND verse=1",
            (self.bid,)).fetchone()["text"])

    def test_merge_without_english_keeps_existing_text_en(self):
        """BUG：merge 一条只带中文的经文，会把已有的 text_en 清成空串。"""
        r = self._import([{"book": "创世记", "chapter": 1, "verse": 1,
                            "text": "起初"}], "merge")
        self.assertTrue(r.get("ok"), r)
        row = self.conn.execute(
            "SELECT text,text_en FROM bible_verses "
            "WHERE book=? AND chapter=1 AND verse=1", (self.bid,)).fetchone()
        self.assertEqual("起初", row["text"])
        self.assertEqual("In the beginning", row["text_en"])

    def test_merge_with_english_still_updates(self):
        r = self._import([{"book": "创世记", "chapter": 1, "verse": 1,
                            "text": "起初", "text_en": "Beginning"}], "merge")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual("Beginning", self.conn.execute(
            "SELECT text_en FROM bible_verses "
            "WHERE book=? AND chapter=1 AND verse=1", (self.bid,)).fetchone()["text_en"])


class ProgressDurationOnlyTests(unittest.TestCase):
    """只上报时长不该把已读进度清零。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE clips (id INTEGER PRIMARY KEY, title TEXT DEFAULT '');
            CREATE TABLE clip_annotations (
                id INTEGER PRIMARY KEY, clip_id INTEGER, start_offset INTEGER DEFAULT 0,
                end_offset INTEGER DEFAULT 0, quote TEXT DEFAULT '', note TEXT DEFAULT '',
                color TEXT DEFAULT '');
        """)
        self.conn.executescript((ROOT / "reading_schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        self._ctx = reading._CTX
        reading._CTX = dict(self._ctx)
        reading._CTX["ValidationError"] = ValueError
        self.addCleanup(lambda: setattr(reading, "_CTX", self._ctx))
        self.conn.execute("INSERT INTO clips(id,title) VALUES (1,'书')")
        self.conn.execute(
            """INSERT INTO reading_items(clip_id,kind,position_unit,char_count,doc_text,
               status,position,progress,duration)
               VALUES (1,'audio','second',0,'','done',1800,75.0,0)""")
        self.conn.commit()

    def test_duration_only_keeps_progress(self):
        r = reading.api_progress(self.conn, {}, {"duration": 3600}, 1)
        self.assertTrue(r.get("ok"), r)
        row = self.conn.execute(
            "SELECT position,progress,status,duration FROM reading_items WHERE clip_id=1"
        ).fetchone()
        self.assertEqual(75.0, row["progress"])
        self.assertEqual("done", row["status"])
        self.assertEqual(1800, row["position"])
        self.assertEqual(3600.0, row["duration"])

    def test_real_progress_report_still_counts(self):
        r = reading.api_progress(self.conn, {}, {"position": 0, "seconds": 30}, 1)
        self.assertTrue(r.get("ok"), r)
        row = self.conn.execute(
            "SELECT progress,status FROM reading_items WHERE clip_id=1").fetchone()
        self.assertEqual(0.0, row["progress"])
        self.assertEqual("unread", row["status"])


class PdfTextFallbackPageLabelTests(unittest.TestCase):
    """兜底页没学到页眉时，显式页码行也要删。"""

    def test_explicit_page_label_removed_without_repeat(self):
        out = pdf_text._clean_plain("第一章\nPage 3\n正文内容\n", set())
        self.assertNotIn("Page 3", out)
        self.assertIn("正文内容", out)

    def test_repeat_lines_still_removed(self):
        out = pdf_text._clean_plain("观澜 | 3\n正文内容\n",
                                    {pdf_text._norm_furniture("观澜 | 3")})
        self.assertNotIn("观澜 | 3", out)
        self.assertIn("正文内容", out)


_SELF_HEAL_CODE = r'''
import os, sys
sys.path.insert(0, r"{root}")
import server
server.init_db()               # 先把 schema 铺好（子进程是全新空库）
c = server.db()
c.execute("INSERT INTO clips(title,source_type,fetch_status,content_text,updated_at) "
          "VALUES ('大PDF','file','extracting','',datetime('now','localtime'))")
c.commit(); c.close()
server.init_db()
c = server.db()
row = c.execute("SELECT fetch_status,fetch_error FROM clips WHERE title='大PDF'").fetchone()
c.close()
print("STATUS=" + row["fetch_status"])
print("HAS_HINT=" + ("1" if row["fetch_error"] else "0"))
'''.replace("{root}", str(ROOT))


class ExtractingSelfHealTests(unittest.TestCase):
    """服务重启后，卡在 extracting 的条目必须自愈并给回重试入口。

    用独立子进程 + 独立 GL_DATA_DIR，绝不碰用户真实库。
    """

    def test_stuck_extracting_is_healed_on_restart(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update({"GL_HOME": d, "GL_DATA_DIR": os.path.join(d, "data"),
                        "GL_CACHE_DIR": os.path.join(d, "cache"),
                        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            for k in ("GL_LAN", "GL_BIND", "PORT", "GL_SEED"):
                env.pop(k, None)
            r = subprocess.run([sys.executable, "-c", _SELF_HEAL_CODE],
                               cwd=str(ROOT), env=env, capture_output=True,
                               text=True, timeout=90)
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertIn("STATUS=failed", r.stdout)
            self.assertIn("HAS_HINT=1", r.stdout)


class MigrationPathTests(unittest.TestCase):
    """搬到 D:\\guanlan 之后不能还有写死的旧机器绝对路径。"""

    def test_make_url_has_no_hardcoded_e_drive(self):
        text = (ROOT / "tools" / "make_url.py").read_text(encoding="utf-8")
        self.assertNotIn("E:/WORKbuddy", text)
        self.assertNotIn("2026-10-31-20-29-37", text)
        self.assertIn("Path(__file__)", text)

    def test_handler_covers_client_disconnect(self):
        text = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("def handle_one_request", text)
        self.assertIn("ConnectionError", text)


if __name__ == "__main__":
    unittest.main()
