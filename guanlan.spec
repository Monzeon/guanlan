# -*- mode: python ; coding: utf-8 -*-
"""观澜 GUANLAN · PyInstaller 单文件打包

    python -m PyInstaller --noconfirm --clean guanlan.spec

产物：dist/观澜.exe

四个角色共用一个入口 entry.py，靠 --role=server|tray|window 区分，
所以 onefile 下子进程仍然指向 sys.executable（也就是这个 exe 自己）。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all
import webview as _webview

ROOT = Path(SPECPATH).resolve()          # noqa: F821  PyInstaller 注入
_WV_LIB = Path(_webview.__file__).parent / "lib"

# 资源解压到 sys._MEIPASS；server.py / bible.py / reading.py 通过
# app_paths.RES_DIR 读它们，数据文件仍然落 D:\GuanlanData。
datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "app"), "app"),
    (str(ROOT / "schema.sql"), "."),
    (str(ROOT / "reading_schema.sql"), "."),
    (str(ROOT / "bible_schema.sql"), "."),
    (str(ROOT / "event_topics_schema.sql"), "."),
    (str(ROOT / "vendor"), "vendor"),
]
# pywebview 的 util.interop_dll_path() 在 frozen 形态下只找三处：
#   <模块目录>/lib/<name>、<exe 目录>/<name>、<sys._MEIPASS>/<name>
# 而 PyInstaller 自己的 hook 把 DLL 放在 _MEIPASS/webview/lib/，它看不到。
# 于是按第三处的期望，把托管程序集平铺到 _MEIPASS 根，把
# WebView2Loader.dll 按 win-x64 / win-x86 / win-arm64 目录放好
# （edgechromium.py 会把这个目录追加进 PATH）。
for _name in ("Microsoft.Web.WebView2.Core.dll",
              "Microsoft.Web.WebView2.WinForms.dll",
              "WebBrowserInterop.x64.dll",
              "WebBrowserInterop.x86.dll"):
    _p = _WV_LIB / _name
    if _p.exists():
        datas.append((str(_p), "."))
for _plat in ("win-arm64", "win-x64", "win-x86"):
    _p = _WV_LIB / "runtimes" / _plat / "native"
    if _p.is_dir():
        datas.append((str(_p), _plat))

binaries = []
hiddenimports = [
    # 单入口里是函数内 import，静态分析容易漏，显式列全
    "app_paths", "launch", "server", "tray", "native_window", "storage",
    "bible", "reading", "reading_ai", "event_topics", "pdf_text", "cloud_sync", "peer_sync", "seed",
    "win32gui", "win32api", "win32con", "win32process",
    "webview", "clr", "clr_loader", "bottle",
]

# pywebview 的 webview/lib/*.dll（含 WebView2 Core / WinForms / Loader）
# 与 pythonnet 的 Python.Runtime.dll 必须随包带走，否则窗口起不来。
for pkg in ("webview", "pythonnet", "clr_loader"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(                              # noqa: F821
    [str(ROOT / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)        # noqa: F821

# 用 onedir 而不是 onefile：四个角色进程会同时启动，onefile 各自解压
# 41 MiB 到 %TEMP% 存在时序竞态（实测并发时 ClrLoader.dll / WebView2
# 程序集时而缺失，出现 0x7e、白屏或窗口起不来）。onedir 把所有 DLL 固定
# 落在 _internal/，没有解压、没有临时目录，启动也快得多。
# 产物仍是双击即用的 dist/观澜/观澜.exe。
exe = EXE(                                # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="观澜",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                        # --noconsole：双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "app" / "guanlan.ico"),
)
coll = COLLECT(                            # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="观澜",
)
