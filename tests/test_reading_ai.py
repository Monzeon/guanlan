#!/usr/bin/env python
"""阅读 AI 离线回归：内存库 + 模型桩，不访问真实云端或用户资料。"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import reading_ai

ROOT = Path(__file__).resolve().parents[1]


class ReadingAITests(unittest.TestCase):
    def setUp(self):
        reading_ai.CACHE.clear()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE clips(id INTEGER PRIMARY KEY,title TEXT);
            CREATE TABLE reading_items (
                clip_id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'book', format TEXT DEFAULT '',
                shelf_id INTEGER, doc_text TEXT NOT NULL DEFAULT '', chapters TEXT NOT NULL DEFAULT '[]',
                char_count INTEGER DEFAULT 0, status TEXT DEFAULT 'unread', progress REAL DEFAULT 0,
                position REAL DEFAULT 0, position_unit TEXT DEFAULT 'char', duration REAL DEFAULT 0,
                read_seconds INTEGER DEFAULT 0, last_read_at TEXT DEFAULT '', added_at TEXT DEFAULT '', meta TEXT DEFAULT '{}');
        """)
        self.conn.executescript((ROOT / "reading_schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)
        self.chapters = [
            {"id":"a","title":"第一章","start":0,"end":12000},
            {"id":"b","title":"第二章","start":12000,"end":12200},
        ]
        self.doc = "第一章开头。" + ("城市发展与公共交通的关系需要结合具体证据分析。" * 340) + "第一章中段。" + ("人口流动与空间布局是另一项重要因素。" * 340) + "第一章结尾。"
        self.doc = self.doc[:12000] + "第二章出现独特词：潮汐政策。"
        self.conn.execute("INSERT INTO clips VALUES(1,'测试书')")
        self.conn.execute("INSERT INTO reading_items(clip_id,doc_text,chapters) VALUES(1,?,?)",
                          (self.doc, __import__('json').dumps(self.chapters,ensure_ascii=False)))
        self.conn.execute("INSERT INTO reading_fts(kind,clip_id,ref_id,offset,title,body) VALUES('chapter','1','a',0,'第一章',?)",(self.doc[:12000],))
        self.conn.execute("INSERT INTO reading_fts(kind,clip_id,ref_id,offset,title,body) VALUES('chapter','1','b',12000,'第二章','第二章出现独特词：潮汐政策。')")
        self.calls=[]
        def model(messages,max_tokens=4000):
            self.calls.append(messages)
            return "基于资料的回答[E1]"
        self.model=model
        self.config=lambda:{"base_url":"test","key":"test","model":"offline","style":"wb","config_error":""}

    def run_ai(self,**kw):
        body={"clip_id":1,"chapter_id":"a","action":"summary",**kw}
        return reading_ai.handle(self.conn,body,ai_chat=self.model,ai_config=self.config)

    def test_long_chapter_is_explicitly_sampled_and_has_real_anchors(self):
        result=self.run_ai()
        self.assertTrue(result["ok"],result)
        self.assertTrue(result["context"]["truncated"])
        self.assertEqual(result["context"]["chapter_chars"],12000)
        self.assertEqual(len(result["sources"]),3)
        for source in result["sources"]:
            start=source["offset"]-self.chapters[0]["start"] if source["chapter_id"]=="a" else source["offset"]
            self.assertEqual(self.doc[source["offset"]:source["offset"]+len(source["quote"])],source["quote"])
        self.assertEqual(len(self.calls),1)

    def test_fake_citation_is_rejected(self):
        result=self.run_ai()
        # 通过替换模型桩验证编号不被接受
        reading_ai.CACHE.clear()
        bad=reading_ai.handle(self.conn,{"clip_id":1,"chapter_id":"a","action":"summary"},
                             ai_chat=lambda *a,**k:"伪造[E99]",ai_config=self.config)
        self.assertIn("无法核验",bad["error"])

    def test_empty_text_does_not_call_model(self):
        self.conn.execute("UPDATE reading_items SET doc_text=''")
        before=len(self.calls)
        result=self.run_ai()
        self.assertIn("没有可读取",result["error"])
        self.assertEqual(len(self.calls),before)

    def test_book_scope_finds_other_chapter(self):
        result=self.run_ai(action="chat",scope="book",question="潮汐政策",selection="")
        self.assertTrue(result["ok"],result)
        self.assertEqual(result["context"]["scope"],"book")
        self.assertTrue(any(s["chapter_id"]=="b" for s in result["sources"]))

    def test_polish_uses_note_only_and_cache_does_not_change_note(self):
        result=self.run_ai(action="polish",content_md="我的原始笔记")
        self.assertTrue(result["ok"],result)
        user=self.calls[-1][1]["content"]
        self.assertIn("我的原始笔记",user)
        self.assertNotIn("城市发展",user)
        cached=self.run_ai(action="polish",content_md="我的原始笔记")
        self.assertTrue(cached["cached"])
        self.assertEqual(len(self.calls),1)

    def test_history_is_bounded(self):
        history=[{"question":f"q{i}","answer":f"a{i}"} for i in range(20)]
        result=self.run_ai(action="chat",question="继续",history=history)
        self.assertEqual(result["context"]["history_turns"],6)

    def test_outline_is_layered_and_limited(self):
        result=self.run_ai(action="outline")
        self.assertTrue(result["ok"],result)
        self.assertEqual(result["context"]["covered_chapters"],2)
        self.assertEqual(result["context"]["total_chapters"],2)
        self.assertGreaterEqual(len(self.calls),3)

    def test_summary_cache_reuses_and_invalidates_on_source_change(self):
        first=self.run_ai()
        second=self.run_ai()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.calls),1)
        self.chapters[0]["end"]=12001
        self.doc += "。"
        self.conn.execute("UPDATE reading_items SET doc_text=?,chapters=?",
                          (self.doc,__import__('json').dumps(self.chapters,ensure_ascii=False)))
        third=self.run_ai()
        self.assertFalse(third["cached"])
        self.assertEqual(len(self.calls),2)

    def test_usage_is_optional_and_passed_through(self):
        result=reading_ai.handle(self.conn,{"clip_id":1,"chapter_id":"a","action":"summary"},
                                ai_chat=self.model,ai_config=self.config,
                                ai_usage=lambda:{"total_tokens":42})
        self.assertEqual(result["usage"]["total_tokens"],42)

    def test_sse_usage_only_collects_numeric_token_counts(self):
        import server
        rows=[b'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"secret":"ignored"}}\n',
              b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',b'data: [DONE]\n']
        text,_=server._read_sse(rows)
        self.assertEqual(text,"ok")
        self.assertEqual(server.AI_LAST_USAGE,{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15})


if __name__ == "__main__":
    unittest.main()
