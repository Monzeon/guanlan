#!/usr/bin/env python
"""回滚「阅读中心 · 目录/标注栏可收起」这次改动（安全模式：默认只预览，不写盘）。

    python tools/rollback_toc_fold.py --dry-run
    python tools/rollback_toc_fold.py --apply
    python tools/rollback_toc_fold.py --apply --keep-docs     # 保留验证截图与说明

做了什么：
  1. static/reading.js  ← static/reading.js.bak-20261108-toc
  2. static/reading.css ← static/reading.css.bak-20261108-toc
  3. README.md 删掉新增的那一行（按整行精确匹配）
  4. 删掉本次的验证脚本、截图、说明与 patch（--keep-docs 时保留）

安全边界：
  * 只用「改动后特征串」确认目标文件确实是本次改过的版本；对不上就跳过该文件，
    不会覆盖你之后的手改（确认要覆盖加 --force）。
  * 不碰数据库、不碰阅读进度、不碰阅读中心以外的任何文件。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (当前文件, 备份文件, 判断「这是改动后版本」的特征串)
RESTORES: list[tuple[str, str, tuple[str, ...]]] = [
    ("static/reading.js", "static/reading.js.bak-20261108-toc",
     ("function bookAnchor()", "rc-rail-left", "function topIn(")),
    ("static/reading.css", "static/reading.css.bak-20261108-toc",
     (".rc-rail", ".rc-panel-head", ".rc-scrim")),
]

README_OLD = "侧栏顶部切到「阅读中心」就进入独立大板块"
README_ADDED = (
    "两边的栏都能收起：点栏头的「«」或按 `[` / `]`，宽屏收成左/右边缘的一条竖签，"
    "窄屏收成抽屉（`Esc`、点遮罩都收）；收起状态记在本机，下次打开还是原样，"
    "正文不会因为折列而丢读到的位置。"
)

NEW_FILES = [
    "tools/verify_toc_fold.js",
    "docs/阅读中心-目录收起.md",
    "docs/阅读中心-目录收起.patch",
]
NEW_DIRS = ["docs/shots-toc-fold"]


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
    if readme.exists() and README_ADDED in readme.read_text(encoding="utf-8"):
        todo.append("README.md 删除新增的「两边的栏都能收起…」那一行")
    elif readme.exists():
        problems.append("README.md 里找不到新增行，跳过（可能已手改）")
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
        text = readme.read_text(encoding="utf-8")
        if README_ADDED in text:
            # 连同它前面那个换行一起去掉，避免留下空行
            text = text.replace("\n" + README_ADDED, "", 1).replace(README_ADDED, "", 1)
            readme.write_text(text, encoding="utf-8", newline="")
    for f in NEW_FILES:
        p = ROOT / f
        if p.exists():
            p.unlink()
    for d in NEW_DIRS:
        p = ROOT / d
        if p.is_dir():
            shutil.rmtree(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="回滚阅读中心目录收起改动")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘（默认行为）")
    ap.add_argument("--keep-docs", action="store_true", help="保留验证脚本、截图和说明")
    ap.add_argument("--force", action="store_true", help="特征串对不上也强行覆盖")
    args = ap.parse_args()

    global NEW_FILES, NEW_DIRS
    if args.keep_docs:
        NEW_FILES, NEW_DIRS = [], []

    todo, problems = plan(args.force)
    head = "将执行" if not args.apply else "执行"
    print(f"── 回滚计划（{head}）──")
    if not todo:
        print("  没有可回滚的内容。")
    for line in todo:
        print("  •", line)
    if problems:
        print("── 跳过 / 注意 ──")
        for line in problems:
            print("  !", line)
    if not args.apply:
        print("\n预览完毕。确认后加 --apply 写盘；回滚后重启服务：python launch.py --stop && python launch.py")
        return 0
    if not todo:
        return 1
    apply()
    print("\n已回滚。重启服务生效：python launch.py --stop && python launch.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
