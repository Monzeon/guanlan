#!/usr/bin/env python
"""阅读 AI 部署：完整备份旧包，短停后逐文件覆盖，不动数据库。"""
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
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = ROOT / "backups" / ("app-pre-reading-ai-" + STAMP)
STAGED = ROOT / "dist" / ("观澜-reading-ai-staging-" + STAMP)


def main() -> int:
    source = Path(os.environ.get("GL_READING_AI_DIST") or
                  (Path(os.environ["BOX_AGENT_SCRATCH_DIR"]) / "reading-ai-dist" / "观澜"))
    required = [source / "观澜.exe", source / "_internal" / "static" / "reading.js",
                source / "_internal" / "static" / "reading-ai.css"]
    if not all(p.is_file() for p in required) or not CURRENT.is_dir():
        print("新包或现有目录不完整，停止部署。", file=sys.stderr)
        return 1
    shutil.copytree(CURRENT, BACKUP)
    print("旧程序备份：" + str(BACKUP), flush=True)
    shutil.copytree(source, STAGED)
    print("新包暂存：" + str(STAGED), flush=True)
    subprocess.run([sys.executable, str(ROOT / "entry.py"), "--stop"],
                   capture_output=True, creationflags=0x08000000, timeout=45)
    time.sleep(1.5)
    count = 0
    for src in STAGED.rglob("*"):
        if not src.is_file():
            continue
        dst = CURRENT / src.relative_to(STAGED)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    print(f"部署完成，覆盖 {count} 个文件；数据库未修改。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
