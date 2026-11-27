#!/usr/bin/env python
"""观澜 · 原生 WebView2 窗口 + 点 × 收进托盘 的端到端验证

跑法（在本目录）：
    python tools/verify_tray.py

做的事：
    1. 清理现场，确认「未运行」
    2. pythonw launch.py 起服务 + 原生宿主窗口 + 托盘守护
    3. 断言窗口类名不是 Chrome_WidgetWin（Edge 不再承载窗口）
    4. 对宿主窗口 PostMessage(WM_CLOSE) —— 与点右上角 × 同一条消息
    5. 断言：窗口同一个 hwnd 仍在、只是 invisible / 服务仍 200 / 托盘仍在
    6. 断言：SW_SHOW 恢复后仍是同一个 hwnd、只有一个窗口
    7. launch.py --stop 收尾，宿主窗口真正销毁，现场恢复「未运行」

结果同时打印到终端并追加到 docs/audit-2026-11-14-native-window.log。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launch                                    # noqa: E402

# 不设 GL_VERIFY_EXE → 测源码单入口 entry.py；
# 设成 exe 绝对路径    → 测 PyInstaller 打包产物。
_EXE = os.environ.get("GL_VERIFY_EXE", "").strip()
if _EXE:
    EXE = str(Path(_EXE).resolve())
    ENTRY = [EXE]                                # 打包形态：观澜.exe
    ENTRY_PY = [EXE]                             # --stop / --status 也走 exe
    MODE = f"exe({Path(EXE).name})"
else:
    ENTRY = [launch.pythonw(), "-u", str(ROOT / "entry.py")]
    ENTRY_PY = [sys.executable, str(ROOT / "entry.py")]
    MODE = "entry.py(源码)"

SW_SHOW = 5
WM_CLOSE = 0x0010
CREATE_NO_WINDOW = 0x08000000
AUDIT = ROOT / "docs" / ("audit-2026-11-14-exe.log" if _EXE
                        else "audit-2026-11-14-native-window.log")

lines: list[str] = []
passed = 0
failed = 0


def say(s: str) -> None:
    print(s, flush=True)
    lines.append(s)


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        say(f"  [PASS] {name}" + (f" · {detail}" if detail else ""))
    else:
        failed += 1
        say(f"  [FAIL] {name}" + (f" · {detail}" if detail else ""))
    return ok


def wait_for(pred, seconds: float, step: float = 0.2):
    deadline = time.time() + seconds
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def file_hwnd() -> int:
    try:
        return int(launch.GL_HWNDFILE.read_text(encoding="ascii").strip())
    except Exception:
        return 0


def webview_child(hwnd: int) -> int:
    """窗口里出现 Chrome_WidgetWin 子窗口 = WebView2 控件真的初始化了。

    只有 WinForms 外框而页面没渲染时，子窗口里不会有 Chromium 窗口。
    这条断言用来挡住「窗口开出来了但是一片白」的情况。
    """
    hits: list[int] = []

    def cb(h, _l):
        if "Chrome_WidgetWin" in launch._class(h):
            hits.append(h)
            return False
        return True

    launch.user32.EnumChildWindows(hwnd, launch.WNDENUMPROC(cb), 0)
    return hits[0] if hits else 0


def count_visible_windows() -> int:
    hits: list[int] = []

    def cb(hwnd, _l):
        if not launch.user32.IsWindowVisible(hwnd):
            return True
        if launch._is_guanlan_title(hwnd):
            hits.append(hwnd)
        return True

    launch.user32.EnumWindows(launch.WNDENUMPROC(cb), 0)
    return len(hits)


def run():
    say(f"=== 观澜验证 [{MODE}] {datetime.now():%Y-%m-%d %H:%M:%S} ===")

    say("[0] 清理现场")
    subprocess.run([*ENTRY_PY, "--stop"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    time.sleep(1.2)
    check("起始状态：服务未运行", not launch.port_open())
    check("起始状态：无观澜窗口", not launch.find_window(include_hidden=True))

    say("[1] 启动 launch.py")
    # 用 Popen 而不是 run()：服务万一没起来，launch.py 会弹一个模态
    # MessageBox 卡住，run() 会被一路拖到超时。这里改成轮询 + 重试。
    # （托管 CLI 的 sitecustomize 偶发 SAFE_DELETE_BULK_GUARD_ERROR 会让
    #   server.py 启动失败；普通桌面双击图标没有这层 shim。）
    def launch_once():
        p = subprocess.Popen(ENTRY, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # --onefile 每个角色进程都要解压一遍 41 MiB，启动比源码慢
        if wait_for(lambda: launch.http_ready(0.8), 25 if _EXE else 12):
            return p
        if p.poll() is None:
            p.kill()
        return None

    proc = None
    for attempt in (1, 2):
        proc = launch_once()
        if proc:
            break
        say(f"  第 {attempt} 次启动未就绪，重试")
        subprocess.run([*ENTRY_PY, "--stop"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        time.sleep(1.0)
    check("服务成功拉起", proc is not None)
    if not proc:
        return

    tray = wait_for(lambda: launch.tray_running(), 20)
    check("托盘守护窗口已建立", bool(tray), f"hwnd={tray}")
    hwnd = wait_for(lambda: launch.find_window(include_hidden=False), 30)
    check("观澜宿主窗口已打开", bool(hwnd), f"hwnd={hwnd}")
    check("服务 HTTP 就绪", launch.http_ready(2.0))
    if not hwnd:
        return

    cls = launch._class(hwnd)
    check("窗口不是 Edge/Chromium 窗口", not cls.startswith("Chrome_WidgetWin"),
          f"class={cls}")
    # shown 事件比窗口可见稍晚一点，等它把 hwnd 落盘
    wrote = wait_for(lambda: file_hwnd() == hwnd, 8, 0.2)
    check("宿主写了 .gl-window-hwnd", bool(wrote),
          f"file={file_hwnd()} hwnd={hwnd}")
    child = wait_for(lambda: webview_child(hwnd), 18, 0.3)
    check("WebView2 渲染子窗口已创建（页面真的加载，不是白屏）",
          bool(child), f"child_hwnd={child}")

    say("[2] 模拟点右上角 × （PostMessage WM_CLOSE）")
    launch.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    hidden = wait_for(lambda: launch.user32.IsWindow(hwnd) and
                       not launch.user32.IsWindowVisible(hwnd), 15, 0.2)
    check("窗口隐藏但未被销毁（同一个 hwnd）", bool(hidden),
          f"hwnd={hwnd} iswindow={launch.user32.IsWindow(hwnd)}")
    check("没有可见的观澜窗口", not launch.find_window(include_hidden=False))
    check("服务仍在后台运行", launch.http_ready(2.0))
    check("托盘图标仍在", bool(launch.tray_running()))
    check(".gl-window-hwnd 仍指向同一个窗口", file_hwnd() == hwnd)

    say("[3] 从托盘恢复")
    launch.user32.ShowWindow(hwnd, SW_SHOW)
    launch.focus_window(hwnd)
    time.sleep(0.4)
    check("窗口重新可见", bool(launch.find_window(include_hidden=False)),
          f"hwnd={launch.find_window(include_hidden=False)}")
    check("恢复的是同一个 hwnd（页面零重载）",
          launch.find_window(include_hidden=False) == hwnd)
    check("只有一个观澜窗口", count_visible_windows() == 1,
          f"count={count_visible_windows()}")

    say("[4] --stop 收尾，恢复原状")
    r = subprocess.run([*ENTRY_PY, "--stop"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    say("  " + r.stdout.decode("mbcs", "replace").strip().replace("\n", " | "))
    check("服务已停止", not launch.port_open())
    check("托盘已撤销", not launch.tray_running())
    gone = wait_for(lambda: not launch.user32.IsWindow(hwnd), 8, 0.2)
    check("宿主窗口真正关闭（IsWindow=0）", bool(gone))
    check("没有残留的观澜窗口", not launch.find_window(include_hidden=True))
    check(".gl-exiting 标志已清理", not launch.EXITFLAG.exists())

    say(f"=== 结果：{passed} PASS / {failed} FAIL ===")
    return failed == 0


if __name__ == "__main__":
    ok = False
    try:
        ok = bool(run())
    except Exception as e:
        say(f"[ERROR] {e!r}")
        import traceback
        say(traceback.format_exc())
    finally:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    raise SystemExit(0 if ok else 1)
