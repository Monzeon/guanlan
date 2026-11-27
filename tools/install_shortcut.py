#!/usr/bin/env python
"""在桌面创建「观澜（应用版）」快捷方式，指向打包产物。

    python tools/install_shortcut.py            创建 / 更新
    python tools/install_shortcut.py --remove   删除

故意**不覆盖**原来的「观澜.url」：那个仍指向 launch.vbs（源码形态），
留作回退入口。新建的是另一个 .lnk，两个入口并存。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "dist" / "观澜" / "观澜.exe"
ICON = ROOT / "app" / "guanlan.ico"
WORKDIR = ROOT / "dist" / "观澜"
DESKTOP = Path.home() / "Desktop"
LNK = DESKTOP / "观澜（应用版）.lnk"


def main(argv: list[str]) -> int:
    if "--remove" in argv:
        if LNK.exists():
            LNK.unlink()
            print(f"已删除 {LNK}")
        else:
            print("快捷方式不存在")
        return 0

    if not TARGET.exists():
        print(f"[ERROR] 找不到 {TARGET}，先跑 python tools/build_exe.py")
        return 1
    if not DESKTOP.is_dir():
        print(f"[ERROR] 桌面目录不存在：{DESKTOP}")
        return 1

    import win32com.client                       # pywin32，本机已装
    sh = win32com.client.Dispatch("WScript.Shell")
    lnk = sh.CreateShortcut(str(LNK))
    lnk.TargetPath = str(TARGET)
    lnk.WorkingDirectory = str(WORKDIR)
    lnk.IconLocation = str(ICON)
    lnk.Description = "观澜 · 原生应用（WebView2，无需 Python 环境）"
    lnk.Save()
    print(f"已创建：{LNK}")
    print(f"  目标：{TARGET}")
    print(f"  图标：{ICON}")
    print("  原「观澜.url」未改动，已改为优先启动同一个 exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
