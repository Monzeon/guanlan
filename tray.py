#!/usr/bin/env python
"""观澜 GUANLAN · 托盘守护（原生窗口版）

窗口现在是观澜自己的 WebView2 宿主（native_window.py）。点 × 时由宿主
的 closing 事件直接 SW_HIDE，窗口对象、DOM、滚动位置、未提交的输入
全部原样保留。所以本进程只负责三件事：托盘图标、右键菜单、气泡提示。

Edge --app 时代那套「巡检窗口 → 关掉后重开一个隐藏窗口预热 → 状态迁移
判定」整段删掉了：窗口不再被销毁，也就没有需要补救的事。

与宿主的通信（全部是消息，零轮询）：
    宿主建窗      → 写 data/.gl-window-hwnd
    宿主点 ×      → PostMessage(本窗口, WM_TRAY_MINIMIZED, hwnd, 0)
    托盘「显示」  → Win32 ShowWindow(hwnd, SW_SHOW) + focus_window
    托盘「退出」  → 写 data/.gl-exiting + PostMessage(宿主, WM_CLOSE)
    launch --stop → PostMessage(本窗口, WM_TRAY_QUIT)

运行：
    pythonw tray.py                 常驻（launch.py 自动拉起）
    python  tray.py --selftest     只自检 Win32 托盘链路，随即自动退出
    python  tray.py --status        打印当前守护状态
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

import launch                                    # 复用窗口查找 / 停服务
import storage
import app_paths

ROOT = app_paths.RES_DIR                        # 图标在随包资源目录里
DATA = storage.DATA_DIR
PIDFILE = DATA / "tray.pid"
HWNDFILE = DATA / ".tray-hwnd"                  # 托盘自己记的一份（兼容旧调用方）
GL_HWNDFILE = DATA / ".gl-window-hwnd"           # 宿主写的权威 hwnd
EXITFLAG = DATA / ".gl-exiting"
LOG = DATA / "tray.log"
ICOPATH = ROOT / "app" / "guanlan.ico"

TRAY_CLASS = "GuanlanTrayWnd"                    # launch.py --stop 靠这个类名找本进程
TRAY_TITLE = "观澜 · 托盘守护"

# ── Win32 常量 ────────────────────────────────────────
WM_NULL          = 0x0000
WM_DESTROY      = 0x0002
WM_CLOSE        = 0x0010
WM_APP          = 0x8000
WM_TRAY         = WM_APP + 1                   # 托盘图标回调
WM_TRAY_QUIT    = WM_APP + 2                   # launch.py --stop 发来的优雅退出
WM_TRAY_MINIMIZED = WM_APP + 3                  # 宿主通知「用户点了 ×」
WM_LBUTTONUP    = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP    = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01

SW_HIDE, SW_SHOW = 0, 5
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_LEFTALIGN, TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0, 0x0002, 0x0100, 0x0080

ID_SHOW, ID_HIDE, ID_SEP, ID_QUIT = 1, 2, 3, 4

IMAGE_ICON      = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE  = 0x0040

user32   = ctypes.WinDLL("user32", use_last_error=True)
shell32  = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", wintypes.UINT),          # union { uTimeout; uVersion; }
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),     # V3 起才有；这里只用 V2 大小
        ("hBalloonIcon", ctypes.c_void_p),
    ]


# NOTIFYICONDATA_V2_SIZE：到 dwInfoFlags 结束。XP~Win11 都能正确解析气泡字段。
NID_V2_SIZE = NOTIFYICONDATAW.dwInfoFlags.offset + ctypes.sizeof(wintypes.DWORD)


def _bind(fn, restype, argtypes) -> None:
    fn.restype = restype
    fn.argtypes = argtypes


# 64 位下句柄是 8 字节，不声明 argtypes 会被 ctypes 截成 int → 图标建不起来。
_bind(user32.RegisterClassExW, wintypes.ATOM, [ctypes.POINTER(WNDCLASSEXW)])
_bind(user32.CreateWindowExW, wintypes.HWND,
      [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       wintypes.HWND, ctypes.c_void_p, wintypes.HINSTANCE, ctypes.c_void_p])
_bind(user32.DefWindowProcW, LRESULT,
      [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM])
_bind(user32.DestroyWindow, wintypes.BOOL, [wintypes.HWND])
_bind(user32.FindWindowW, wintypes.HWND, [wintypes.LPCWSTR, wintypes.LPCWSTR])
_bind(user32.PostMessageW, wintypes.BOOL,
      [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM])
_bind(user32.ShowWindow, wintypes.BOOL, [wintypes.HWND, ctypes.c_int])
_bind(user32.IsWindow, wintypes.BOOL, [wintypes.HWND])
_bind(user32.IsWindowVisible, wintypes.BOOL, [wintypes.HWND])
_bind(user32.SetForegroundWindow, wintypes.BOOL, [wintypes.HWND])
_bind(user32.GetMessageW, ctypes.c_int,
      [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT])
_bind(user32.TranslateMessage, wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)])
_bind(user32.DispatchMessageW, LRESULT, [ctypes.POINTER(wintypes.MSG)])
_bind(user32.PostQuitMessage, None, [ctypes.c_int])
_bind(user32.LoadImageW, ctypes.c_void_p,
      [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
       ctypes.c_int, ctypes.c_int, wintypes.UINT])
_bind(user32.DestroyIcon, wintypes.BOOL, [ctypes.c_void_p])
_bind(user32.CreatePopupMenu, ctypes.c_void_p, [])
_bind(user32.AppendMenuW, wintypes.BOOL,
      [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR])
_bind(user32.DestroyMenu, wintypes.BOOL, [ctypes.c_void_p])
_bind(user32.TrackPopupMenu, ctypes.c_int,
      [ctypes.c_void_p, wintypes.UINT, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, wintypes.HWND, ctypes.c_void_p])
_bind(user32.GetCursorPos, wintypes.BOOL, [ctypes.POINTER(POINT)])
_bind(shell32.Shell_NotifyIconW, wintypes.BOOL,
      [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)])


# ── 全局状态 ──────────────────────────────────────────
_hwnd: int = 0                 # 本守护的隐藏消息窗口
_hicon: int = 0                # 托盘图标句柄
_nid: NOTIFYICONDATAW | None = None
_known: int = 0                # 当前接管的宿主窗口（可能处于隐藏态）
_wndproc_keep: WNDPROC | None = None      # 防止回调被 GC
_selftest: bool = False
_icon_added: bool = False
_cleaned: bool = False


def log(msg: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def unlink_quietly(path: Path) -> None:
    """删状态文件；某些托管环境把 Path.unlink 换成移入回收站，
    文件已不在时连 missing_ok=True 都抛，这里整段吞掉。"""
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        log(f"删 {path.name} 失败（已忽略）：{e!r}")


def _is_guanlan(hwnd: int) -> bool:
    """只认标题，不再认 Chrome_WidgetWin 类名（窗口已经是 WinForms 了）。"""
    return bool(hwnd) and user32.IsWindow(hwnd) and launch._is_guanlan_title(hwnd)


def _write_hwnd(hwnd: int) -> None:
    HWNDFILE.write_text(str(hwnd), encoding="ascii")


def known_window() -> int:
    """拿到宿主窗口：内存里记的 → 宿主写的 hwnd 文件 → 按标题枚举兜底。"""
    global _known
    if _is_guanlan(_known):
        return _known
    for f in (GL_HWNDFILE, HWNDFILE):
        try:
            h = int(f.read_text(encoding="ascii").strip())
        except Exception:
            continue
        if _is_guanlan(h):
            _known = h
            return h
    h = launch.find_window(include_hidden=True)
    if h:
        _known = h
    return h


def notify(title: str, text: str) -> None:
    """气泡提示；图标还没建好时静默跳过。"""
    if not _icon_added or _nid is None:
        return
    nid = NOTIFYICONDATAW()
    nid.cbSize = NID_V2_SIZE
    nid.hWnd = _hwnd
    nid.uID = 1
    nid.uFlags = NIF_INFO
    nid.szInfo = text[:255]
    nid.szInfoTitle = title[:63]
    nid.dwInfoFlags = NIIF_INFO
    nid.uTimeout = 4000
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))


# ── 窗口动作 ──────────────────────────────────────────

def show_window() -> None:
    """托盘左键 / 「显示观澜」：SW_SHOW + 提到前台，同一个 hwnd，零重载。"""
    h = known_window()
    if not h:
        # 宿主进程已经不在了，重新开一个
        launch.open_window()
        log("宿主窗口不存在，已重新 open_window()")
        return
    if not user32.IsWindowVisible(h):
        user32.ShowWindow(h, SW_SHOW)
    launch.focus_window(h)
    log(f"恢复窗口 hwnd={h}")


def hide_window() -> None:
    """右键「隐藏到托盘」：不销毁窗口，只 SW_HIDE。"""
    h = known_window()
    if not h:
        return
    user32.ShowWindow(h, SW_HIDE)
    notify("观澜已收进托盘", "单击托盘图标或再点桌面图标即可恢复")
    log(f"隐藏窗口 hwnd={h}")


def quit_from_stop() -> None:
    """launch.py --stop 通知：只撤自己的图标并退出，窗口与服务由 --stop 收尾。"""
    try:
        cleanup()
    except Exception as e:
        log(f"cleanup 异常（已忽略）：{e!r}")
    finally:
        user32.PostQuitMessage(0)      # 无论清理出什么问题，消息循环必须能退出


def quit_full() -> None:
    """托盘「退出观澜」：真退出 = 放行宿主关闭 + 停服务 + 撤图标。"""
    try:
        h = known_window()
        # 写标志让宿主 closing 事件放行，否则 WM_CLOSE 会被吃掉变成最小化
        try:
            EXITFLAG.write_text("1", encoding="ascii")
        except Exception:
            pass
        if h and user32.IsWindow(h):
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
        launch.stop(kill_tray=False)
    except Exception as e:
        log(f"stop() 异常（已忽略）：{e!r}")
    finally:
        try:
            cleanup()
        except Exception as e:
            log(f"cleanup 异常（已忽略）：{e!r}")
        user32.PostQuitMessage(0)


def cleanup() -> None:
    global _icon_added, _hicon, _cleaned
    if _cleaned:
        return
    _cleaned = True
    if _icon_added and _nid is not None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_nid))
        _icon_added = False
    if _hicon:
        user32.DestroyIcon(_hicon)
        _hicon = 0
    # 只删托盘自己的状态文件；.gl-window-hwnd 归宿主管，别动
    if not _selftest:
        unlink_quietly(PIDFILE)
        unlink_quietly(HWNDFILE)
    log("托盘已撤销，守护退出")


def popup_menu() -> None:
    menu = user32.CreatePopupMenu()
    if not menu:
        return
    try:
        user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示观澜")
        user32.AppendMenuW(menu, MF_STRING, ID_HIDE, "隐藏到托盘")
        user32.AppendMenuW(menu, MF_SEPARATOR, ID_SEP, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出观澜")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(_hwnd)   # 否则菜单点了不消失
        cmd = user32.TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON |
                                    TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, _hwnd, None)
        user32.PostMessageW(_hwnd, WM_NULL, 0, 0)
        if cmd == ID_SHOW:
            show_window()
        elif cmd == ID_HIDE:
            hide_window()
        elif cmd == ID_QUIT:
            quit_full()
    finally:
        user32.DestroyMenu(menu)


def _wndproc(hwnd, msg, wparam, lparam):
    # ctypes 回调里抛异常只会打到 stderr（"Exception ignored"），消息照样丢，
    # 退出流程可能就此卡死。整段兜住。
    try:
        return _wndproc_inner(hwnd, msg, wparam, lparam)
    except Exception as e:
        log(f"WndProc 异常 msg={msg:#x}: {e!r}")
        if msg in (WM_TRAY_QUIT, WM_DESTROY):
            user32.PostQuitMessage(0)
        return 0


def _wndproc_inner(hwnd, msg, wparam, lparam):
    global _known
    if msg == WM_TRAY:
        event = lparam & 0xFFFF
        if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            show_window()
        elif event == WM_RBUTTONUP:
            popup_menu()
        return 0
    if msg == WM_TRAY_MINIMIZED:
        _known = int(wparam) or 0
        if _known:
            _write_hwnd(_known)
        notify("观澜已最小化到托盘", "服务仍在后台运行，单击图标即可恢复")
        log(f"宿主通知：点 × 收进托盘 hwnd={_known}")
        return 0
    if msg == WM_TRAY_QUIT:
        quit_from_stop()
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ── 建窗口 / 建图标 ────────────────────────────────────

def create_window() -> int:
    global _wndproc_keep
    _wndproc_keep = WNDPROC(_wndproc)        # 全局引用，回调不能被回收
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _wndproc_keep
    wc.hInstance = hinst
    wc.lpszClassName = TRAY_CLASS
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom and ctypes.get_last_error() != 1410:    # 1410 = 类已注册
        raise OSError(f"RegisterClassExW 失败 err={ctypes.get_last_error()}")
    hwnd = user32.CreateWindowExW(0, TRAY_CLASS, TRAY_TITLE, 0,
                                  0, 0, 0, 0,
                                  None, None, hinst, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
    return hwnd


def add_icon() -> None:
    global _hicon, _nid, _icon_added
    if ICOPATH.exists():
        _hicon = user32.LoadImageW(None, str(ICOPATH), IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not _hicon:
        _hicon = user32.LoadImageW(None, 32512, IMAGE_ICON, 0, 0, LR_DEFAULTSIZE)
    nid = NOTIFYICONDATAW()
    nid.cbSize = NID_V2_SIZE
    nid.hWnd = _hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = _hicon
    nid.szTip = "观澜 · 单击显示 / 右键菜单"
    _nid = nid
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        raise OSError(f"Shell_NotifyIcon(NIM_ADD) 失败 err={ctypes.get_last_error()}")
    _icon_added = True


def ensure_single_instance() -> None:
    """已有守护在跑就直接退出：托盘图标只允许有一个。"""
    if user32.FindWindowW(TRAY_CLASS, None):
        log("已有托盘守护在运行，本次不重复启动")
        raise SystemExit(0)


def main(argv: list[str]) -> int:
    global _hwnd, _selftest, _known
    DATA.mkdir(parents=True, exist_ok=True)

    if "--status" in argv:
        h = user32.FindWindowW(TRAY_CLASS, None)
        print(f"观澜托盘：{'运行中' if h else '未运行'} · 宿主 hwnd={known_window()}")
        print(f"日志：{LOG}")
        return 0 if h else 1

    _selftest = "--selftest" in argv
    if not _selftest:
        ensure_single_instance()

    log("── 托盘守护启动 ──")
    _hwnd = create_window()
    if not _selftest:
        PIDFILE.write_text(str(os.getpid()), encoding="ascii")
    add_icon()
    log(f"窗口 hwnd={_hwnd} · 图标已建立 · V2_SIZE={NID_V2_SIZE}")

    if _selftest:
        # 图标 + 窗口 + 消息循环 + 优雅退出全跑一遍；不开服务、不碰任何窗口状态
        user32.PostMessageW(_hwnd, WM_TRAY_QUIT, 0, 0)
    else:
        h = known_window()
        if h:
            _known = h
            _write_hwnd(h)
            log(f"接管宿主窗口 hwnd={h}")
        notify("观澜在后台运行", "点右上角 × 会收进托盘，不会退出")

    msg = wintypes.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret in (0, -1):
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    cleanup()
    if _hwnd:
        user32.DestroyWindow(_hwnd)
    log("── 托盘守护结束 ──")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
