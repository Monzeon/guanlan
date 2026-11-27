#!/usr/bin/env python
"""验证桌面「观澜」原图标（观澜.url → launch.vbs）现在能打开应用。

    python tools/verify_desktop.py

模拟真实双击：用 Windows Script Host 运行 launch.vbs，然后检查服务、
托盘、宿主窗口、WebView2 渲染子窗口，并确认 entry.log 里本次 launcher
是 frozen=True（走打包 exe），而不是被 PATH 里缺依赖的 Python 3.10 接管。
最后 --stop 收尾，恢复「未运行」。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launch                                    # noqa: E402
import storage                                   # noqa: E402

CREATE_NO_WINDOW = 0x08000000
passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}" + (f" · {detail}" if detail else ""), flush=True)
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" · {detail}" if detail else ""), flush=True)


def wait_for(pred, seconds: float, step: float = 0.25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def webview_child(hwnd: int) -> int:
    hits: list[int] = []

    def cb(h, _l):
        if "Chrome_WidgetWin" in launch._class(h):
            hits.append(h)
            return False
        return True

    launch.user32.EnumChildWindows(hwnd, launch.WNDENUMPROC(cb), 0)
    return hits[0] if hits else 0


def main() -> int:
    print("=== 桌面原图标验证 ===", flush=True)
    entry_log = storage.DATA_DIR / "entry.log"
    native_log = storage.DATA_DIR / "native.log"
    before = len(entry_log.read_text(encoding="utf-8", errors="replace").splitlines()) \
        if entry_log.exists() else 0
    native_before = len(native_log.read_text(encoding="utf-8", errors="replace").splitlines()) \
        if native_log.exists() else 0

    print("[0] 清理现场", flush=True)
    subprocess.run([sys.executable, str(ROOT / "entry.py"), "--stop"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    time.sleep(1.2)
    check("起始状态：服务未运行", not launch.port_open())

    print("[1] 模拟双击桌面「观澜」（wscript launch.vbs）", flush=True)
    mode = "wscript"
    try:
        p = subprocess.Popen(["wscript.exe", str(ROOT / "launch.vbs")],
                             cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  wscript pid={p.pid}", flush=True)
        p.wait(timeout=15)
    except Exception as e:
        print(f"  wscript 不可用（{e!r}），退而直接启动 exe", flush=True)
        mode = "exe"
        subprocess.Popen([str(ROOT / "dist" / "观澜" / "观澜.exe")],
                         cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    tray = wait_for(lambda: launch.tray_running(), 25)
    check("托盘守护已建立", bool(tray), f"hwnd={tray}")
    hwnd = wait_for(lambda: launch.find_window(include_hidden=False), 35)
    check("观澜宿主窗口已打开", bool(hwnd), f"hwnd={hwnd}")
    check("服务 HTTP 就绪", launch.http_ready(2.0))
    if hwnd:
        check("窗口不是 Edge/Chromium 窗口",
              not launch._class(hwnd).startswith("Chrome_WidgetWin"),
              f"class={launch._class(hwnd)}")
        child = wait_for(lambda: webview_child(hwnd), 18, 0.3)
        check("WebView2 渲染子窗口已创建（不是白屏）", bool(child),
              f"child={child}")

    # 关键证据：本次 launcher 应该是 frozen=True（打包 exe），
    # 而不是 PATH 里那个没装 pywebview 的 Python 3.10。
    lines = entry_log.read_text(encoding="utf-8", errors="replace").splitlines()[before:] \
        if entry_log.exists() else []
    launchers = [l for l in lines if "role=launcher" in l and "argv=[]" in l]
    frozen = [l for l in launchers if "frozen=True" in l]
    check("本次走打包 exe（frozen=True），不是系统 Python",
          bool(frozen), frozen[-1][:150] if frozen else (launchers[-1][:150] if launchers else "无记录"))
    # 只看本轮新增行；native.log 末尾可能还留着上次 Python 3.10 的旧报错
    new_native = "\n".join(
        native_log.read_text(encoding="utf-8", errors="replace").splitlines()[native_before:]) \
        if native_log.exists() else ""
    check("本轮没有 webview 导入失败",
          "No module named 'webview'" not in new_native)

    print("[2] --stop 收尾", flush=True)
    subprocess.run([sys.executable, str(ROOT / "entry.py"), "--stop"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    time.sleep(0.8)
    check("服务已停止", not launch.port_open())
    check("托盘已撤销", not launch.tray_running())
    if hwnd:
        gone = wait_for(lambda: not launch.user32.IsWindow(hwnd), 8, 0.2)
        check("宿主窗口已关闭", bool(gone))
    print(f"=== 结果：{passed} PASS / {failed} FAIL（模式 {mode}）===", flush=True)
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
