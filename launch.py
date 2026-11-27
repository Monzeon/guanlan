#!/usr/bin/env python
"""观澜 GUANLAN · 启动器

一个入口，只开一个窗口。

    pythonw launch.py           正常启动（桌面图标就走这条）
    python  launch.py --stop    停止后台服务
    python  launch.py --status  看一眼状态

为什么要有这个文件：
    旧 start.cmd 无条件执行 `pythonw server.py`，于是点第二次图标时又起了
    一个进程；那个进程绑不上 8790，server.py 的"端口占用"分支就
    webbrowser.open() 弹了个普通浏览器窗口——紧接着 start.cmd 又开了
    Edge --app 窗口。同一个页面，两个界面。

    现在职责切开：server.py 只负责服务，绝不开窗；窗口只由本文件开，
    开之前先看有没有已经开着的，有就提到前台。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

import storage                                   # 数据与缓存的落盘根目录
import app_paths                                 # 单入口 / 打包后的程序目录

ROOT = app_paths.APP_DIR
DATA = storage.DATA_DIR
LOG = DATA / "server.log"
PIDFILE = DATA / "server.pid"
STAMP = DATA / ".ui-opened"
LOCKFILE = DATA / ".launch.lock"
TRAY_PIDFILE = DATA / "tray.pid"
TRAY_HWNDFILE = DATA / ".tray-hwnd"
TRAY_LOGFILE = DATA / "tray.log"
TRAY_CLASS = "GuanlanTrayWnd"
GL_HWNDFILE = DATA / ".gl-window-hwnd"       # native_window.py 建窗后写入
EXITFLAG = DATA / ".gl-exiting"               # 托盘 / --stop 写，宿主据此放行关闭
NATIVE_LOGFILE = DATA / "native.log"

PORT = int(os.environ.get("PORT") or 8790)
URL = f"http://127.0.0.1:{PORT}/"

WM_CLOSE = 0x0010

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
SW_RESTORE = 9

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# ── 探测 ─────────────────────────────────────────────

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port_open(timeout: float = 0.25) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def http_ready(timeout: float = 1.5) -> bool:
    """服务是不是真的起来了。

    只看 HTTP 200 是不够的：端口上若是别的应用（比如用户自己起的
    http 服务），它也会回 200，launcher 就会把它当成观澜弹窗。
    这里认准观澜自己的 /api/sync（回 {"version": N}）。
    """
    try:
        with _OPENER.open(f"{URL}api/sync", timeout=timeout) as r:
            if r.status != 200:
                return False
            body = r.read(4096).decode("utf-8", "replace")
        return '"version"' in body
    except Exception:
        return False


def wait_ready(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if http_ready(0.8):
            return True
        time.sleep(0.12)
    return False


# ── 窗口 ─────────────────────────────────────────────

def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_guanlan_title(hwnd: int) -> bool:
    # 托盘守护自己有一个标题含「观澜」的隐藏消息窗口，必须排除，
    # 否则 find_window() 会把它当宿主，启动流程提前 return、宿主压根不开。
    if _class(hwnd) == TRAY_CLASS:
        return False
    t = _title(hwnd)
    return "观澜" in t or "GUANLAN" in t.upper() or f":{PORT}" in t


def find_window(include_hidden: bool = False) -> int:
    """已开着的观澜窗口。

    窗口现在是自己的 WebView2 宿主（WinForms BrowserForm），没有
    Chrome_WidgetWin 类名了，所以按「hwnd 文件 + 标题」两条路认：
    native_window.py 建窗后写 .gl-window-hwnd，这是最准的一条；
    枚举顶层窗口按标题匹配作为兜底（也兼容还开着的旧 Edge 窗口）。
    收进托盘的窗口是 SW_HIDE 状态，默认不认；托盘用 include_hidden=True。
    """
    try:
        hwnd = int(GL_HWNDFILE.read_text(encoding="ascii").strip())
        if user32.IsWindow(hwnd) and (include_hidden or user32.IsWindowVisible(hwnd)) \
                and _is_guanlan_title(hwnd):
            return hwnd
    except Exception:
        pass

    hits: list[int] = []

    def cb(hwnd, _lparam):
        if not include_hidden and not user32.IsWindowVisible(hwnd):
            return True
        if _is_guanlan_title(hwnd):
            hits.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hits[0] if hits else 0


TRAY_SHOW = 5


def tray_hidden_window() -> int:
    """托盘接管的、正处于隐藏态的观澜窗口。

    桌面图标被再点一次时走这里：直接 SW_SHOW + 提到前台，不再开第二个窗口。
    """
    for f in (GL_HWNDFILE, TRAY_HWNDFILE):
        try:
            hwnd = int(f.read_text(encoding="ascii").strip())
        except Exception:
            continue
        if not user32.IsWindow(hwnd) or user32.IsWindowVisible(hwnd):
            continue
        if _is_guanlan_title(hwnd):
            return hwnd
    hidden = find_window(include_hidden=True)
    return hidden if hidden and not user32.IsWindowVisible(hidden) else 0


def focus_window(hwnd: int) -> None:
    if not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, TRAY_SHOW)     # 从托盘恢复
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    # 前台权限在别的线程手里，短接一下再设，避免 SetForegroundWindow 静默失败
    user32.AttachThreadInput(tid_fg, tid_me, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(tid_fg, tid_me, False)


def ui_recently_opened(window: float = 2.5) -> bool:
    """连点两下时第二次不再开窗：第一下开的窗可能还在加载。"""
    try:
        return time.time() - float(STAMP.read_text().strip()) < window
    except Exception:
        return False


# ── 服务 ─────────────────────────────────────────────

def pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def spawn_detached(cmd: list[str], **kwargs) -> subprocess.Popen:
    """脱离父进程的作业对象：启动器退出后服务和应用窗口都留在桌面上。

    某些作业对象禁止 breakaway（会报 Access Denied），那就退回普通分离，
    在自己点图标（无作业）的场景里两者等价。
    """
    try:
        return subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB,
                                close_fds=True, **kwargs)
    except OSError:
        return subprocess.Popen(cmd, creationflags=DETACHED_PROCESS, close_fds=True, **kwargs)


def start_server() -> subprocess.Popen:
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"      # 日志统一 UTF-8，出错时 tail 才读得懂
    # 子进程按同一套根目录解析：启动器可能被带不同环境变量地调起，
    # 显式传过去，服务端就不会在另一个盘上另起一份库。
    env["GL_HOME"] = str(storage.HOME)
    env["GL_DATA_DIR"] = str(storage.DATA_DIR)
    env["GL_CACHE_DIR"] = str(storage.CACHE_DIR)
    with LOG.open("ab") as log:
        log.write(f"\n── {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 ──\n".encode())
        log.flush()
        # -u：无控制台时 stdout 是文件，行缓冲会憋住日志，崩溃就看不清死在哪
        # --lan：端口绑到局域网，手机才连得进来。
        # 能不能进由「设置 · 手机访问」的开关决定，关着时非本机一律 403。
        proc = spawn_detached(
            app_paths.role_cmd("server", "--lan"),
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    PIDFILE.write_text(str(proc.pid))
    return proc


def open_window() -> None:
    """起原生 WebView2 宿主窗口（取代旧的 msedge.exe --app）。

    窗口进程属于观澜自己，WM_CLOSE 由 native_window.py 的 closing 事件
    处理：点 × 直接 hide()，不再销毁、不再重开、不再预热。
    """
    DATA.mkdir(parents=True, exist_ok=True)
    unlink_quietly(GL_HWNDFILE)           # 旧 hwnd 作废，必须在 spawn 之前删
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["GL_HOME"] = str(storage.HOME)
    env["GL_DATA_DIR"] = str(storage.DATA_DIR)
    env["GL_CACHE_DIR"] = str(storage.CACHE_DIR)
    with NATIVE_LOGFILE.open("ab") as log:
        log.write(f"\n── {time.strftime('%Y-%m-%d %H:%M:%S')} 开窗 ──\n".encode())
        log.flush()
        spawn_detached(
            app_paths.role_cmd("window"),
            cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )


# ── 托盘守护 ─────────────────────────────────────────

def tray_running() -> int:
    """返回托盘守护的隐藏窗口 hwnd；0 表示没在跑。"""
    return user32.FindWindowW(TRAY_CLASS, None)


def start_tray() -> None:
    """拉起托盘守护：DETACHED_PROCESS + pythonw，无控制台，日志单独一份。"""
    if tray_running():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["GL_HOME"] = str(storage.HOME)
    env["GL_DATA_DIR"] = str(storage.DATA_DIR)
    env["GL_CACHE_DIR"] = str(storage.CACHE_DIR)
    with TRAY_LOGFILE.open("ab") as log:
        log.write(f"\n── {time.strftime('%Y-%m-%d %H:%M:%S')} 拉起托盘 ──\n".encode())
        log.flush()
        proc = spawn_detached(
            app_paths.role_cmd("tray"),
            cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
    TRAY_PIDFILE.write_text(str(proc.pid), encoding="ascii")
    # 等它把图标建起来；没起来也不阻塞主流程，窗口照样能用
    for _ in range(24):
        if tray_running():
            return
        time.sleep(0.1)


def is_guanlan_tray(pid: int) -> bool:
    """这个 PID 是不是「本目录下的观澜 tray.py」，别 taskkill 到别的东西。"""
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, timeout=15,
            creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return False
    norm = raw.decode("mbcs", "replace").lower().replace("\\", "/")
    # 单入口形态：观澜.exe / entry.py --role=tray
    if "--role=tray" in norm and ("guanlan" in norm or "观澜" in norm):
        return True
    target = str(ROOT / "tray.py").lower().replace("\\", "/")
    return target in norm or ("tray.py" in norm and "guanlan" in norm)


def stop_tray() -> None:
    """优雅退出：放行并关掉原生窗口 + 撤托盘图标。

    顺序：先写 .gl-exiting 让宿主 closing 事件放行，再 PostMessage(WM_CLOSE)
    等窗口真的消失，最后通知托盘退出。窗口不能留成 SW_HIDE 的隐形状态。
    """
    win = tray_hidden_window() or find_window(include_hidden=True)
    if win and user32.IsWindow(win):
        try:
            EXITFLAG.write_text("1", encoding="ascii")
        except Exception:
            pass
        user32.PostMessageW(win, WM_CLOSE, 0, 0)
        for _ in range(20):
            if not user32.IsWindow(win):
                break
            time.sleep(0.1)
    else:
        # 没有宿主窗口就别留下「允许退出」标志，下次启动会误放行
        unlink_quietly(EXITFLAG)

    tray = tray_running()
    if tray:
        # 0x8000 + 2 == WM_TRAY_QUIT，与 tray.py 约定一致
        user32.PostMessageW(tray, 0x8002, 0, 0)
        for _ in range(10):
            if not tray_running():
                break
            time.sleep(0.1)
    try:
        pid = int(TRAY_PIDFILE.read_text(encoding="ascii").strip())
    except Exception:
        pid = 0
    if pid and is_guanlan_tray(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    unlink_quietly(TRAY_PIDFILE)
    unlink_quietly(TRAY_HWNDFILE)
    unlink_quietly(GL_HWNDFILE)


# ── 运维 ─────────────────────────────────────────────

def pid_on_port() -> int | None:
    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return None
    for line in raw.decode("mbcs", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") \
                and parts[3].upper() == "LISTENING" and parts[4].isdigit():
            return int(parts[4])
    return None


def is_python(pid: int) -> bool:
    try:
        raw = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return False
    return "python" in raw.decode("mbcs", "replace").lower()


def is_guanlan_server(pid: int) -> bool:
    """这个 PID 是不是「本目录下的观澜 server.py」。

    只看进程名里有 python 是不够的：PID 会被系统回收给别人，
    端口也可能被别的 python 程序占着。只看名字就 taskkill，
    会把用户自己的 jupyter / 脚本一起杀掉。

    认准的是「本目录下的 server.py 这条完整路径」，而不是命令行里
    出现过 "server.py" 三个字就认——那样 ``python -c "看 server.py"``
    这类无关进程会被误判成观澜并被杀。
    """
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, timeout=15,
            creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return False
    text = raw.decode("mbcs", "replace")
    if not text.strip():
        return False
    # 统一成小写 + 正斜杠，两种写法（反斜杠绝对路径 / 相对路径）都能比
    norm = text.lower().replace("\\", "/")
    # 单入口形态：观澜.exe / entry.py --role=server
    if "--role=server" in norm and ("guanlan" in norm or "观澜" in norm):
        return True
    target = str(ROOT / "server.py").lower().replace("\\", "/")
    if target in norm:
        return True
    # 官方启动包装器（start-guanlan-clean.py）：runpy 把 server.py 跑在包装器
    # 进程里，命令行里根本没有 server.py。文件名是我们自己的约定，不会误伤别人。
    if "start-guanlan" in norm:
        return True
    # 退一步：相对路径启动（cwd 在本目录）时命令行里只有 server.py，
    # 这时要求同时出现 guanlan 目录名，避免认错别的项目的 server.py
    return "server.py" in norm and "guanlan" in norm


def tail(n: int = 12) -> str:
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return "（没有日志）"
    return "\n".join(lines) or "（日志为空）"


def unlink_quietly(path: Path) -> None:
    """删状态文件；某些托管环境会把 Path.unlink 换成移入回收站，
    文件已不在时连 missing_ok=True 都会抛，这里整段吞掉。"""
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def alert(text: str) -> None:
    user32.MessageBoxW(0, text, "观澜", 0x10)


def stop(kill_tray: bool = True) -> int:
    """停掉后台服务（默认连托盘守护一起收）。

    顺序很重要：先关掉原生窗口 + 撤托盘，再停服务。窗口连着服务，
    服务先没了，剩下的窗口就是一张空白页。
    """
    if kill_tray:
        stop_tray()
    file_pid: int | None = None
    try:
        file_pid = int(PIDFILE.read_text().strip())
    except Exception:
        pass
    port_pid = pid_on_port()

    # --onefile 下 PyInstaller bootloader 会再派生一个子进程真正跑
    # server，proc.pid 是父、监听端口的是子。优先按端口找干活的那个。
    pid: int | None = None
    if port_pid and is_guanlan_server(port_pid):
        pid = port_pid
    elif file_pid and is_guanlan_server(file_pid):
        pid = file_pid
    elif file_pid and file_pid == port_pid and is_python(file_pid):
        # 用启动包装器（start-guanlan-clean.py 之类）跑起来的情况：runpy 把
        # server.py 执行在包装器进程里，命令行里没有 server.py 的路径，
        # is_guanlan_server 认不出来，于是 --stop 会对着自己人喊「别的程序占着」。
        # 「pid 文件记的进程」正好就是「监听本端口的 python 进程」，这两条对上
        # 已经足够认定是自己，不必再纠结命令行长什么样。
        pid = file_pid

    if not pid:
        if port_pid:
            # 端口上是别人的程序（或 PID 已被回收给了别人的 python）：
            # 明确不动它，绝不 taskkill 一个不认识的进程。
            print(f"观澜：端口 {PORT} 被别的程序占着（PID {port_pid}），没动它。")
            return 1
        print("观澜：没有在运行。")
        return 0

    extra = (file_pid if file_pid and file_pid != pid
             and is_guanlan_server(file_pid) else None)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    if extra:
        subprocess.run(["taskkill", "/PID", str(extra), "/T", "/F"],
                        capture_output=True, creationflags=CREATE_NO_WINDOW)
    unlink_quietly(PIDFILE)
    for _ in range(40):
        if not port_open():
            break
        time.sleep(0.1)
    print(f"观澜：已停止（PID {pid}）。")
    return 0


def status() -> int:
    pid = pid_on_port()
    if pid:
        print(f"观澜：运行中 · {URL} · PID {pid}")
        print(f"日志：{LOG}")
        return 0
    print("观澜：未运行。")
    return 1


# ── 主流程 ───────────────────────────────────────────

def main(argv: list[str]) -> int:
    if "--stop" in argv:
        return stop()
    if "--status" in argv:
        return status()

    DATA.mkdir(parents=True, exist_ok=True)
    unlink_quietly(EXITFLAG)         # 上次异常退出可能留下「允许退出」标志

    started = False
    if not port_open():
        # 「看端口 → 起服务」之间没有互斥：连点两下就是两个 server.py、
        # 两个窗口，还把 PID 文件写成后一个的死 PID。用一个排他锁文件
        # 把这段临界区包住：拿不到锁说明别的启动流程正在干同样的事。
        lock_fd = None
        try:
            lock_fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # 锁可能是上次异常退出留下的；超过 30 秒就当过期，删掉重来
            try:
                if time.time() - LOCKFILE.stat().st_mtime > 30:
                    unlink_quietly(LOCKFILE)
                    lock_fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                lock_fd = None
        if lock_fd is None:
            # 另一个启动流程已经在处理：等它把服务拉起来，然后把窗口提上来
            if wait_ready(25):
                hwnd = tray_hidden_window() or find_window()
                if hwnd:
                    focus_window(hwnd)
                    return 0
            return 0
        try:
            os.write(lock_fd, str(os.getpid()).encode())
            os.close(lock_fd)
        except OSError:
            pass
        try:
            if not port_open():
                start_server()
                started = True
        finally:
            unlink_quietly(LOCKFILE)

    if not wait_ready(25 if started else 6):
        if not started and port_open():
            alert(f"端口 {PORT} 被别的程序占用，观澜没能起来。\n\n{tail()}")
        else:
            alert(f"观澜服务 25 秒内没有就绪。\n\n{tail()}")
        return 2

    # 托盘守护先于窗口就位：点 × 之后窗口收进托盘，图标要一直在。
    # 托盘自己调 launch.stop(kill_tray=False)，这里不会再重复拉起。
    start_tray()

    # 收进托盘的窗口是隐藏的：直接 SW_SHOW 恢复，绝不再开第二个
    hidden = tray_hidden_window()
    if hidden:
        focus_window(hidden)
        return 0

    hwnd = find_window()
    if hwnd:
        focus_window(hwnd)          # 已经开着 → 提上来，不再开第二个
        return 0

    if ui_recently_opened():
        return 0

    open_window()
    STAMP.write_text(str(time.time()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
