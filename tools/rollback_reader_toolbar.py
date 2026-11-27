#!/usr/bin/env python
"""回滚「阅读中心 · A− / A＋ / ◐ 按钮修复」这次改动（默认只预览，不写盘）。

    python tools/rollback_reader_toolbar.py --dry-run
    python tools/rollback_reader_toolbar.py --apply
    python tools/rollback_reader_toolbar.py --apply --keep-docs

做了什么：
  1. static/reading.js                                  ← 同名 .bak-20261109-toolbar
  2. android-local/app/src/main/python/static/reading.js ← 同名 .bak-20261109-toolbar
  3. 删掉本次新增的验证脚本、验证日志、截图、说明与 patch（--keep-docs 时保留）

安全边界：
  * 先用「改动后特征串」确认目标文件确实是本次改过的版本；对不上就跳过该文件，
    不会覆盖你之后的手改（确认要覆盖加 --force）。
  * 备份文件保留在原地，随时可再手工还原。
  * 不碰数据库、不碰阅读进度、不碰本机偏好（gl_reading_prefs）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = "bak-20261109-toolbar"

# (当前文件, 备份文件, 判断「这是改动后版本」的特征串)
RESTORES: list[tuple[str, str, tuple[str, ...]]] = [
    ("static/reading.js", f"static/reading.js.{TAG}",
     ("id=\"rc-paper-surface\"", "id=\"rc-paper-btn\"", "PAPER_NAME")),
    ("android-local/app/src/main/python/static/reading.js",
     f"android-local/app/src/main/python/static/reading.js.{TAG}",
     ("id=\"rc-paper-surface\"", "id=\"rc-paper-btn\"", "PAPER_NAME")),
]

NEW_FILES = [
    "tools/verify_reader_toolbar.js",
    "docs/阅读中心-字号纸张按钮修复.md",
    "docs/阅读中心-字号纸张按钮修复.patch",
    "docs/阅读中心-字号纸张按钮修复.verify.log",
]
NEW_DIRS = ["docs/shots-toolbar"]


def plan(force: bool, keep_docs: bool) -> tuple[list[str], list[str]]:
    todo: list[str] = []
    problems: list[str] = []
    for cur, bak, marks in RESTORES:
        cpath, bpath = ROOT / cur, ROOT / bak
        if not cpath.exists():
            problems.append(f"缺少 {cur}")
            continue
        if not bpath.exists():
            problems.append(f"缺少备份 {bak}（跳过 {cur}）")
            continue
        text = cpath.read_text(encoding="utf-8", errors="replace")
        if not force and not all(m in text for m in marks):
            problems.append(f"{cur} 已经不是本次改动后的版本（特征串对不上），跳过")
            continue
        todo.append(f"还原 {cur} ← {bak}")
    if not keep_docs:
        for f in NEW_FILES:
            if (ROOT / f).exists():
                todo.append(f"删除 {f}")
        for d in NEW_DIRS:
            if (ROOT / d).is_dir():
                todo.append(f"删除目录 {d}/")
    return todo, problems


def apply(keep_docs: bool) -> None:
    for cur, bak, _marks in RESTORES:
        bpath, cpath = ROOT / bak, ROOT / cur
        if bpath.exists():
            shutil.copyfile(bpath, cpath)
    if not keep_docs:
        for f in NEW_FILES:
            p = ROOT / f
            if p.exists():
                p.unlink()
        for d in NEW_DIRS:
            p = ROOT / d
            if p.is_dir():
                shutil.rmtree(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="回滚阅读器工具栏按钮修复")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘（默认行为）")
    ap.add_argument("--keep-docs", action="store_true", help="保留验证脚本、截图和说明")
    ap.add_argument("--force", action="store_true", help="特征串对不上也强行覆盖")
    args = ap.parse_args()

    todo, problems = plan(args.force, args.keep_docs)
    print(f"── 回滚计划（{'执行' if args.apply else '预览'}）──")
    if not todo:
        print("  没有可回滚的内容。")
    for line in todo:
        print("  •", line)
    if problems:
        print("── 跳过 / 注意 ──")
        for line in problems:
            print("  !", line)
    if not args.apply:
        print("\n预览完毕。确认后加 --apply 写盘；回滚后刷新页面（Ctrl+F5）生效。")
        return 0
    if not todo:
        return 1
    apply(args.keep_docs)
    print("\n已回滚。刷新页面（Ctrl+F5）生效；服务本身读磁盘，无需重启。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
