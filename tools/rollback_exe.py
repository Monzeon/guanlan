#!/usr/bin/env python
"""回滚「单入口 + PyInstaller exe」改造，回到上一版（四个独立 .py + 源码形态）。

    python tools/rollback_exe.py --dry-run    只检查，不改任何文件
    python tools/rollback_exe.py               真正回滚

做的事：
    1. 用当前 entry.py --stop 收干净（服务 / 托盘 / 宿主窗口）
    2. 移除桌面「观澜（应用版）」快捷方式（原「观澜.url」不动）
    3. 用 backup/2026-11-14-exe/ 的精确副本还原 11 个文件
    4. 移走本次新增文件（备份成 .retired）与 dist/、build/ 产物
    5. 清掉新状态文件，语法自检

保留：backup/2026-11-14-exe/（原始副本 + 对照差分）、
      docs/audit-2026-11-14-exe.log（验证记录）、各 *.log。
pip 装的 pyinstaller / pywebview 等不自动卸载（可能影响别的项目）。
"""
from __future__ import annotations

import ctypes
import py_compile
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BK = ROOT / "backup" / "2026-11-14-exe"
CREATE_NO_WINDOW = 0x08000000
TRAY_CLASS = "GuanlanTrayWnd"

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]

RESTORE = {
    "launch.py.orig":            "launch.py",
    "tray.py.orig":              "tray.py",
    "native_window.py.orig":      "native_window.py",
    "storage.py.orig":           "storage.py",
    "server.py.orig":            "server.py",
    "bible.py.orig":             "bible.py",
    "reading.py.orig":          "reading.py",
    "cloud_sync.py.orig":        "cloud_sync.py",
    "README.md.orig":            "README.md",
    "launch.vbs.orig":           "launch.vbs",
    "start.cmd.orig":            "start.cmd",
    "verify_tray.py.orig":       "tools/verify_tray.py",
    "rollback_tray.py.orig":    "tools/rollback_tray.py",
    "rollback_native.py.orig":  "tools/rollback_native.py",
}
RETIRE = [
    "entry.py", "app_paths.py", "guanlan.spec",
    "tools/build_exe.py", "tools/install_shortcut.py",
    "tools/rollback_exe.py",
]
STATE_FILES = [".gl-window-hwnd", ".gl-exiting", ".tray-hwnd",
               "tray.pid", "server.pid"]
COMPILE = ["launch.py", "tray.py", "native_window.py", "storage.py",
           "server.py", "tools/verify_tray.py"]


def say(s: str) -> None:
    print(s, flush=True)


def unlink_quietly(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def stop_current() -> None:
    entry = ROOT / "entry.py"
    if entry.exists():
        r = subprocess.run([sys.executable, str(entry), "--stop"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW,
                           timeout=60)
        say("  " + r.stdout.decode("mbcs", "replace").strip().replace("\n", " | "))
    tray = user32.FindWindowW(TRAY_CLASS, None)
    if tray:
        user32.PostMessageW(tray, 0x8002, 0, 0)
        for _ in range(10):
            if not user32.FindWindowW(TRAY_CLASS, None):
                break
            time.sleep(0.1)


def main(argv: list[str]) -> int:
    say("=== 回滚：单入口 exe → 四个独立 .py 源码形态 ===")
    missing = [k for k in RESTORE if not (BK / k).exists()]
    if missing:
        say(f"[ERROR] 缺原始副本：{missing}，未做任何修改")
        return 1

    if "--dry-run" in argv:
        say("[dry-run] 只检查，不改动任何文件")
        for k, rel in RESTORE.items():
            say(f"  OK  {k}  →  {rel}")
        for rel in ("launch.py.orig", "tray.py.orig", "native_window.py.orig",
                    "server.py.orig", "verify_tray.py.orig"):
            src = BK / rel
            try:
                py_compile.compile(str(src), doraise=True,
                                   cfile=str(BK / ("_dry_" + rel + ".pyc")))
                unlink_quietly(BK / ("_dry_" + rel + ".pyc"))
                say(f"  {rel} 编译通过")
            except Exception as e:
                say(f"  [ERROR] {rel} 编译失败：{e!r}")
                return 1
        say(f"  待移走：{', '.join(RETIRE)}")
        say("  待删除：dist/观澜/、dist/观澜.exe、build/、桌面「观澜（应用版）.lnk」")
        say(f"  待清理状态文件：{', '.join(STATE_FILES)}")
        say("=== dry-run 通过，真正回滚请去掉 --dry-run ===")
        return 0

    say("[1] 停掉当前进程")
    try:
        stop_current()
    except Exception as e:
        say(f"  --stop 异常（继续回滚）：{e!r}")

    say("[2] 移除桌面应用版快捷方式（原「观澜.url」保留）")
    tool = ROOT / "tools" / "install_shortcut.py"
    if tool.exists():
        subprocess.run([sys.executable, str(tool), "--remove"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)

    say("[3] 还原改造前的文件")
    for backup, rel in RESTORE.items():
        shutil.copy2(BK / backup, ROOT / rel)
        say(f"  {rel}  ←  {backup}")

    say("[4] 移走本次新增文件（备份成 .retired）")
    for rel in RETIRE:
        p = ROOT / rel
        if p.exists():
            shutil.copy2(p, BK / (p.name + ".retired"))
            say(f"  {rel}  →  backup/2026-11-14-exe/{p.name}.retired")

    say("[5] 清理打包产物与状态文件")
    for p in (ROOT / "dist" / "观澜", ROOT / "build"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            say(f"  删除目录 {p.relative_to(ROOT)}")
    unlink_quietly(ROOT / "dist" / "观澜.exe")
    sys.path.insert(0, str(ROOT))
    try:
        import storage
        for name in STATE_FILES:
            unlink_quietly(storage.DATA_DIR / name)
        say(f"  已清理 {len(STATE_FILES)} 个状态文件")
    except Exception as e:
        say(f"  状态文件清理跳过：{e!r}")

    say("[6] 语法自检")
    for rel in COMPILE:
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
            say(f"  {rel} 编译通过")
        except Exception as e:
            say(f"  [ERROR] {rel} 编译失败：{e!r}")
            return 1

    say("=== 回滚完成：回到四个独立 .py + launch.vbs 源码形态 ===")
    say(f"原始副本 / 对照差分 / 验证记录仍在 {BK.relative_to(ROOT)} 与 docs/")
    say("如需卸载本次 pip 依赖：")
    say("  python -m pip uninstall pyinstaller pywebview pythonnet clr_loader "
        "proxy_tools bottle altgraph pefile pyinstaller-hooks-contrib pywin32-ctypes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
