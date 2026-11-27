#!/usr/bin/env python
"""观澜 GUANLAN · 路径解析（源码 / PyInstaller 两种形态共用）

打包成 exe 之后有两个目录，混起来会出事：

    APP_DIR   exe 所在目录       数据落盘、cwd、子进程命令行校验
    RES_DIR   随包资源解压目录    static/、*.sql、app/ 图标、vendor/
              （--onefile 下是 sys._MEIPASS 临时目录，源码形态下 = APP_DIR）

PyInstaller 里 ``__file__`` 指向 PYZ 的虚拟路径甚至不存在，别再拿它算程序目录。
"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = (Path(sys.executable).resolve().parent if FROZEN
           else Path(__file__).resolve().parent)
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) if FROZEN else APP_DIR
EXE = Path(sys.executable).resolve()


def self_prefix() -> list[str]:
    """派生「另一个角色」时的命令前缀，保持与当前形态一致。

    * 打包形态：``观澜.exe --role=server``
    * 源码形态：``pythonw.exe -u D:/guanlan/entry.py --role=server``
    """
    if FROZEN:
        return [str(EXE)]
    pw = EXE.with_name("pythonw.exe")
    py = str(pw) if pw.exists() else str(EXE)
    return [py, "-u", str(APP_DIR / "entry.py")]


def role_cmd(role: str, *args: str) -> list[str]:
    return [*self_prefix(), f"--role={role}", *args]
