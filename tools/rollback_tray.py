#!/usr/bin/env python
"""回滚「关闭按钮 = 最小化到托盘」这次改动，恢复成改动前的状态。

跑法（在本目录）：
    python tools/rollback_tray.py

做的事：
    1. 先优雅撤掉托盘守护（发 WM_TRAY_QUIT 撤图标；1 秒后仍在才强杀）
    2. 把隐藏中的观澜窗口恢复显示，别在桌面上留隐形窗口
    3. 用 backup/2026-11-14-tray/ 里的精确副本还原 launch.py 和 README.md
    4. 移走 tray.py 和 tools/verify_tray.py（备份在 backup/2026-11-14-tray/）
    5. 清掉 data/tray.pid、data/.tray-hwnd
    6. 语法自检

保留不动：backup/2026-11-14-tray/（原始副本 + 对照差分）、
          docs/audit-2026-11-14-tray.log（验证记录）、data/tray.log（运行日志）。
服务进程不主动停：回滚只撤托盘功能，`start.cmd stop` 才是停服务。
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
BK = ROOT / "backup" / "2026-11-14-tray"
sys.path.insert(0, str(ROOT))
import storage                                   # noqa: E402  这个文件本次没改过

DATA = storage.DATA_DIR
TRAY_CLASS = "GuanlanTrayWnd"
WM_TRAY_QUIT = 0x8002
SW_SHOW = 5
CREATE_NO_WINDOW = 0x08000000

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]


def say(s: str) -> None:
    print(s, flush=True)


def read_hidden_hwnd() -> int:
    try:
        return int((DATA / ".tray-hwnd").read_text(encoding="ascii").strip())
    except Exception:
        return 0


def is_our_tray(pid: int) -> bool:
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, timeout=15,
            creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return False
    norm = raw.decode("mbcs", "replace").lower().replace("\\", "/")
    target = str(ROOT / "tray.py").lower().replace("\\", "/")
    return target in norm or ("tray.py" in norm and "guanlan" in norm)


def stop_tray() -> None:
    hidden = read_hidden_hwnd()          # 守护退出时会删这个文件，先读
    hwnd = user32.FindWindowW(TRAY_CLASS, None)
    if hwnd:
        say(f"  通知托盘守护退出 hwnd={hwnd}")
        user32.PostMessageW(hwnd, WM_TRAY_QUIT, 0, 0)
        for _ in range(12):
            if not user32.FindWindowW(TRAY_CLASS, None):
                break
            time.sleep(0.1)
    else:
        say("  托盘守护未在运行")

    try:
        pid = int((DATA / "tray.pid").read_text(encoding="ascii").strip())
    except Exception:
        pid = 0
    if pid and is_our_tray(pid):
        say(f"  强杀残留守护 PID={pid}")
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)

    if hidden and user32.IsWindow(hidden):
        user32.ShowWindow(hidden, SW_SHOW)
        say(f"  已恢复隐藏窗口 hwnd={hidden}")
    for name in ("tray.pid", ".tray-hwnd"):
        (DATA / name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    say("=== 回滚：关闭按钮最小化到托盘 ===")
    dry = "--dry-run" in (argv or [])
    if not (BK / "launch.py.orig").exists():
        say(f"[ERROR] 找不到原始副本 {BK / 'launch.py.orig'}，已停止，未做任何修改")
        return 1

    if dry:
        say("[dry-run] 只检查，不改动任何文件")
        for rel in ("launch.py.orig", "README.md.orig", "tray.py.bak",
                    "launch.py.diff"):
            p = BK / rel
            say(f"  {'OK' if p.exists() else 'MISSING'}  {p.relative_to(ROOT)}")
        orig = BK / "launch.py.orig"
        say(f"  原始 launch.py：{len(orig.read_text(encoding='utf-8').splitlines())} 行")
        try:
            py_compile.compile(str(orig), doraise=True,
                               cfile=str(BK / "_dryrun.pyc"))
            (BK / "_dryrun.pyc").unlink(missing_ok=True)
            say("  原始 launch.py 编译通过")
        except Exception as e:
            say(f"  [ERROR] 原始 launch.py 编译失败：{e!r}")
            return 1
        say(f"  待还原：launch.py / README.md；待移走：tray.py / tools/verify_tray.py")
        say(f"  当前托盘守护：{'运行中' if user32.FindWindowW(TRAY_CLASS, None) else '未运行'}")
        say("=== dry-run 检查通过，真正回滚请去掉 --dry-run ===")
        return 0

    say("[1] 撤托盘守护")
    stop_tray()

    say("[2] 还原 launch.py / README.md")
    shutil.copy2(BK / "launch.py.orig", ROOT / "launch.py")
    shutil.copy2(BK / "README.md.orig", ROOT / "README.md")
    say(f"  launch.py  {len((ROOT / 'launch.py').read_text(encoding='utf-8').splitlines())} 行")

    say("[3] 移走本次新增文件（备份保留在 backup/2026-11-14-tray/）")
    for rel in ("tray.py", "tools/verify_tray.py"):
        p = ROOT / rel
        if p.exists():
            dst = BK / (p.name + ".retired")
            shutil.copy2(p, dst)
            p.unlink()
            say(f"  {rel} → {dst.relative_to(ROOT)}")

    say("[4] 语法自检")
    try:
        py_compile.compile(str(ROOT / "launch.py"), doraise=True)
        say("  launch.py 编译通过")
    except Exception as e:
        say(f"  [ERROR] 编译失败：{e!r}")
        return 1
    r = subprocess.run([sys.executable, str(ROOT / "launch.py"), "--status"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    say("  " + r.stdout.decode("mbcs", "replace").strip())

    say("=== 回滚完成 ===")
    say("服务进程未动；如需彻底停止：start.cmd stop")
    say(f"原始副本 / 对照差分 / 验证记录仍保留在 {BK.relative_to(ROOT)} 与 docs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
