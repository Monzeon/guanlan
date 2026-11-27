#!/usr/bin/env python
"""观澜 · 一键打包 dist/观澜.exe

    python tools/build_exe.py

依赖：python -m pip install pyinstaller pywebview
产物大小、路径与构建日志尾部会打印出来；日志全文在 build/pyi.log。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "build" / "pyi.log"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(ROOT / "dist"),
           "--workpath", str(ROOT / "build"),
           str(ROOT / "guanlan.spec")]
    print("构建命令:", " ".join(cmd), flush=True)
    t0 = time.time()
    with LOG.open("w", encoding="utf-8", errors="replace") as f:
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace")
        for line in p.stdout or []:
            f.write(line)
            if any(k in line for k in ("ERROR", "WARNING", "Building EXE",
                                       "completed successfully", "INFO: Build")):
                print("  " + line.rstrip(), flush=True)
        rc = p.wait()

    exe = ROOT / "dist" / "观澜" / "观澜.exe"
    total = sum(p.stat().st_size for p in (ROOT / "dist" / "观澜").rglob("*")
                if p.is_file()) if (ROOT / "dist" / "观澜").is_dir() else 0
    print(f"\n耗时 {time.time() - t0:.1f}s · PyInstaller rc={rc}")
    if exe.exists():
        print(f"产物：{exe}")
        print(f"      目录合计 {total / 1048576:.1f} MiB（onedir，含 _internal/）")
    else:
        print(f"[ERROR] 没有生成 {exe}，日志见 {LOG}")
    print(f"完整日志：{LOG}")
    return 0 if rc == 0 and exe.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
