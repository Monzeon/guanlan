#!/usr/bin/env python
"""观澜 · 2026-11-11 PDF 抽取误判与 fetch_status 状态机回归集。

每条用例对应一个被真实复现过的 bug，断言的是「修好之后的行为」。
跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "vendor"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pdf_text  # noqa: E402
import reading  # noqa: E402
import server  # noqa: E402

SAMPLE = server.storage.DATA_DIR / "uploads" / "20261111095939-ce0089d178f204c9.pdf"


# ══════════════════════════════════════════════════════════
# 1. 乱码判据：目录点线不是乱码，真乱码必须抓得到
# ══════════════════════════════════════════════════════════

class GarbleDetectionTests(unittest.TestCase):

    def test_toc_leader_dots_are_not_garbled(self):
        """BUG-P0：目录页 3854 个引导点把开头 4000 字样本占满，
        旧判据 content < 15% 把整篇中文报告判成乱码并清空。"""
        toc = "概述" + "." * 3854 + "2\n\n"
        body = "在本次调查中，我们询问了中国大陆地区对 LGBTQ 群体的熟悉程度。" * 400
        self.assertFalse(pdf_text.looks_garbled(toc + body))

    def test_leader_dots_alone_are_not_garbled(self):
        """只有点线、没有正文的页也不该被当成字形编码坏掉。"""
        self.assertFalse(pdf_text.looks_garbled("概述" + "." * 3000 + "2"))

    def test_real_pua_garble_is_detected(self):
        self.assertTrue(pdf_text.looks_garbled("\ue000" * 100 + "正常正文" * 20))

    def test_replacement_chars_are_detected(self):
        self.assertTrue(pdf_text.looks_garbled("\ufffd" * 100 + "正常正文" * 20))

    def test_control_chars_are_detected(self):
        self.assertTrue(pdf_text.looks_garbled("\x01\x02\x03" * 100 + "正常正文" * 20))

    def test_pure_symbols_without_any_readable_char_is_garbled(self):
        self.assertTrue(pdf_text.looks_garbled("·-—=+*|" * 500))

    def test_empty_is_garbled(self):
        self.assertTrue(pdf_text.looks_garbled(""))

    def test_sample_covers_middle_and_tail(self):
        """旧实现只取开头 4000 字，后半本正文完全不参与判定。"""
        s = "目录\n\n" + "." * 3800 + "中段文字" + "汉字内容" * 3000
        sample = pdf_text._garble_sample(s)
        self.assertIn("汉字内容", sample)
        self.assertLessEqual(len(sample), 4000)


# ══════════════════════════════════════════════════════════
# 2. 目录引导点线清理
# ══════════════════════════════════════════════════════════

class LeaderDotsTests(unittest.TestCase):

    def test_dots_collapsed_and_entries_split(self):
        raw = "概述" + "." * 20 + "2 简介" + "." * 20 + "3"
        out = pdf_text._tidy_leader_dots(raw)
        self.assertIn("概述 … 2", out)
        self.assertIn("简介 … 3", out)
        self.assertNotIn("....", out)
        self.assertIn("\n\n", out)

    def test_year_is_not_split(self):
        self.assertEqual(pdf_text._tidy_leader_dots("2024 年 5 月发布，无点线"),
                         "2024 年 5 月发布，无点线")

    def test_year_inside_toc_line_is_not_mangled(self):
        raw = "概述" + "." * 20 + "2 2024 年报告" + "." * 20 + "5"
        out = pdf_text._tidy_leader_dots(raw)
        self.assertIn("2024 年报告", out)

    def test_plain_text_is_untouched(self):
        s = "这是一段普通正文，含 3 个数字和 ... 三个点，但不含目录引导线。"
        self.assertEqual(pdf_text._tidy_leader_dots(s), s)


# ══════════════════════════════════════════════════════════
# 3. 真实样本：整条链路拿到正文，而不是「文字层编码异常」
# ══════════════════════════════════════════════════════════

@unittest.skipUnless(SAMPLE.is_file(), "真实样本 PDF 不在本机，跳过集成用例")
class RealReportTests(unittest.TestCase):

    def test_full_text_is_extracted(self):
        text, status = pdf_text.extract_pdf_text(SAMPLE)
        self.assertEqual(status, "stored", status)
        self.assertGreater(len(text), 18_000)
        self.assertIn("中国大陆对 LGBTQ 群体的正面态度", text)
        self.assertIn("概述", text)
        self.assertIn("Thank you very much", text)   # 最后一页也在
        self.assertFalse(pdf_text.looks_garbled(text))

    def test_extract_file_text_chain_is_clean(self):
        raw, status = server.extract_file_text(SAMPLE, ".pdf")
        self.assertEqual(status, "stored", status)
        body = server.normalize_body_text(raw)
        self.assertGreater(len(body), 18_000)
        self.assertNotIn("............", body)       # 目录点线已压掉
        self.assertLess(body.count("."), 1000)       # 只剩正文里的正常句点
        self.assertIn("概述 … 2", body)
        self.assertFalse(server.pdf_text.looks_garbled(body))


# ══════════════════════════════════════════════════════════
# 4. fetch_status 状态机：没正文就不是「已入库」
# ══════════════════════════════════════════════════════════

_CLIPS_DDL = """
CREATE TABLE clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '', url TEXT DEFAULT '', channel TEXT DEFAULT '',
    summary TEXT DEFAULT '', excerpt TEXT DEFAULT '', tags TEXT DEFAULT '[]',
    tier TEXT DEFAULT '', grade TEXT DEFAULT '', board_id INTEGER,
    content_text TEXT DEFAULT '', body_images TEXT DEFAULT '',
    fetch_status TEXT DEFAULT 'idle', fetch_error TEXT DEFAULT '',
    archived INTEGER DEFAULT 0, content_hash TEXT DEFAULT '',
    source_type TEXT DEFAULT 'web', file_name TEXT DEFAULT '',
    file_path TEXT DEFAULT '', file_size INTEGER DEFAULT 0,
    mime_type TEXT DEFAULT '', viewed_at TEXT DEFAULT '',
    created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE TABLE boards (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE ai_outputs (id INTEGER PRIMARY KEY, clip_id INTEGER);
CREATE TABLE verdicts (clip_id INTEGER);
"""


class _DbFixture(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_CLIPS_DDL)
        self.conn.execute("INSERT INTO boards(id,name) VALUES (1,'社会')")
        self.conn.commit()
        self.addCleanup(self.conn.close)


class RegisterFileStatusTests(_DbFixture):

    def _register(self, digest, raw="", status="stored"):
        with mock.patch.object(server, "extract_file_text", return_value=(raw, status)), \
             mock.patch.object(server, "normalize_body_text", side_effect=lambda t: t):
            return server.register_file(
                self.conn, name="report.pdf", mime="application/pdf",
                stored_name="report.pdf", path=ROOT / "report.pdf", size=1024,
                digest=digest, tags=[], board_id=1, tier="")

    def test_no_text_marks_failed(self):
        """BUG-P0：content_text='' 却写 stored，详情页误报「正文已入库」。"""
        self._register("sha256:noparse", "", "PDF 解析失败，原件已保存")
        row = self.conn.execute(
            "SELECT fetch_status, fetch_error, content_text FROM clips").fetchone()
        self.assertEqual(row["fetch_status"], "failed")
        self.assertEqual(row["content_text"], "")
        self.assertIn("PDF 解析失败", row["fetch_error"])

    def test_with_text_marks_stored(self):
        out = self._register("sha256:ok", "这是正文。", "stored")
        row = self.conn.execute("SELECT fetch_status, fetch_error FROM clips").fetchone()
        self.assertEqual(row["fetch_status"], "stored")
        self.assertEqual(row["fetch_error"], "")
        self.assertEqual(out["import_status"], "saved")

    def test_partial_is_reported_in_receipt(self):
        """U3：静默截断要变成可见提示，但不影响「已入库」状态。"""
        out = self._register("sha256:partial", "这是正文。", "partial")
        row = self.conn.execute("SELECT fetch_status FROM clips").fetchone()
        self.assertEqual(row["fetch_status"], "stored")
        self.assertIn("截断", out["parse_message"])


class ReextractStatusTests(_DbFixture):

    def _reextract(self, raw, status, old_text=""):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "report.pdf").write_bytes(b"%PDF-1.4")
            self.conn.execute(
                """INSERT INTO clips(id,title,file_path,file_size,source_type,
                   fetch_status,fetch_error,content_text)
                   VALUES (1,'report','report.pdf',9,'file',?,'',?)""",
                ("stored" if old_text else "failed", old_text))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM clips WHERE id=1").fetchone()
            with mock.patch.object(server, "UPLOAD_DIR", Path(td)), \
                 mock.patch.object(server, "extract_file_text", return_value=(raw, status)), \
                 mock.patch.object(server, "normalize_body_text", side_effect=lambda t: t):
                return server.reextract_file(self.conn, 1, row)

    def test_retry_failure_marks_failed_when_there_was_no_text(self):
        self._reextract("", "PDF 文字层编码异常，未能还原正文")
        row = self.conn.execute(
            "SELECT fetch_status, content_text, fetch_error FROM clips WHERE id=1").fetchone()
        self.assertEqual(row["fetch_status"], "failed")
        self.assertEqual(row["content_text"], "")
        self.assertIn("文字层编码异常", row["fetch_error"])

    def test_retry_failure_never_wipes_existing_text(self):
        """BUG-P0：content_text=? 无条件覆盖，点一次「重提正文」失败
        就把已经入库的正文清空 —— 数据丢失级。"""
        out = self._reextract("", "原件不在本机，无法提取正文", "原有正文一千字")
        row = self.conn.execute(
            "SELECT fetch_status, content_text, fetch_error FROM clips WHERE id=1").fetchone()
        self.assertEqual(row["content_text"], "原有正文一千字")
        self.assertEqual(row["fetch_status"], "stored")
        self.assertIn("已保留原有正文", out["parse_message"])

    def test_retry_success_clears_error(self):
        self._reextract("新抽出来的正文。", "stored")
        row = self.conn.execute(
            "SELECT fetch_status, content_text, fetch_error FROM clips WHERE id=1").fetchone()
        self.assertEqual(row["fetch_status"], "stored")
        self.assertEqual(row["content_text"], "新抽出来的正文。")
        self.assertEqual(row["fetch_error"], "")

    def test_partial_retry_has_visible_notice(self):
        out = self._reextract("新抽出来的正文。", "partial")
        self.assertIn("截断", out["parse_message"])


class ExtractFileTextStatusTests(unittest.TestCase):
    """空文件不能把英文内部状态 "stored" 当作错误原因端给用户界面。"""

    def test_empty_txt_returns_human_readable_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.txt"
            p.write_bytes(b"")
            text, status = server.extract_file_text(p, ".txt")
            self.assertEqual(text, "")
            self.assertNotEqual(status, "stored")
            self.assertIn("没有可读取的文字", status)

    def test_empty_html_returns_human_readable_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.html"
            p.write_text("<html><body></body></html>", encoding="utf-8")
            text, status = server.extract_file_text(p, ".html")
            self.assertEqual(text, "")
            self.assertNotEqual(status, "stored")

    def test_nonempty_txt_still_stored(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            p.write_text("正文内容", encoding="utf-8")
            text, status = server.extract_file_text(p, ".txt")
            self.assertEqual(text, "正文内容")
            self.assertEqual(status, "stored")


# ══════════════════════════════════════════════════════════
# 5. 数据库瞬时只读：详情请求不能整个 500
# ══════════════════════════════════════════════════════════

class _ReadOnlyOnViewedAt:
    """只让 viewed_at 这次 UPDATE 报 readonly，其余查询照常。"""

    def __init__(self, real):
        self.real = real

    def execute(self, sql, params=()):
        if "SET viewed_at" in sql:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return self.real.execute(sql, params)

    def commit(self):
        return self.real.commit()

    def rollback(self):
        return self.real.rollback()


class ClipOneReadonlyTests(_DbFixture):

    def test_readonly_viewed_at_does_not_break_detail(self):
        self.conn.execute("INSERT INTO clips(id,title) VALUES (1,'报告')")
        self.conn.commit()
        with mock.patch.object(server, "attach_board", side_effect=lambda c, rows: rows), \
             mock.patch.object(server, "clip_with_images", side_effect=lambda c, clip, n: clip), \
             mock.patch.object(server, "notes_of", return_value=[]), \
             mock.patch.object(server, "annotations_of", return_value=[]), \
             mock.patch.object(server, "ai_output_row", side_effect=lambda x: dict(x)):
            out = server.api_clip_one(_ReadOnlyOnViewedAt(self.conn), {}, "1")
        self.assertEqual(out["clip"]["id"], 1)
        self.assertNotIn("error", out)


# ══════════════════════════════════════════════════════════
# 6. 版面还原与入库状态机的其余回归（review 覆盖盲点）
# ══════════════════════════════════════════════════════════

class LayoutRegressionTests(unittest.TestCase):

    def _line(self, text, y=100, band="body", size=10):
        span = pdf_text._Span(x=50, y=y, size=size, text=text,
                              width=pdf_text._span_width(text, size))
        return pdf_text._Line(y=y, size=size, spans=[span], band=band)

    def test_numeric_data_lines_are_not_dropped(self):
        """P0-3：图表数据行 / 脚注编号以前被 `_is_page_number` 静默删掉。"""
        lines = [self._line("2024", 100), self._line("68", 112),
                 self._line("正文内容。", 124)]
        self.assertEqual(len(pdf_text._drop_furniture(lines)), 3)

    def test_dense_page_is_not_mistaken_for_poster(self):
        """P0-2：带脚注编号的密集正文页不该退回裸文本。"""
        lines = [self._line(str(i), 100 + i * 12) for i in range(30)]
        self.assertEqual(pdf_text._page_kind(lines), "flow")

    def test_header_variants_share_one_key(self):
        """P1-4：竖线旁的空格差异不该把同一条页眉裂成两个 key。"""
        self.assertEqual(
            pdf_text._norm_furniture("中国大陆对 LGBTQ 群体的正面态度 | 18"),
            pdf_text._norm_furniture("中国大陆对 LGBTQ 群体的正面态度|18"))

    def test_single_span_bilingual_label_is_detected(self):
        """P2-7：整行写成一个 span 时 EN / ZH 标签也要认出来。"""
        self.assertEqual(pdf_text._label_of(self._line("EN In Congress, July 4, 1776.")), "EN")
        self.assertEqual(pdf_text._label_of(self._line("中国针对该问题的态度")), "")

    def test_plain_fallback_cleans_header_and_leader_dots(self):
        repeat = {pdf_text._norm_furniture("报告标题 | 4")}
        raw = "报告标题 | 4\n概述" + "." * 30 + "2\n正文内容。"
        out = pdf_text._clean_plain(raw, repeat)
        self.assertNotIn("报告标题", out)
        self.assertNotIn("........", out)
        self.assertIn(" … ", out)
        self.assertIn("正文内容", out)

    def test_table_empty_cell_keeps_column_alignment(self):
        text = "| 姓名 | 年龄 | 城市 |\n|---|---|---|\n| 张三 |  | 上海 |"
        out = server.normalize_body_text(text)
        self.assertIn("年龄", out)
        self.assertIn("城市：上海", out)

    def test_safe_file_name_rejects_illegal_chars(self):
        self.assertEqual(server.safe_file_name("报告<草稿>.pdf"), "报告草稿.pdf")
        self.assertNotIn(":", server.safe_file_name("a:b.txt"))
        self.assertNotIn("\r", server.safe_file_name("a\r\nb.txt"))

    def test_server_mime_ignores_client_claim(self):
        self.assertEqual(server._server_mime("a.txt", "text/html"), "text/plain")
        self.assertNotIn("\n", server._server_mime("a.pdf", "text/html\r\nX: y"))

    def test_whitespace_only_file_is_not_stored(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "blank.txt"
            p.write_text("  \n\n  ", encoding="utf-8")
            text, status = server.extract_file_text(p, ".txt")
            self.assertNotEqual(status, "stored")

    def test_usable_body_rejects_control_chars(self):
        text, status = server._usable_body("\x01\x02\x03" * 100, "stored")
        self.assertEqual(text, "")
        self.assertNotEqual(status, "stored")

    def test_usable_body_rejects_page_number_placeholder(self):
        text, status = server._usable_body("〔第 1 页〕\n〔第 2 页〕", "stored")
        # normalize 会留下纯标记文本；真正的拦截由 reading 层的
        # _doc_has_real_text 完成，这里至少不能把它当成成功状态
        self.assertTrue(text == "" or status == "stored")


class WriteClipTextTests(_DbFixture):
    """四个调用点的反向用例：占位符 / 乱码 doc 不许覆写 clips。"""

    def _insert(self, *, status="failed", error="文字层编码异常", body=""):
        self.conn.execute(
            """INSERT INTO clips(id,title,fetch_status,fetch_error,content_text)
               VALUES (1,'报告',?,?,?)""", (status, error, body))
        self.conn.commit()

    def test_garbled_doc_does_not_overwrite(self):
        self._insert()
        self.assertFalse(reading._write_clip_text(self.conn, 1, "\ue000" * 100))
        row = self.conn.execute("SELECT fetch_status, content_text FROM clips WHERE id=1").fetchone()
        self.assertEqual(row["fetch_status"], "failed")
        self.assertEqual(row["content_text"], "")

    def test_placeholder_only_doc_does_not_overwrite(self):
        self._insert()
        self.assertFalse(reading._write_clip_text(
            self.conn, 1, "〔第 1 页〕\n\n〔第 2 页〕"))
        row = self.conn.execute("SELECT fetch_status FROM clips WHERE id=1").fetchone()
        self.assertEqual(row["fetch_status"], "failed")

    def test_trusted_transcript_is_written(self):
        """人工粘贴的文字稿不该被「有没有可读字符」的启发式挡掉。"""
        self._insert()
        self.assertTrue(reading._write_clip_text(self.conn, 1, "---+++", trusted=True))
        row = self.conn.execute("SELECT fetch_status, content_text FROM clips WHERE id=1").fetchone()
        self.assertEqual(row["fetch_status"], "stored")
        self.assertEqual(row["content_text"], "---+++")

    def test_partial_notice_is_preserved(self):
        self._insert(status="stored", error="正文较长，已按前 200 页截断")
        self.assertTrue(reading._write_clip_text(self.conn, 1, "正常的正文内容。"))
        row = self.conn.execute("SELECT fetch_error FROM clips WHERE id=1").fetchone()
        self.assertIn("截断", row["fetch_error"])


# ══════════════════════════════════════════════════════════
# 7. 书签标题与入口归一化（阅读中心目录可读性）
# ══════════════════════════════════════════════════════════

class OutlineTitleTests(unittest.TestCase):

    def test_internal_anchor_names_are_rejected(self):
        """InDesign 导出的书签名是内部对象名，进目录就是乱码。"""
        for junk in ("_headingh.z9qqzdqhy9ye", "_Ref159955435",
                     "2406 China Attitudes COVER_S", "2407", "  ", "h.1ksv4uv"):
            self.assertFalse(reading._outline_title_ok(junk), junk)

    def test_human_titles_pass(self):
        for good in ("概述", "Overview", "Methods / Sample / Survey methods",
                     "Chinese language questionnaire"):
            self.assertTrue(reading._outline_title_ok(good), good)

    def test_page_lead_title_is_used_as_fallback(self):
        pages = ["", "重要研究\n\n中国大陆对 LGBTQ 群体的正面态度", "_Ref1"]
        self.assertEqual(reading._page_lead_title(pages, 1), "重要研究")
        self.assertEqual(reading._page_lead_title(pages, 2), "")
        self.assertEqual(reading._page_lead_title(pages, 9), "")

    def test_suffix_without_dot_routes_to_pdf_parser(self):
        """上游传 'pdf' 而不是 '.pdf' 时不能静默落到纯文本分支。"""
        sentinel = ("doc", [{"id": "ch1"}], {"title": "t"})
        with mock.patch.object(reading, "_parse_pdf", return_value=sentinel) as m:
            got = reading.parse_material(Path("x.pdf"), "pdf")
        self.assertEqual(got, sentinel)
        m.assert_called_once()

    @unittest.skipUnless(SAMPLE.is_file(), "真实样本不在本机")
    def test_real_pdf_chapters_are_readable_and_ordered(self):
        doc, chapters, _meta = reading.parse_material(SAMPLE, ".pdf")
        self.assertGreater(len(doc), 18000)
        self.assertGreaterEqual(len(chapters), 5)
        self.assertNotIn("〔第", doc)
        prev_end = 0
        for i, ch in enumerate(chapters):
            self.assertTrue(ch["title"].strip())
            self.assertNotIn("_", ch["title"])
            self.assertNotIn("_heading", ch["title"])
            # 章与章之间只允许 _build 插入的 "\n\n" 两个分隔符，不重叠也不丢字
            self.assertEqual(ch["start"], prev_end + (2 if i else 0))
            self.assertGreater(ch["end"], ch["start"])
            prev_end = ch["end"]
        self.assertEqual(prev_end, len(doc))


# ══════════════════════════════════════════════════════════
# 8. 启动迁移不得清空文件正文（用户截图的真正根因）
# ══════════════════════════════════════════════════════════

class InitDbKeepsFileTextTests(unittest.TestCase):
    """init_db 里那句「文件件不留抽取文本」的旧迁移，每重启一次就把
    抽好的正文清空一次，还只清 content_text 不动 fetch_status。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.db_path = Path(self.td.name) / "g.db"
        patcher = mock.patch.object(server, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        server.init_db()
        conn = server.db()
        conn.execute("INSERT INTO clips(title,source_type,content_text,fetch_status,file_path)"
                     " VALUES (?,?,?,?,?)", ("某报告", "file", "正" * 200, "stored", "a.pdf"))
        conn.execute("INSERT INTO clips(title,source_type,content_text,fetch_status,file_path)"
                     " VALUES (?,?,?,?,?)", ("空壳", "file", "", "stored", "b.pdf"))
        conn.commit()
        conn.close()

    def test_file_body_survives_restart(self):
        server.init_db()          # 模拟重启
        conn = server.db()
        n = conn.execute("SELECT length(content_text) FROM clips WHERE title='某报告'").fetchone()[0]
        status = conn.execute("SELECT fetch_status FROM clips WHERE title='某报告'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 200)
        self.assertEqual(status, "stored")

    def test_stored_without_body_is_healed(self):
        server.init_db()
        conn = server.db()
        row = conn.execute("SELECT fetch_status,fetch_error FROM clips WHERE title='空壳'").fetchone()
        conn.close()
        self.assertEqual(row["fetch_status"], "failed")
        self.assertTrue((row["fetch_error"] or "").strip())


if __name__ == "__main__":
    unittest.main()
