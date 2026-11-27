#!/usr/bin/env python
"""事件板块部署：保留旧包副本、短停、完整替换；不接触数据库文件。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "dist" / "观澜"
STAMP = os.environ.get("GL_EVENT_STAMP", "20261119-094938")
BACKUP = ROOT / "backups" / ("app-pre-events-" + STAMP)
STAGED = ROOT / "dist" / ("观澜-event-staging-" + STAMP)
NO_WINDOW = 0x08000000


def stop() -> None:
    subprocess.run([sys.executable, str(ROOT / "entry.py"), "--stop"],
                   capture_output=True, creationflags=NO_WINDOW, timeout=45)
    time.sleep(1.5)


def main() -> int:
    source_text = os.environ.get("GL_EVENT_DIST", "")
    source = Path(source_text) if source_text else Path(os.environ["BOX_AGENT_SCRATCH_DIR"]) / "event-dist-deploy" / "观澜"
    exe = source / "观澜.exe"
    required = [source / "_internal" / "static" / "event-topics.js",
                source / "_internal" / "event_topics_schema.sql"]
    if not exe.is_file() or not all(p.is_file() for p in required):
        print(f"独立包不完整，未部署：{source}", file=sys.stderr)
        return 1
    if not CURRENT.is_dir():
        print(f"现有程序目录不存在：{CURRENT}", file=sys.stderr)
        return 1
    print("[1] 旧程序完整副本")
    if not BACKUP.is_dir():
        print("缺少旧程序备份，停止部署。", file=sys.stderr)
        return 1
    print(BACKUP)
    print("[2] 新包暂存目录")
    if not STAGED.is_dir():
        print("缺少新包暂存目录，停止部署。", file=sys.stderr)
        return 1
    print("[3] 停止观澜（数据库不动）")
    stop()
    print("[4] 逐文件覆盖，不移动或删除任何目录")
    count = 0
    for src in STAGED.rglob("*"):
        if not src.is_file():
            continue
        dst = CURRENT / src.relative_to(STAGED)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    print(f"已覆盖 {count} 个文件；旧程序完整备份与暂存副本均保留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
