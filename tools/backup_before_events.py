#!/usr/bin/env python
"""部署事件板块前：SQLite 在线一致性备份，不读取或修改业务记录。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import storage


def main() -> int:
    source = storage.DATA_DIR / "guanlan.db"
    if not source.is_file():
        print(f"数据库不存在：{source}", file=sys.stderr)
        return 1
    backup_dir = Path(r"D:\guanlan\backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / ("guanlan-pre-events-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db")
    if target.exists():
        print(f"备份目标已存在，停止：{target}", file=sys.stderr)
        return 1
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        print(f"备份完整性检查失败：{result}", file=sys.stderr)
        return 1
    print(f"备份完成：{target}")
    print(f"大小：{target.stat().st_size} bytes；完整性：{result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
