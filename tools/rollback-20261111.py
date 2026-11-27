#!/usr/bin/env python
"""观澜 · 2026-11-11 PDF 修复回滚工具

用法：
    python tools/rollback-20261111.py --list
    python tools/rollback-20261111.py --db                 # 恢复 pre-pdf-fix 基线库
    python tools/rollback-20261111.py --db --file <path>   # 恢复指定备份
    python tools/rollback-20261111.py --check              # 只看当前库的健康度，不动任何东西

它做三件事：停服务 → 备份当前库 → 换回旧库 → 起服务 → 复核。
代码层不做自动回滚：本仓库 HEAD 落后工作区 7000+ 行，git checkout
会连带抹掉未提交的既有工作。逐条撤销请对照
`docs/audit-2026-11-11-pdf-fix.md` 第三节「关键差分」反向替换。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB = DATA / "guanlan.db"
BACKUP_DIR = ROOT / "backups"
DEFAULT_BACKUP = BACKUP_DIR / "guanlan.pre-pdf-fix-20261111.db"
PORT = 8790


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=15).stdout
    except Exception:
        return ""
    return out.decode("mbcs", "replace")


def pid_on_port() -> int | None:
    for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") \
                and parts[3].upper() == "LISTENING" and parts[4].isdigit():
            return int(parts[4])
    return None


def is_python(pid: int) -> bool:
    return "python" in _run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).lower()


def stop_server() -> None:
    pid = pid_on_port()
    if not pid:
        print("· 服务没在跑，跳过停止")
        return
    if not is_python(pid):
        print(f"· 端口 {PORT} 上是别的程序（PID {pid}），没动它。请手工处理后再跑")
        raise SystemExit(1)
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, creationflags=0x08000000)
    for _ in range(40):
        if not pid_on_port():
            break
        time.sleep(0.1)
    print(f"· 已停止 PID {pid}")


def start_server() -> None:
    sys.path.insert(0, str(ROOT))
    try:
        import launch
        proc = launch.start_server()
        print(f"· 已启动 PID {proc.pid}")
    except Exception as exc:            # 起不来时给出可操作的替代路径
        print(f"· 自动启动失败（{type(exc).__name__}: {exc}）")
        print("  请双击桌面「观澜」图标启动")
        return
    for _ in range(60):
        if pid_on_port():
            print(f"· 端口 {PORT} 已监听")
            return
        time.sleep(0.25)
    print(f"· 等待超时，端口 {PORT} 仍未监听；看 data/app.log")


def health(db: Path) -> None:
    if not db.is_file():
        print(f"· {db} 不存在")
        return
    conn = sqlite3.connect(db)
    try:
        try:
            rows = conn.execute(
                "SELECT id,length(coalesce(content_text,'')) n,fetch_status "
                "FROM clips WHERE source_type='file' ORDER BY id").fetchall()
        except sqlite3.DatabaseError as exc:
            print(f"· 读不出来：{exc}")
            return
        total = sum(n for _, n, _ in rows)
        bad = [i for i, n, st in rows if st == "stored" and n == 0]
        print(f"· {db.name}: {len(rows)} 条文件条目 / {total} 字")
        print(f"· 「stored 但空正文」: {len(bad)} 条 {bad[:8]}")
    finally:
        conn.close()


def rollback(target: Path) -> int:
    if not target.is_file():
        print(f"找不到备份：{target}")
        return 1
    stop_server()
    DATA.mkdir(parents=True, exist_ok=True)
    if DB.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        keep = BACKUP_DIR / f"guanlan.before-rollback-{stamp}.db"
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB, keep)
        print(f"· 当前库已另存：{keep}")
    for suffix in ("-wal", "-shm"):
        DB.with_name(DB.name + suffix).unlink(missing_ok=True)
    shutil.copy2(target, DB)
    print(f"· 已恢复：{target.name}")
    health(DB)
    start_server()
    print("完成。若界面仍显示旧状态，浏览器 Ctrl+F5 强刷一次。")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rollback-20261111", add_help=True)
    ap.add_argument("--db", action="store_true", help="恢复数据库备份")
    ap.add_argument("--file", default="", help="指定备份文件（默认 pre-pdf-fix）")
    ap.add_argument("--list", action="store_true", help="列出可用备份")
    ap.add_argument("--check", action="store_true", help="只看当前库健康度")
    args = ap.parse_args(argv)

    if args.list or not (args.db or args.check):
        print("可用的数据库备份：")
        found = sorted(BACKUP_DIR.glob("guanlan*.db")) if BACKUP_DIR.is_dir() else []
        for p in found:
            size = p.stat().st_size / 1024 / 1024
            print(f"  {p.relative_to(ROOT)}  {size:.1f} MB")
        if not found:
            print("  （没有）")
        print("\n--db 恢复默认基线；--check 只体检不动库。")
        print("代码层回滚请对照 docs/audit-2026-11-11-pdf-fix.md 第三节。")
        if not args.db and not args.check:
            return 0

    if args.check:
        health(DB)
        return 0

    target = Path(args.file).expanduser() if args.file else DEFAULT_BACKUP
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    return rollback(target)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
