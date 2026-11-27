#!/usr/bin/env python
"""校验阅读中心的书籍解析：章名、层级、小节位置、正文是否重复章名。

    python tools/verify_epub_parse.py            # 校验库里所有书籍
    python tools/verify_epub_parse.py --reparse  # 先按原件重解析，再校验

只读原件与数据库；--reparse 会重建 doc_text/chapters（等同阅读器里的「重新解析」）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reading  # noqa: E402
import storage  # noqa: E402

DB = storage.DATA_DIR / "guanlan.db"
UPLOADS = storage.DATA_DIR / "uploads"
PUNCT = " \t\r\n\u3000·・:：、,，.。!！?？-—－_/\\|()（）[]【】{}'\"“”‘’《》〈〉"


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in PUNCT)


def check_book(conn, row) -> tuple[bool, list[str]]:
    cid = row["clip_id"]
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not clip or not clip["file_path"]:
        return False, [f"#{cid} 缺少原件记录"]
    path = UPLOADS / clip["file_path"]
    if not path.is_file():
        return False, [f"#{cid} 原件不在本机：{path.name}"]
    suffix = Path(clip["file_name"] or path.name).suffix.lower()
    doc, chapters, meta = reading.parse_material(path, suffix)
    item = conn.execute("SELECT * FROM reading_items WHERE clip_id=?", (cid,)).fetchone()
    stored = json.loads(item["chapters"] or "[]") if item else []
    lines = [f"#{cid} {(clip['title'] or '')[:36]}"]
    lines.append(f"  解析结果：{len(chapters)} 章 / "
                 f"{sum(len(c.get('sections') or []) for c in chapters)} 节 / {len(doc)} 字"
                 f"（库中：{len(stored)} 章）")
    bad = []
    for i, c in enumerate(chapters, 1):
        title = c["title"]
        if re_placeholder(title):
            bad.append(f"  第 {i} 章标题仍是占位名：{title}")
        head = norm(doc[c["start"]:c["start"] + len(title) * 2 + 8])
        if head.startswith(norm(title)) and len(norm(title)) >= 4:
            bad.append(f"  第 {i} 章正文开头重复章名：{title}")
        for s in c.get("sections") or []:
            at = c["start"] + int(s["offset"])
            if not (c["start"] <= at < c["end"]):
                bad.append(f"  小节越界：{title} / {s['title']}")
                continue
            if not norm(doc[at:at + len(s["title"]) + 4]).startswith(norm(s["title"])[:12]):
                bad.append(f"  小节落点对不上：{s['title']} @{s['offset']}")
    lines.append("  " + ("FAIL" if bad else "PASS"))
    return not bad, lines + bad


def re_placeholder(title: str) -> bool:
    import re
    return bool(re.fullmatch(r"第\s*\d+\s*节", title or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reparse", action="store_true", help="先按原件重解析并写回数据库")
    args = ap.parse_args()

    reading._CTX = {"UPLOAD_DIR": UPLOADS}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM reading_items ORDER BY clip_id").fetchall()
    books = []
    for r in rows:
        clip = conn.execute("SELECT * FROM clips WHERE id=?", (r["clip_id"],)).fetchone()
        if clip and Path(clip["file_name"] or "").suffix.lower() in {".epub", ".pdf", ".txt", ".md"}:
            books.append(r)
    if not books:
        print("库里还没有可校验的书籍。")
        return 0

    if args.reparse:
        for r in books:
            item = conn.execute("SELECT * FROM reading_items WHERE clip_id=?",
                                (r["clip_id"],)).fetchone()
            clip = conn.execute("SELECT * FROM clips WHERE id=?", (r["clip_id"],)).fetchone()
            path = UPLOADS / clip["file_path"]
            doc, chapters, _ = reading.parse_material(path, Path(clip["file_name"]).suffix.lower())
            pct = float(item["progress"] or 0)
            conn.execute("""UPDATE reading_items SET doc_text=?, chapters=?, char_count=?,
                            position=?, progress=? WHERE clip_id=?""",
                         (doc, json.dumps(chapters, ensure_ascii=False), len(doc),
                          int(len(doc) * pct / 100), pct, r["clip_id"]))
            reading.reindex(conn, r["clip_id"])
            print(f"已重解析 #{r['clip_id']}")
        conn.commit()

    ok_all = True
    for r in books:
        ok, lines = check_book(conn, r)
        ok_all = ok_all and ok
        print("\n".join(lines))
    conn.close()
    print("\n" + ("全部通过" if ok_all else "有项目未通过"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
