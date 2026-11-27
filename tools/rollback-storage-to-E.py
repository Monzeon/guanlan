#!/usr/bin/env python
"""观澜 · 把存储退回 E 盘程序目录（撤销 2026-11-11 的「数据搬去 D 盘」）

迁移之后数据在 D:\\GuanlanData\\data，代码通过 storage.py 解析这个根。
退回去不用改代码：把数据搬回 ROOT\\data，再让 GL_HOME 指回程序目录就行
（storage.py 里环境变量优先于默认的 D 盘）。

    python tools/rollback-storage-to-E.py --dry-run   # 只看会做什么
    python tools/rollback-storage-to-E.py             # 真做：停服 → 搬回 → 设环境变量
    python tools/rollback-storage-to-E.py --from-snapshot
                                                      # 用迁移时的快照，而不是 D 盘现状

搬什么：
    默认搬 D 盘现在这份——它是迁移后真正在用的数据。
    --from-snapshot 搬 data.bak-moved-20261111，等于回到迁移前那一刻（之后的新内容不要了）。

搬完要重新双击「观澜」图标：跑着的进程还在用旧路径。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data"
SNAPSHOT = ROOT / "data.bak-moved-20261111"
DEFAULT_SOURCE = Path(r"D:\GuanlanData\data")


def stop_server() -> None:
    launcher = ROOT / "launch.py"
    if not launcher.is_file():
        return
    print("· 停止服务")
    subprocess.run([sys.executable, str(launcher), "--stop"], cwd=str(ROOT),
                   check=False, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="把观澜的数据目录搬回程序目录（E 盘）")
    ap.add_argument("--from-snapshot", action="store_true",
                    help="用迁移时的快照 data.bak-moved-20261111，而不是 D 盘现状")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不动文件")
    args = ap.parse_args()

    source = SNAPSHOT if args.from_snapshot else DEFAULT_SOURCE
    if not source.is_dir():
        print(f"找不到源目录：{source}")
        return 1

    print(f"源  {source}")
    print(f"目标 {TARGET}")
    if args.dry_run:
        print("（--dry-run：到此为止，没有动任何文件）")
        return 0

    stop_server()

    if TARGET.exists():
        # 目标已经有一份，不覆盖：换个名字留着，谁也不知道里面有没有新东西
        parked = ROOT / f"data.before-rollback-{time.strftime('%Y%m%d-%H%M%S')}"
        print(f"· 目标已存在，先挪到 {parked.name}")
        TARGET.rename(parked)

    print("· 复制中…")
    shutil.copytree(source, TARGET, copy_function=shutil.copy2)
    files = [p for p in TARGET.rglob("*") if p.is_file()]
    print(f"· 完成：{len(files)} 个文件，"
          f"{sum(p.stat().st_size for p in files) / 1024 / 1024:.1f} MB")

    if sys.platform == "win32":
        # 只对新开的进程生效；已经开着的终端要重开
        subprocess.run(["setx", "GL_HOME", str(ROOT)], check=False,
                       capture_output=True, text=True)
        print(f"· 已设置用户环境变量 GL_HOME={ROOT}（新开的进程才生效）")

    print("\n回滚完成。重新双击「观澜」图标即可；要再搬回 D 盘就删掉 GL_HOME 变量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
