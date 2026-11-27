#!/usr/bin/env python
"""回滚「原生 WebView2 窗口」改造，回到 Edge --app + 预热托盘的上一版。

跑法（在本目录）：
    python tools/rollback_native.py --dry-run     只检查，不改任何文件
    python tools/rollback_native.py               真正回滚

做的事：
    1. 先用当前 launch.py --stop 收干净：撤托盘、关宿主窗口、停服务
    2. 用 backup/2026-11-14-native-window/ 里的精确副本还原 5 个文件
    3. 移走本次新增的 native_window.py 与本回滚脚本（备份留 .retired）
    4. 清掉 .gl-window-hwnd / .gl-exiting 等新状态文件
    5. 语法自检

保留不动：backup/2026-11-14-native-window/（原始副本 + 对照差分）、
          docs/audit-2026-11-14-native-window.log（验证记录）、各 *.log。
pip 装的 pywebview / pythonnet 不自动卸载（可能影响别的项目），
如需清理：python -m pip uninstall pywebview pythonnet clr_loader bottle proxy_tools
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
BK = ROOT / "backup" / "2026-11-14-native-window"
sys.path.insert(0, str(ROOT))
import storage                                   # noqa: E402  本次没改过

DATA = storage.DATA_DIR
CREATE_NO_WINDOW = 0x08000000
TRAY_CLASS = "GuanlanTrayWnd"

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]

# 备份文件名 → 项目里的相对路径
RESTORE = {
    "launch.py.orig":          "launch.py",
    "tray.py.orig":            "tray.py",
    "README.md.orig":         "README.md",
    "verify_tray.py.orig":     "tools/verify_tray.py",
    "rollback_tray.py.orig":  "tools/rollback_tray.py",
}
RETIRE = ["native_window.py", "tools/rollback_native.py"]
STATE_FILES = [".gl-window-hwnd", ".gl-exiting", ".tray-hwnd", "tray.pid"]


def say(s: str) -> None:
    print(s, flush=True)


def unlink_quietly(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def stop_current() -> None:
    """用改造后的 launch.py 收干净，再用 Win32 兜底撤残留托盘。"""
    r = subprocess.run([sys.executable, str(ROOT / "launch.py"), "--stop"],
                        capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=60)
    say("  " + r.stdout.decode("mbcs", "replace").strip().replace("\n", " | "))
    tray = user32.FindWindowW(TRAY_CLASS, None)
    if tray:
        user32.PostMessageW(tray, 0x8002, 0, 0)   # WM_TRAY_QUIT
        for _ in range(10):
            if not user32.FindWindowW(TRAY_CLASS, None):
                break
            time.sleep(0.1)


def main(argv: list[str]) -> int:
    say("=== 回滚：原生 WebView2 窗口 → Edge --app ===")
    missing = [k for k in RESTORE if not (BK / k).exists()]
    if missing:
        say(f"[ERROR] 缺原始副本：{missing}，未做任何修改")
        return 1

    if "--dry-run" in argv:
        say("[dry-run] 只检查，不改动任何文件")
        for k, rel in RESTORE.items():
            p = BK / k
            say(f"  OK  {p.relative_to(ROOT)}  →  {rel}")
        for rel in ("launch.py.orig", "tray.py.orig", "verify_tray.py.orig"):
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
        say(f"  待清理状态文件：{', '.join(STATE_FILES)}")
        say("=== dry-run 通过，真正回滚请去掉 --dry-run ===")
        return 0

    say("[1] 停掉当前进程（托盘 / 宿主窗口 / 服务）")
    try:
        stop_current()
    except Exception as e:
        say(f"  --stop 异常（继续回滚）：{e!r}")

    say("[2] 还原改造前的文件")
    for backup, rel in RESTORE.items():
        dst = ROOT / rel
        shutil.copy2(BK / backup, dst)
        say(f"  {rel}  ←  {backup}")

    say("[3] 移走本次新增文件（备份成 .retired）")
    for rel in RETIRE:
        p = ROOT / rel
        if p.exists():
            dst = BK / (p.name + ".retired")
            shutil.copy2(p, dst)
            say(f"  {rel}  →  {dst.relative_to(ROOT)}")

    say("[4] 清理新状态文件")
    for name in STATE_FILES:
        unlink_quietly(DATA / name)
        say(f"  {name}")

    say("[5] 语法自检")
    for rel in ("launch.py", "tray.py", "tools/verify_tray.py"):
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
            say(f"  {rel} 编译通过")
        except Exception as e:
            say(f"  [ERROR] {rel} 编译失败：{e!r}")
            return 1

    say("=== 回滚完成：窗口回到 Edge --app + 预热托盘版本 ===")
    say(f"原始副本 / 对照差分 / 验证记录仍在 {BK.relative_to(ROOT)} 与 docs/")
    say("如需卸载本次 pip 依赖：")
    say("  python -m pip uninstall pywebview pythonnet clr_loader bottle proxy_tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
