#!/usr/bin/env python
"""观澜 · 本轮全面 review 的回归集。

每个用例都对应一条被证实过的 bug，断言的是「修好之后的行为」，
不是「现在的行为」。跑法：

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bible  # noqa: E402
import reading  # noqa: E402
import seed  # noqa: E402


# ══════════════════════════════════════════════════════════
# 夹具：最小可用库
# ══════════════════════════════════════════════════════════

_CLIPS_DDL = """
CREATE TABLE clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '', url TEXT DEFAULT '', channel TEXT DEFAULT '',
    summary TEXT DEFAULT '', excerpt TEXT DEFAULT '', tags TEXT DEFAULT '[]',
    tier TEXT DEFAULT '', grade TEXT DEFAULT '', board_id INTEGER,
    content_text TEXT DEFAULT '', body_images TEXT DEFAULT '',
    fetch_status TEXT DEFAULT 'idle', fetch_error TEXT DEFAULT '',
    archived INTEGER DEFAULT 0, content_hash TEXT DEFAULT '',
    source_type TEXT DEFAULT 'web', file_name TEXT DEFAULT '', file_size INTEGER DEFAULT 0,
    mime_type TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE TABLE clip_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id INTEGER NOT NULL,
    text TEXT DEFAULT '', images TEXT DEFAULT '[]'
);
CREATE TABLE clip_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id INTEGER NOT NULL,
    start_offset INTEGER DEFAULT 0, end_offset INTEGER DEFAULT 0,
    quote TEXT DEFAULT '', note TEXT DEFAULT '', color TEXT DEFAULT ''
);
CREATE TABLE boards (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE claims (id INTEGER PRIMARY KEY, code TEXT, statement TEXT);
CREATE TABLE code_counters (prefix TEXT, year INTEGER, seq INTEGER);
"""


class _ReadingFixture(unittest.TestCase):
    """建一个带 reading_* 表的内存库，并塞一本可读的书。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_CLIPS_DDL)
        self.conn.executescript((ROOT / "reading_schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        self._ctx = reading._CTX
        reading._CTX = dict(self._ctx)
        reading._CTX["ValidationError"] = ValueError   # 测试里不依赖 server
        self.addCleanup(lambda: setattr(reading, "_CTX", self._ctx))

        self.conn.execute(
            "INSERT INTO clips(id,title,summary,excerpt,tags) VALUES (1,'旧书名很长','简介','','')")
        self.conn.execute(
            """INSERT INTO reading_items(clip_id,kind,position_unit,char_count,doc_text,status)
               VALUES (1,'book','char',1200,'正文开始。这是一段用于测试的正文内容。','unread')""")
        self.conn.commit()

    def _book(self):
        return self.conn.execute("SELECT * FROM reading_items WHERE clip_id=1").fetchone()


class ReadingProgressTests(_ReadingFixture):
    def test_update_rejects_non_numeric_duration(self):
        """BUG-3：duration 存成 TEXT 后，这本书再也存不进进度。"""
        r = reading.api_item_update(self.conn, {}, {"duration": "abc"}, 1)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(0.0, self._book()["duration"])
        # 关键：后续进度保存不能被这条脏数据毒死
        p = reading.api_progress(self.conn, {}, {"position": 100}, 1)
        self.assertTrue(p.get("ok"), p)

    def test_update_rejects_non_numeric_progress(self):
        r = reading.api_item_update(self.conn, {}, {"progress": "xyz"}, 1)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(0.0, self._book()["progress"])

    def test_update_clamps_progress_to_range(self):
        """BUG-14：progress 999 会被存成 999，reparse 直接把读者送到书尾。"""
        reading.api_item_update(self.conn, {}, {"progress": 999}, 1)
        self.assertEqual(100.0, self._book()["progress"])
        reading.api_item_update(self.conn, {}, {"progress": -5}, 1)
        self.assertEqual(0.0, self._book()["progress"])

    def test_audio_without_duration_is_not_marked_done(self):
        """BUG-4：有声书时长未知时，1.5 秒播放被算成 100% 读完。"""
        self.conn.execute(
            """INSERT INTO reading_items(clip_id,kind,position_unit,char_count,doc_text,status)
               VALUES (2,'audio','second',0,'','unread')""")
        self.conn.execute("INSERT INTO clips(id,title) VALUES (2,'有声书')")
        self.conn.commit()
        r = reading.api_progress(self.conn, {}, {"position": 1.5, "duration": 0}, 2)
        self.assertTrue(r.get("ok"), r)
        self.assertLess(r["progress"], 100.0)
        self.assertNotEqual("done", r["status"])

    def test_progress_zero_clears_done_status(self):
        """BUG-10：读完后回到开头，状态仍是 done，0% 的书仍算「读完」。"""
        reading.api_progress(self.conn, {}, {"position": 1200}, 1)
        self.assertEqual("done", self._book()["status"])
        back = reading.api_progress(self.conn, {}, {"position": 0}, 1)
        self.assertEqual(0.0, back["progress"])
        self.assertNotEqual("done", self._book()["status"])

    def test_rename_reindexes_fts(self):
        """BUG-6：改书名不重建索引，新名搜不到、旧名还在。"""
        reading.api_item_update(self.conn, {}, {"title": "新书名标题"}, 1)
        rows = self.conn.execute(
            "SELECT title FROM reading_fts WHERE kind='meta' AND clip_id='1'").fetchall()
        self.assertTrue(rows)
        self.assertEqual("新书名标题", rows[0]["title"])


class ReadingSearchTests(_ReadingFixture):
    def test_like_wildcards_are_escaped(self):
        """BUG-5：搜 '%' 返回全部条目——用户输入成了通配符。"""
        self.conn.execute("INSERT INTO clips(id,title) VALUES (2,'100% 的报告')")
        self.conn.execute(
            """INSERT INTO reading_items(clip_id,kind,position_unit,char_count,doc_text,status)
               VALUES (2,'doc','char',50,'正文','unread')""")
        self.conn.commit()
        r = reading.api_items(self.conn, {"q": ["%"]})
        # 修好前：'%' 变成 %%% 命中两条；修好后：只命中标题真含 '%' 的那一条
        self.assertEqual(1, r["total"], "'%' 不该当成通配符命中一切")
        self.assertEqual("100% 的报告", r["items"][0]["clip"]["title"])

    def test_underscore_is_escaped_in_search(self):
        """BUG-5：搜 '_' 命中了不含下划线的笔记——_ 成了单字符通配符。"""
        self.conn.execute(
            """INSERT INTO reading_notes(clip_id,kind,title,content_md,tags,source_quote)
               VALUES (1,'note','axb','x','','y')""")
        self.conn.execute(
            """INSERT INTO reading_notes(clip_id,kind,title,content_md,tags,source_quote)
               VALUES (1,'note','a_b','x','','y')""")
        self.conn.commit()
        r = reading.api_notes(self.conn, {"q": ["_"]})
        # 只有标题里真含下划线的那条该命中
        self.assertEqual(1, len(r["notes"]))
        self.assertEqual("a_b", r["notes"][0]["title"])

    def test_literal_percent_still_matches(self):
        self.conn.execute("INSERT INTO clips(id,title) VALUES (2,'100% 的报告')")
        self.conn.execute(
            """INSERT INTO reading_items(clip_id,kind,position_unit,char_count,doc_text,status)
               VALUES (2,'doc','char',50,'正文','unread')""")
        self.conn.commit()
        r = reading.api_items(self.conn, {"q": ["100%"]})
        self.assertEqual(1, r["total"])


class ReadingShelfTests(_ReadingFixture):
    def test_non_numeric_sort_does_not_crash(self):
        """BUG-9：sort 传 'abc' 直接 500。"""
        r = reading.api_shelf_create(self.conn, {}, {"name": "新书架", "sort": "abc"})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(0, r["shelf"]["sort"])


class ReadingPathParamTests(_ReadingFixture):
    def test_bad_path_param_is_client_error_not_500(self):
        """BUG-8：/api/reading/book/abc 应该 400，不是内部错误 500。"""
        with self.assertRaises(ValueError):
            reading.api_book(self.conn, {}, "abc")
        with self.assertRaises(ValueError):
            reading.api_export(self.conn, {}, "abc")
        with self.assertRaises(ValueError):
            reading.api_progress(self.conn, {}, {"position": 1}, "abc")

    def test_zero_and_negative_are_rejected(self):
        for bad in ("0", "-5"):
            with self.assertRaises(ValueError):
                reading.api_book(self.conn, {}, bad)


class ReadingLinkTests(_ReadingFixture):
    def test_deleting_note_cleans_links(self):
        """BUG-7：reading_links 没有外键，删了笔记关联还留着。"""
        self.conn.execute(
            """INSERT INTO reading_notes(clip_id,kind,title,content_md,tags,source_quote)
               VALUES (1,'note','n','c','','q')""")
        nid = self.conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        self.conn.execute(
            """INSERT INTO reading_links(src_kind,src_id,dst_kind,dst_id,relation,clip_id,note)
               VALUES ('clip','1','note',?,'refs',1,'')
               , ('note',?,'clip','1','refs',1,'')""", (str(nid), str(nid)))
        self.conn.commit()
        reading.api_note_delete(self.conn, {}, {}, nid)
        left = self.conn.execute(
            "SELECT COUNT(*) n FROM reading_links WHERE dst_kind='note' OR src_kind='note'"
        ).fetchone()["n"]
        self.assertEqual(0, left)

    def test_deleting_bookmark_cleans_links(self):
        self.conn.execute(
            """INSERT INTO reading_bookmarks(clip_id,offset,label,note)
               VALUES (1,10,'b','n')""")
        bid = self.conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        self.conn.execute(
            """INSERT INTO reading_links(src_kind,src_id,dst_kind,dst_id,relation,clip_id,note)
               VALUES ('clip','1','bookmark',?,'refs',1,'')""", (str(bid),))
        self.conn.commit()
        reading.api_bookmark_delete(self.conn, {}, {}, bid)
        left = self.conn.execute(
            "SELECT COUNT(*) n FROM reading_links WHERE dst_kind='bookmark'").fetchone()["n"]
        self.assertEqual(0, left)

    def test_link_to_missing_note_is_rejected(self):
        r = reading.api_link_create(self.conn, {}, {
            "src_kind": "clip", "src_id": "1", "dst_kind": "note", "dst_id": "99999"})
        self.assertIn("error", r)


# ══════════════════════════════════════════════════════════
# bible：导入即销毁
# ══════════════════════════════════════════════════════════

class BibleImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "bible_schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        bible.ensure(self.conn)
        self.conn.execute(
            """INSERT INTO bible_verses(book,chapter,verse,text,text_en,version)
               VALUES ('GEN',1,1,'起初 神创造天地','', 'demo')""")
        self.conn.execute(
            """INSERT INTO bible_highlights(book,chapter,verse,start,end,quote,color,note)
               VALUES ('GEN',1,1,0,4,'起初','','')""")
        self.conn.execute(
            """INSERT INTO bible_notes(book,chapter,verse,title,content_md,created_at,updated_at)
               VALUES ('GEN',1,1,'n','c','','')""")
        self.conn.execute(
            """INSERT INTO bible_bookmarks(book,chapter,verse,label,created_at)
               VALUES ('GEN',1,1,'b','')""")
        self.conn.commit()

    def test_invalid_import_does_not_wipe_bible(self):
        """BUG-2：一条都过不了校验，却把整本经文删了还回 ok。"""
        before = self.conn.execute("SELECT COUNT(*) n FROM bible_verses").fetchone()["n"]
        r = bible.api_bible_import(self.conn, {}, {
            "mode": "replace",
            "verses": [{"book": "不存在的书", "chapter": "x", "verse": 1, "text": "t"}],
        })
        self.assertIn("error", r)
        after = self.conn.execute("SELECT COUNT(*) n FROM bible_verses").fetchone()["n"]
        self.assertEqual(before, after, "非法导入不得清空经文")

    def test_replace_clears_annotations(self):
        """BUG-1：覆盖导入留下旧标注（偏移属于旧译本），界面承诺会清空。"""
        bible.api_bible_import(self.conn, {}, {
            "mode": "replace",
            "verses": [{"book": "创世记", "chapter": 1, "verse": 1, "text": "起初 神创造天地"}],
        })
        for table in ("bible_highlights", "bible_notes", "bible_bookmarks"):
            n = self.conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
            self.assertEqual(0, n, f"{table} 未随覆盖导入清空")


# ══════════════════════════════════════════════════════════
# seed：示例数据不得污染真实库
# ══════════════════════════════════════════════════════════

class SeedGuardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)

    def test_empty_library_gets_seeded(self):
        self.assertTrue(seed._library_is_empty(self.conn))
        seed.run(self.conn)
        n = self.conn.execute("SELECT COUNT(*) n FROM claims").fetchone()["n"]
        self.assertEqual(7, n)

    def test_library_with_clips_but_no_claims_is_not_seeded(self):
        """BUG-5：只看 claims 表 → 真实库被灌进 7 条带 T0 的假命题。"""
        self.conn.execute(
            "INSERT INTO clips(title,created_at) VALUES ('我的真实剪藏','2026-01-01')")
        self.conn.execute(
            "INSERT INTO sources(code,title,publisher,url,tier,published,kind)"
            " VALUES ('S-2026-0001','我的真实来源：某券商研报','','','T1','','report')")
        self.conn.commit()
        self.assertFalse(seed._library_is_empty(self.conn))
        seed.run(self.conn)
        n = self.conn.execute("SELECT COUNT(*) n FROM claims").fetchone()["n"]
        self.assertEqual(0, n, "有真实数据的库不应被灌入示例命题")

    def test_seeding_twice_does_not_duplicate_calibers(self):
        """BUG-6：calibers 没有唯一约束，重灌一次就多 4 行。"""
        seed.run(self.conn)
        first = self.conn.execute("SELECT COUNT(*) n FROM calibers").fetchone()["n"]
        self.conn.execute("DELETE FROM claims")
        self.conn.execute("DELETE FROM sources")
        self.conn.execute("DELETE FROM observations")
        self.conn.execute("DELETE FROM series")
        self.conn.execute("DELETE FROM baserates")
        self.conn.execute("DELETE FROM predictions")
        self.conn.execute("DELETE FROM decisions")
        self.conn.commit()
        seed.run(self.conn)
        second = self.conn.execute("SELECT COUNT(*) n FROM calibers").fetchone()["n"]
        self.assertEqual(first, second)


# ══════════════════════════════════════════════════════════
# seed：示例编号不得占用 next_code() 的编号空间
# ══════════════════════════════════════════════════════════

class SeedNumberingTests(unittest.TestCase):
    """示例条目一律 DEMO- 前缀。

    不加前缀时示例编号与真实编号同构，用户第一条命题会从 0008 起编，
    且 [C-2026-0005] 这类引用可能指向示例。这两条断言的就是「不该再发生」。
    """

    def setUp(self):
        import server  # 只为拿 next_code，导入 0.3s 左右
        self.server = server
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)

    def _codes(self, table):
        return [r["code"] for r in self.conn.execute(f"SELECT code FROM {table}")]

    def test_every_seeded_code_carries_demo_prefix(self):
        seed.run(self.conn)
        for table in ("sources", "claims", "predictions", "decisions"):
            codes = self._codes(table)
            self.assertTrue(codes, f"{table} 应有示例条目")
            offenders = [c for c in codes if not c.startswith(seed.DEMO)]
            self.assertEqual([], offenders, f"{table} 里混进了不带 DEMO- 前缀的编号")

    def test_demo_codes_do_not_look_like_generated_codes(self):
        """去掉 DEMO- 之后不能再和 next_code() 的产物同构。"""
        seed.run(self.conn)
        generated = re.compile(r"^[CPQSD]-\d{4}-\d{4}$")
        for table in ("sources", "claims", "predictions", "decisions"):
            for code in self._codes(table):
                self.assertIsNone(generated.match(code), f"{code} 与真实编号同构")

    def test_internal_references_resolve_to_seeded_codes(self):
        """links / depends_on 里的引用必须指向示例自己的编号。"""
        seed.run(self.conn)
        claim_codes = set(self._codes("claims"))
        links = json.loads(
            self.conn.execute("SELECT links FROM claims WHERE code=?",
                              ("DEMO-C-0005",)).fetchone()["links"])
        for target in links.get("contradicts", []):
            self.assertIn(target, claim_codes)
        for table in ("predictions", "decisions"):
            for row in self.conn.execute(f"SELECT code, depends_on FROM {table}"):
                for target in json.loads(row["depends_on"]):
                    self.assertIn(target, claim_codes, f"{row['code']} 引用了不存在的 {target}")

    def test_next_code_ignores_demo_rows(self):
        """灌过示例的库，用户第一条命题仍拿 C-YYYY-0001。"""
        seed.run(self.conn)
        code = self.server.next_code(self.conn, "C", "claims")
        self.assertEqual(f"C-{datetime.now().year}-0001", code)


# ══════════════════════════════════════════════════════════
# seed：存量库里的老示例编号迁移
# ══════════════════════════════════════════════════════════

class LegacyCodeMigrationTests(unittest.TestCase):
    """老库已经用 C-2026-0001 式编号灌过示例，迁移得只动示例、不动用户数据。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)

    def _claim(self, code, statement, question="", links="{}"):
        self.conn.execute(
            """INSERT INTO claims (code,kind,statement,question,domain,tier,confidence,
               as_of,observed_at,falsifier,status,links)
               VALUES (?,?,?,?,'economy','T0',0.9,'2026-01-01','2026-10-31','x','active',?)""",
            (code, "question" if question else "claim", statement, question, links))
        self.conn.commit()

    def test_rows_whose_text_does_not_match_are_left_alone(self):
        """编号撞上了，但内容是用户自己写的 —— 不许改，且要报冲突。"""
        self._claim("C-2026-0001", "2018年1月起国家统计局正式发布全国城镇调查失业率，此前…")
        self._claim("C-2026-0002", "我自己写的一条命题，恰好占了这个号")
        plan = seed.legacy_demo_plan(self.conn)
        self.assertEqual(["C-2026-0001"], [old for _t, old, _n, _i in plan["renames"]])
        self.assertTrue(plan["problems"], "内容不符的一条必须作为冲突报出来")
        self.assertIn("C-2026-0002", " ".join(plan["problems"]))

    def test_migration_renames_codes_and_rewrites_references(self):
        self._claim("C-2026-0005", "2025年中国出生人口约为954万人",
                    links=json.dumps({"contradicts": ["C-2026-0006"]}))
        self._claim("C-2026-0006", "2025年中国出生人口约为820万人")
        self._claim("C-2026-0007", "", question="博主称新口径下16-24岁青年失业率系统性低于旧口径")
        self.conn.execute(
            """INSERT INTO predictions (code,proposition,probability,criteria,resolve_by,depends_on)
               VALUES ('P-2026-0001','2027年底前房价回升','0.35','口径按70城指数','2027-12-31',?)""",
            (json.dumps(["C-2026-0005"]),))
        self.conn.commit()

        plan = seed.legacy_demo_plan(self.conn)
        self.assertEqual(4, len(plan["renames"]))
        self.assertEqual([], plan["problems"])
        seed.apply_legacy_demo_codes(self.conn, plan)

        codes = {r["code"] for r in self.conn.execute("SELECT code FROM claims")}
        self.assertEqual({"DEMO-C-0005", "DEMO-C-0006", "DEMO-Q-0001"}, codes)
        links = json.loads(self.conn.execute(
            "SELECT links FROM claims WHERE code='DEMO-C-0005'").fetchone()["links"])
        self.assertEqual(["DEMO-C-0006"], links["contradicts"])
        depends = json.loads(self.conn.execute(
            "SELECT depends_on FROM predictions WHERE code='DEMO-P-0001'").
            fetchone()["depends_on"])
        self.assertEqual(["DEMO-C-0005"], depends)
        self.assertEqual([], seed.legacy_demo_plan(self.conn)["renames"], "迁移必须可重复执行")


# ══════════════════════════════════════════════════════════
# 引用识别：DEMO- 编号没有年份段，正则不能只顾一种形状
# ══════════════════════════════════════════════════════════

class CitationPatternTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def test_both_code_shapes_are_recognized(self):
        cites = self.server.extract_citations(
            "甲。[DEMO-C-0005] 乙。[C-2026-0011] 丙。[Q-2026-0002]")
        self.assertEqual({"DEMO-C-0005", "C-2026-0011", "Q-2026-0002"}, cites)

    def test_demo_citation_at_sentence_end_moves_before_punctuation(self):
        text = self.server.normalize_citation_tail("出生人口口径有冲突。[DEMO-C-0005]")
        self.assertEqual("出生人口口径有冲突[DEMO-C-0005]。", text)

    def test_malformed_codes_are_not_citations(self):
        self.assertEqual(set(), self.server.extract_citations("看 [DEMO-X-0001] 和 [C-26-001]"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
