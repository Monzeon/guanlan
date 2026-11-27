#!/usr/bin/env python
"""回滚「PDF 正文抽取 · 版面还原」这次改动（默认只预览）。

    python tools/rollback_pdf_layout.py --dry-run
    python tools/rollback_pdf_layout.py --apply
    python tools/rollback_pdf_layout.py --apply --keep-docs

做了什么：
  1. server.py  ← server.py.bak-20261109-pdf（PDF 分支回到 pypdf 裸抽取，
     _smart_join 回到不补中英边界空格的老规则，backfill 不再支持 redo）
  2. reading.py ← reading.py.bak-20261109-pdf（_parse_pdf 回到逐页裸抽取）
  3. 删掉 pdf_text.py 与本次的校验脚本、说明、patch（--keep-docs 保留）

不动数据库：已经重抽过的正文仍是新版整理出来的段落，旧代码也能正常显示；
要回到旧版段落，重新导入原件或再点一次「重提正文」即可。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RESTORES: list[tuple[str, str, tuple[str, ...]]] = [
    ("server.py", "server.py.bak-20261109-pdf",
     ("import pdf_text", "pdf_text.extract_pdf_text(path)", "_is_cjk_char")),
    ("reading.py", "reading.py.bak-20261109-pdf",
     ("import pdf_text", "pdf_text.extract_pdf_pages")),
]

README_NEW = (
    "- **PDF 正文按版面还原**（`pdf_text.py`）：先按字符坐标把每页还原成行，"
    "再找栏缝、去页眉页脚、按行距与缩进切段，最后才接断行——双栏论文不再串栏，"
    "双语对照的原文与译文各成一段（不会再出现「1776.中大陆会议」这种粘连）。"
    "扫描件与加密件给的是具体原因，不是一句「解析失败」。"
    "老条目想用新版重抽：详情页点「重提正文」，或调一次 "
    "`POST /api/tools/backfill {\"redo\":\"pdf\"}` 批量重抽所有 PDF。"
)

NEW_FILES = [
    "pdf_text.py",
    "tools/verify_pdf_parse.py",
    "docs/PDF-正文抽取.md",
    "docs/PDF-正文抽取.patch",
]


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
        if not force and not any(m in text for m in marks):
            problems.append(f"{cur} 已经不是本次改动后的版本（特征串对不上），跳过")
            continue
        todo.append(f"还原 {cur} ← {bak}")
    readme = ROOT / "README.md"
    if readme.exists() and README_NEW.encode("utf-8") in readme.read_bytes():
        todo.append("README.md 删掉新增的「PDF 正文按版面还原」那一条")
    for f in NEW_FILES:
        if (ROOT / f).exists():
            todo.append(f"删除 {f}")
    return todo, problems


def apply() -> None:
    for cur, bak, _marks in RESTORES:
        bpath, cpath = ROOT / bak, ROOT / cur
        if bpath.exists():
            shutil.copyfile(bpath, cpath)
    readme = ROOT / "README.md"
    if readme.exists():
        raw = readme.read_bytes()
        new = README_NEW.encode("utf-8")
        if new in raw:
            readme.write_bytes(raw.replace(new + b"\n", b"", 1))
    for f in NEW_FILES:
        p = ROOT / f
        if p.exists():
            p.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="回滚 PDF 正文抽取的版面还原改动")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘（默认行为）")
    ap.add_argument("--keep-docs", action="store_true", help="保留校验脚本与说明")
    ap.add_argument("--force", action="store_true", help="特征串对不上也强行覆盖")
    args = ap.parse_args()

    global NEW_FILES
    if args.keep_docs:
        NEW_FILES = []

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
