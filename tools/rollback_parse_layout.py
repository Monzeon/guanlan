#!/usr/bin/env python
"""回滚「阅读中心 · 导入书籍的解析与排版」这次改动（默认只预览）。

    python tools/rollback_parse_layout.py --dry-run
    python tools/rollback_parse_layout.py --apply
    python tools/rollback_parse_layout.py --apply --keep-docs

做了什么：
  1. reading.py         ← reading.py.bak-20261108-parse
  2. static/reading.js  ← static/reading.js.bak-20261108-parse
  3. static/reading.css ← static/reading.css.bak-20261108-parse
  4. README.md 还原「阅读」那一条
  5. 删掉本次的校验脚本、说明、patch 与截图（--keep-docs 保留）

不动数据库：已经重解析过的书仍是新目录，旧前端也能正常读；
要回到旧目录，重新导入原件即可。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RESTORES: list[tuple[str, str, tuple[str, ...]]] = [
    ("reading.py", "reading.py.bak-20261108-parse",
     ("def _html_blocks(", "def _epub_nav(", "def api_reparse(")),
    ("static/reading.js", "static/reading.js.bak-20261108-parse",
     ("rc-toc-caret", "rc-toc-sec", "revealTop")),
    ("static/reading.css", "static/reading.css.bak-20261108-parse",
     (".rc-toc-caret", "h3.rc-sec", 'data-kind="book"')),
]

README_NEW = (
    "- **阅读**：EPUB 按原书目录锚点分章、按标题分节（目录里可展开收起，点小节直接跳转），"
    "PDF 按大纲或页组分章；上/下章、五色高亮与 Markdown 批注、书签、字号/行距/宽度/纸张可调、"
    "续读定位与进度记录。高亮直接写进 `clip_annotations`，回收存详情页同样看得见。"
)
README_OLD = (
    "- **阅读**：分章渲染、目录跳转、上/下章、五色高亮与 Markdown 批注、书签、"
    "字号/行距/宽度/纸张可调、续读定位与进度记录。高亮直接写进 `clip_annotations`，"
    "回收存详情页同样看得见。"
)

NEW_FILES = [
    "tools/verify_epub_parse.py",
    "docs/阅读中心-解析与排版.md",
    "docs/阅读中心-解析与排版.patch",
]
NEW_DIRS = ["docs/shots-parse"]


def plan(force: bool) -> tuple[list[str], list[str]]:
    todo: list[str] = []
    problems: list[str] = []
    for cur, bak, marks in RESTORES:
        cpath, bpath = ROOT / cur, ROOT / bak
        if not cpath.exists():
            problems.append(f"缺少 {cur}")
            continue
        if not bpath.exists():
            problems.append(f"缺少备份 {bak}（无法还原 {cur}）")
            continue
        text = cpath.read_text(encoding="utf-8", errors="replace")
        if not force and not all(m in text for m in marks):
            problems.append(f"{cur} 已经不是本次改动后的版本（特征串对不上），跳过")
            continue
        todo.append(f"还原 {cur} ← {bak}")
    readme = ROOT / "README.md"
    if readme.exists() and README_NEW.encode("utf-8") in readme.read_bytes():
        todo.append("README.md 还原「阅读」那一条")
    elif readme.exists():
        problems.append("README.md 里找不到本次改的那一条，跳过（可能已手改）")
    for f in NEW_FILES:
        if (ROOT / f).exists():
            todo.append(f"删除 {f}")
    for d in NEW_DIRS:
        if (ROOT / d).is_dir():
            todo.append(f"删除目录 {d}/")
    return todo, problems


def apply() -> None:
    for cur, bak, _marks in RESTORES:
        bpath, cpath = ROOT / bak, ROOT / cur
        if bpath.exists():
            shutil.copyfile(bpath, cpath)
    readme = ROOT / "README.md"
    if readme.exists():
        raw = readme.read_bytes()
        new, old = README_NEW.encode("utf-8"), README_OLD.encode("utf-8")
        if new in raw:
            readme.write_bytes(raw.replace(new, old, 1))
    for f in NEW_FILES:
        p = ROOT / f
        if p.exists():
            p.unlink()
    for d in NEW_DIRS:
        p = ROOT / d
        if p.is_dir():
            shutil.rmtree(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="回滚阅读中心解析与排版改动")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘（默认行为）")
    ap.add_argument("--keep-docs", action="store_true", help="保留校验脚本、截图和说明")
    ap.add_argument("--force", action="store_true", help="特征串对不上也强行覆盖")
    args = ap.parse_args()

    global NEW_FILES, NEW_DIRS
    if args.keep_docs:
        NEW_FILES, NEW_DIRS = [], []

    todo, problems = plan(args.force)
    print(f"── 回滚计划（{'执行' if args.apply else '将执行'}）──")
    if not todo:
        print("  没有可回滚的内容。")
    for line in todo:
        print("  •", line)
    if problems:
        print("── 跳过 / 注意 ──")
        for line in problems:
            print("  !", line)
    if not args.apply:
        print("\n预览完毕。确认后加 --apply 写盘；回滚后重启服务："
              "python launch.py --stop && python launch.py")
        return 0
    if not todo:
        return 1
    apply()
    print("\n已回滚。重启服务生效：python launch.py --stop && python launch.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
