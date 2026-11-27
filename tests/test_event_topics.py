#!/usr/bin/env python
"""事件专题：临时/内存库回归，不读取真实资料库。"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import event_topics


ROOT = Path(__file__).resolve().parents[1]


class EventTopicTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE clips(id INTEGER PRIMARY KEY,title TEXT NOT NULL,
                source_type TEXT DEFAULT 'text',file_name TEXT DEFAULT '',
                url TEXT DEFAULT '',created_at TEXT DEFAULT '');
            CREATE TABLE events(id INTEGER PRIMARY KEY,code TEXT UNIQUE,
                what TEXT NOT NULL,when_at TEXT NOT NULL);
            INSERT INTO events VALUES (1,'E-OLD','旧时间锚点','2026-01-01');
        """)
        event_topics._READY = False
        self.addCleanup(setattr, event_topics, "_READY", event_topics._READY)
        self.old_ctx = event_topics._CTX
        event_topics._CTX = {"ValidationError": ValueError}
        self.addCleanup(setattr, event_topics, "_CTX", self.old_ctx)
        event_topics.ensure(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.executemany("INSERT INTO clips(id,title,created_at) VALUES (?,?,?)",
                              [(1, "资料甲", "2026-01-01"), (2, "资料乙", "2026-01-02")])
        created = event_topics.api_create(self.conn, {}, {
            "title": "测试事件", "purpose": "验证完整闭环", "tags": ["测试"]})
        self.tid = created["id"]

    def test_legacy_events_are_not_touched(self):
        row = self.conn.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["what"], "旧时间锚点")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='event_topics'").fetchone())

    def test_complete_update_and_link_flow(self):
        update = event_topics.api_update_create(self.conn, {}, {
            "happened_at": "2026-11-18T09:30", "body": "第一条进展"}, self.tid)
        self.assertTrue(update["ok"])
        link = event_topics.api_link_create(self.conn, {}, {"clip_id": 1, "note": "依据"}, self.tid)
        self.assertTrue(link["ok"])
        detail = event_topics.api_one(self.conn, {}, str(self.tid))
        self.assertEqual(detail["updates"][0]["body"], "第一条进展")
        self.assertEqual(detail["links"][0]["title"], "资料甲")
        self.assertEqual(event_topics.api_for_clip(self.conn, {}, "1")["links"][0]["id"], self.tid)
        edited = event_topics.api_update_edit(self.conn, {}, {
            "body": "修订后的进展", "happened_at": "2026-11-18"}, update["id"])
        self.assertTrue(edited["ok"])
        self.assertEqual(event_topics.api_one(self.conn, {}, str(self.tid))["updates"][0]["body"],
                         "修订后的进展")

    def test_topic_delete_preserves_clips(self):
        event_topics.api_link_create(self.conn, {}, {"clip_id": 1}, self.tid)
        self.assertTrue(event_topics.api_delete(self.conn, {}, {}, str(self.tid))["ok"])
        self.assertEqual(self.conn.execute("SELECT title FROM clips WHERE id=1").fetchone()[0],
                         "资料甲")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_updates").fetchone()[0], 0)

    def test_clip_delete_only_removes_link(self):
        event_topics.api_link_create(self.conn, {}, {"clip_id": 1}, self.tid)
        event_topics.purge_clip(self.conn, 1)
        self.conn.execute("DELETE FROM clips WHERE id=1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_clip_links").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT title FROM event_topics").fetchone()[0], "测试事件")

    def test_invalid_input_is_rejected_without_partial_creation(self):
        before = self.conn.execute("SELECT COUNT(*) FROM event_topics").fetchone()[0]
        with self.assertRaises(ValueError):
            event_topics.api_create(self.conn, {}, {"title": ""})
        with self.assertRaises(ValueError):
            event_topics.api_update_create(self.conn, {}, {
                "happened_at": "2026-02-30", "body": "坏日期"}, self.tid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_topics").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_updates").fetchone()[0], 0)

    def test_list_search_and_empty_filter(self):
        event_topics.api_create(self.conn, {}, {"title": "另一个专题", "tags": ["人口"]})
        result = event_topics.api_list(self.conn, {"q": ["人口"], "status": ["all"]})
        self.assertEqual([r["title"] for r in result["topics"]], ["另一个专题"])
        self.assertEqual(event_topics.api_list(self.conn, {"status": ["archived"]})["topics"], [])

    def test_list_query_count_does_not_grow_with_topics(self):
        for i in range(30):
            event_topics.api_create(self.conn, {}, {"title": f"事件{i}"})
        statements = []
        self.conn.set_trace_callback(statements.append)
        result = event_topics.api_list(self.conn, {"status": ["all"]})
        self.conn.set_trace_callback(None)
        self.assertEqual(len(result["topics"]), 31)
        self.assertLessEqual(len(statements), 2)

    def test_export_contains_all_event_records(self):
        event_topics.api_update_create(self.conn, {}, {
            "happened_at": "2026-11-18", "body": "进展"}, self.tid)
        event_topics.api_link_create(self.conn, {}, {"clip_id": 1}, self.tid)
        exported = event_topics.api_export(self.conn, {})
        self.assertEqual(len(exported["topics"]), 1)
        self.assertEqual(len(exported["updates"]), 1)
        self.assertEqual(len(exported["links"]), 1)


if __name__ == "__main__":
    unittest.main()
