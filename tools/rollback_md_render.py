#!/usr/bin/env python
"""回滚「输入框自动识别 Markdown · 手写内容规范排版」这次改动（默认只预览）。

    python tools/rollback_md_render.py --dry-run
    python tools/rollback_md_render.py --apply
    python tools/rollback_md_render.py --apply --keep-docs

做了什么：
  1. static/app.js     ← static/app.js.bak-20261109-md
  2. static/reading.js ← static/reading.js.bak-20261109-md
  3. static/extra.css  ← static/extra.css.bak-20261109-md
  4. README.md 删掉新增的「手写内容按 Markdown 排版」那一条
  5. 删掉本次的说明与 patch（--keep-docs 保留）

不动数据库：已经存下来的 Markdown 文本原样留着，只是不再渲染成版式，
退回「esc + 换行」的老样子。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RESTORES: list[tuple[str, str, tuple[str, ...]]] = [
    ("static/app.js", "static/app.js.bak-20261109-md",
     ("mdBody", "bindMdPreview", "data-md-preview")),
    ("static/reading.js", "static/reading.js.bak-20261109-md",
     ("bindMdPreview", "data-md-preview")),
    ("static/extra.css", "static/extra.css.bak-20261109-md",
     (".md-body", ".md-preview")),
]

README_NEW = (
    """- **手写内容按 Markdown 排版**：收存框、我的判断、随手记、批注、AI 简介里写的 `#` / `**粗体**` / `- 列表` / `> 引用` / `---` / 代码块 / 表格，存下来就按版式显示，不再把记号原样露出来；正在写的输入框下面会长出一块「预览」，只在真写了记号时出现。标题取第一行，但会把记号摘掉。卡片摘要里也只留干净的句子。"""
)

NEW_FILES = [
    "docs/Markdown-输入与排版.md",
    "docs/Markdown-输入与排版.patch",
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
        todo.append("README.md 删掉新增的「手写内容按 Markdown 排版」那一条")
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
    ap = argparse.ArgumentParser(description="回滚输入框 Markdown 渲染改动")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘（默认行为）")
    ap.add_argument("--keep-docs", action="store_true", help="保留说明与 patch")
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
        print("\n预览完毕。确认后加 --apply 写盘；回滚后刷新浏览器（Ctrl+F5）即可。")
        return 0
    if not todo:
        return 1
    apply()
    print("\n已回滚。刷新浏览器（Ctrl+F5）生效；静态文件有缓存，必要时重启服务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
