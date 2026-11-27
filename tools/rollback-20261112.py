#!/usr/bin/env python
"""把 2026-11-12 这轮 bugfix 回滚到改前状态。

改前基线保存在 backup/pre-fix-20261112/，与本脚本同一份程序目录。
回滚只碰代码文件与新增的回归测试，审计文档 / 差分 / 验证记录保留，
方便对照当时到底改了什么。

用法：
    python tools/rollback-20261112.py --dry-run    # 只看会恢复哪些文件
    python tools/rollback-20261112.py --yes        # 真正回滚

回滚后建议复跑：python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "backup" / "pre-fix-20261112"

# (程序里的目标路径, 改前基线文件名)
FILES = [
    ("server.py", "server.py"),
    ("reading.py", "reading.py"),
    ("bible.py", "bible.py"),
    ("pdf_text.py", "pdf_text.py"),
    ("tools/make_url.py", "make_url.py"),
    ("static/app.js", "app.js"),
    ("static/reading.js", "reading.js"),
]

# 本轮新增、回滚后不该再留下的测试（它断言的是修复后的行为）
NEW_TEST = ROOT / "tests" / "test_review_2026_11_12.py"


def main() -> int:
    dry = "--dry-run" in sys.argv
    yes = "--yes" in sys.argv
    if not dry and not yes:
        print("用法：python tools/rollback-20261112.py --dry-run | --yes")
        return 2
    if not BASE.is_dir():
        print(f"找不到改前基线目录：{BASE}")
        return 1
    missing = [src for _, src in FILES if not (BASE / src).is_file()]
    if missing:
        print("基线不完整，拒绝回滚：", ", ".join(missing))
        return 1

    for dst, src in FILES:
        print(("将恢复 " if dry else "已恢复 ") + dst)
        if not dry:
            shutil.copyfile(BASE / src, ROOT / dst)
    if NEW_TEST.is_file():
        print(("将删除 " if dry else "已删除 ") + str(NEW_TEST.relative_to(ROOT)))
        if not dry:
            NEW_TEST.unlink()
    print("回滚完成" if not dry else "（dry-run，未改动任何文件）")
    print("复跑：python -m unittest discover -s tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
