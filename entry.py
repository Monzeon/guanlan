#!/usr/bin/env python
"""观澜 GUANLAN · 单一入口

四个角色共用一个可执行文件，靠 ``--role`` 区分，PyInstaller --onefile
打包之后子进程仍然指向自己（sys.executable），不会再去找磁盘上的 .py：

    观澜.exe                            启动器（默认角色）
    观澜.exe --role=server --lan        HTTP 服务（server.py 的 main）
    观澜.exe --role=tray                托盘守护（tray.py 的 main）
    观澜.exe --role=window              WebView2 宿主窗口（native_window.py）
    观澜.exe --stop / --status          运维（还是 launch.py 的 main）

源码形态下等价写法：

    pythonw entry.py
    pythonw entry.py --role=server --lan
    python  entry.py --status
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import app_paths


def bootstrap_stdio() -> None:
    """--noconsole 下 sys.stdout / sys.stderr 可能是 None，print() 会炸。

    优先复用 fd 1/2（launch.py 派生子进程时把它们重定向到了日志文件），
    实在没有再退到 os.devnull。
    """
    if not app_paths.FROZEN:
        return
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name, None) is not None:
            continue
        try:
            setattr(sys, name, os.fdopen(
                fd, "w", encoding="utf-8", errors="replace", closefd=False))
        except Exception:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass


def split_role(argv: list[str]) -> tuple[str | None, list[str]]:
    """同时认 ``--role=server`` 和 ``--role server``，其余参数原样留下。"""
    role: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--role" and i + 1 < len(argv):
            role = argv[i + 1]
            i += 2
        elif a.startswith("--role="):
            role = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    return role, rest


def main(argv: list[str]) -> int:
    bootstrap_stdio()
    role, rest = split_role(argv)

    # --noconsole 下没有控制台，排查启动问题全靠这份日志
    try:
        import storage
        storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (storage.DATA_DIR / "entry.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"role={role or 'launcher'} argv={rest!r} "
                    f"frozen={app_paths.FROZEN} exe={app_paths.EXE}\n")
    except Exception:
        pass

    if role == "server":
        import server
        sys.argv = ["server.py", *rest]       # server.main() 内部用 argparse
        return server.main()
    if role == "tray":
        import tray
        return tray.main(rest)
    if role == "window":
        import native_window
        return native_window.main()
    if role is None or role == "launcher":
        import launch
        return launch.main(rest)
    print(f"观澜：未知角色 --role={role}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
