#!/usr/bin/env python
"""观澜 GUANLAN · 原生窗口宿主（WebView2 嵌入）

这一层取代了旧的 ``msedge.exe --app=...``：窗口由本进程创建（WinForms
BrowserForm），渲染用系统已安装的 WebView2 Runtime 作为**控件**嵌进来。
于是：

    * 进程是观澜自己的，不再是浏览器主进程；没有地址栏 / 标签栏 / 浏览器菜单
    * WM_CLOSE 走的是我们自己的 closing 事件 —— 点 × 直接 hide()，
      窗口对象、页面 DOM、滚动位置、未提交的输入全部原样保留
    * 托盘恢复就是 SW_SHOW，瞬时，零重载（上一版「关掉再预热隐藏窗口」
      那套 workaround 整个删掉了）

与托盘守护的通信走两条：
    * 建窗后把 hwnd 写进 data/.gl-window-hwnd，托盘据此接管
    * 点 × 时给托盘隐藏窗口 PostMessage(WM_TRAY_MINIMIZED)，托盘弹气泡
    * 托盘「退出观澜」/ launch.py --stop 写 data/.gl-exiting，
      closing 见到这个标志才放行，真正销毁窗口

注意 storage_path 必须是纯 ASCII：WebView2 的 user data folder 遇到
中文临时目录会报 E_ABORT（本机 %TEMP% 带中文用户名，实测过）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

import app_paths                             # 提前：等随包 DLL 落盘要用 RES_DIR

# pythonnet 3.x 默认在 Windows 走 coreclr / hostfxr，需要
# Python.Runtime.runtimeconfig.json —— 这个 wheel 里根本没带，PyInstaller
# 冻结后 hostfxr 直接报 COR_E_INVALIDOPERATION (0x80131509)。
# 改成系统自带的 .NET Framework，并且在 import webview 之前就显式把
# pythonnet 加载好：clr.py 里那句 load() 见到 _LOADED 会直接返回。
os.environ["PYTHONNET_RUNTIME"] = "netfx"

# --onefile 下每个角色进程各自解压 41 MiB；四个进程并发启动时，
# data 文件可能还没落盘，cffi 加载 ClrLoader.dll 会报 0x7e。
# 先等关键 DLL 出现再初始化 CLR（实测单进程启动能过、并发启动会踩到）。
_res = app_paths.RES_DIR
_clr_dll = _res / "clr_loader" / "ffi" / "dlls" / "amd64" / "ClrLoader.dll"
_core_dll = _res / "webview" / "lib" / "Microsoft.Web.WebView2.Core.dll"
if app_paths.FROZEN:
    for _ in range(80):
        if _clr_dll.is_file() and _core_dll.is_file():
            break
        time.sleep(0.125)
try:
    import pythonnet
    pythonnet.load("netfx")
    print("[guanlan] CLR runtime = netfx (preloaded)", flush=True)
except Exception as e:                       # 失败也让 pywebview 自己再试一次
    print(f"[guanlan] netfx 预加载失败: {e!r}", flush=True)

import webview

import storage

ROOT = app_paths.RES_DIR                        # 图标在随包资源目录里
DATA = storage.DATA_DIR
PORT = int(os.environ.get("PORT") or 8790)
URL = f"http://127.0.0.1:{PORT}/"
TITLE = "观澜"
ICO = ROOT / "app" / "guanlan.ico"

HWNDFILE = DATA / ".gl-window-hwnd"
EXITFLAG = DATA / ".gl-exiting"
TRAY_CLASS = "GuanlanTrayWnd"
WM_TRAY_MINIMIZED = 0x8003
SW_HIDE = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]


def profile_dir() -> Path:
    """WebView2 的 user data folder，保证纯 ASCII。"""
    p = storage.CACHE_DIR / "webview2"
    if not str(p).isascii():
        # 数据目录必须落在 exe 所在盘，不能放 _MEIPASS 临时解压目录
        p = app_paths.APP_DIR / "cache" / "webview2"   # D:/guanlan/... 全 ASCII
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_hwnd(hwnd: int) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    HWNDFILE.write_text(str(hwnd), encoding="ascii")


def tell_tray(hwnd: int) -> None:
    tray = user32.FindWindowW(TRAY_CLASS, None)
    if tray:
        user32.PostMessageW(tray, WM_TRAY_MINIMIZED, hwnd, 0)


def fix_interop_paths() -> None:
    """让 pywebview 在冻结形态下找到随包的 WebView2 程序集。

    ``webview.util.interop_dll_path`` 依次按
    ``__file__/lib`` → ``argv[0]`` → ``sys._MEIPASS`` 根目录找 DLL，
    PyInstaller 的 hook 却把 DLL 放在 ``_MEIPASS/webview/lib/``。
    实测这三条分支都对不上，直接抛 FileNotFoundError。

    两层处理：
      1) 把 ``webview/lib`` 下的托管 DLL 平铺到 ``_MEIPASS`` 根（临时目录可写），
         把各平台 WebView2Loader.dll 目录也平铺成 ``_MEIPASS/win-x64`` 等；
      2) 在 edgechromium 被 import 之前替换 interop_dll_path，优先按
         RES_DIR 下的实际路径解析，orig 保留作兜底。
    """
    import shutil
    import sys
    import webview.util as wu

    root = app_paths.RES_DIR
    lib = root / "webview" / "lib"
    dlls = ("Microsoft.Web.WebView2.Core.dll",
            "Microsoft.Web.WebView2.WinForms.dll",
            "WebBrowserInterop.x64.dll", "WebBrowserInterop.x86.dll")
    plats = ("win-arm64", "win-x64", "win-x86")

    for name in dlls:
        src, dst = lib / name, root / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"[guanlan] 平铺 {name} 失败: {e!r}", flush=True)
    for plat in plats:
        src, dst = lib / "runtimes" / plat / "native", root / plat
        if src.is_dir() and not dst.is_dir():
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            except Exception as e:
                print(f"[guanlan] 平铺 {plat} 失败: {e!r}", flush=True)
    # .NET 的 CreateCoreWebView2Environment 用 LoadLibrary("WebView2Loader.dll")
    # 找原生 loader：最稳的是让它和 Microsoft.Web.WebView2.Core.dll 同目录。
    # PATH 在 netfx 预加载之后才被 edgechromium.py 追加，CLR 不一定认，
    # 所以别只靠 PATH，直接复制到 RES_DIR 根。
    loader_dst = root / "WebView2Loader.dll"
    if not loader_dst.exists():
        for src in (root / "win-x64" / "WebView2Loader.dll",
                    lib / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll",
                    lib / "WebView2Loader.dll"):
            if src.is_file():
                try:
                    shutil.copy2(src, loader_dst)
                    break
                except Exception as e:
                    print(f"[guanlan] 平铺 WebView2Loader.dll 失败: {e!r}", flush=True)
    print(f"[guanlan] root_loader={loader_dst.exists()}", flush=True)

    print(f"[guanlan] _MEIPASS={getattr(sys, '_MEIPASS', None)!r} "
          f"RES_DIR={root} lib_dir={lib.is_dir()} "
          f"root_dll={ (root / dlls[0]).exists() } "
          f"loader={ (root / 'win-x64' / 'WebView2Loader.dll').exists() }",
          flush=True)
    orig = wu.interop_dll_path

    def patched(name: str) -> str:
        # edgechromium.py 会把 win-arm64 / win-x64 / win-x86 三个目录依次
        # 追加进 PATH。包里未必三个平台都齐（arm64 常被 hook 漏掉），
        # .orig 遇到不存在的那个会直接抛 FileNotFoundError 打断整个
        # import —— 这里对平台名一律返回一个确实存在的目录。
        if name in plats:
            for c in (lib / "runtimes" / name / "native", root / name):
                if c.is_dir():
                    return str(c)
            return str(root)
        for c in (root / name, lib / name, root / name / "native"):
            if c.exists():
                return str(c)
        return orig(name)

    wu.interop_dll_path = patched


def main() -> int:
    fix_interop_paths()
    DATA.mkdir(parents=True, exist_ok=True)
    # 上次异常退出可能留下标志；新进程一律按「还没有退出意图」开始
    try:
        if EXITFLAG.exists():
            EXITFLAG.unlink()
    except Exception:
        pass

    win = webview.create_window(
        TITLE, URL, width=1280, height=860, min_size=(900, 600),
        background_color="#F5F3EE", text_select=True,
    )

    def on_shown() -> None:
        # shown 在 GUI 线程触发，此时 native.Handle 已经可用。
        # 之前在窗口 ready 之前碰它会让 WebView2 初始化 E_ABORT。
        try:
            hwnd = int(win.native.Handle.ToInt64())
        except Exception:
            hwnd = user32.FindWindowW(None, TITLE)
        # 托盘靠 .gl-window-hwnd 自己接管窗口；这里不发 WM_TRAY_MINIMIZED，
        # 否则每次开窗都会误报一次「用户点了 ×」。
        write_hwnd(hwnd)

    def on_closing() -> bool:
        if EXITFLAG.exists():
            return True                  # 真退出：放行 WM_CLOSE
        # 用 Win32 SW_HIDE 而不是 win.hide()：托盘恢复走的是另一个进程的
        # ShowWindow(SW_SHOW)，两边对称，WinForms 的 Visible 状态不会被搅乱。
        # 实测：窗口 iswindow=1 / visible=0，恢复后 same_hwnd=True。
        try:
            hwnd = int(win.native.Handle.ToInt64())
        except Exception:
            hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            already_hidden = not user32.IsWindowVisible(hwnd)
            user32.ShowWindow(hwnd, SW_HIDE)
            if not already_hidden:      # WinForms 可能连发两次 closing，去重
                tell_tray(hwnd)
        else:
            win.hide()
        return False                     # 吃掉关闭 = 最小化到托盘

    win.events.shown += on_shown
    win.events.closing += on_closing

    webview.start(private_mode=False, storage_path=str(profile_dir()),
                  icon=str(ICO) if ICO.exists() else None)

    # 走到这里说明窗口真的关了；清掉自己的状态文件
    for p in (HWNDFILE, EXITFLAG):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
