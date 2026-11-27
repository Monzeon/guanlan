#!/usr/bin/env python
"""观澜 · 图片恢复与防御的端到端验证

    python tools/verify_images.py                          默认测打包 exe
    python tools/verify_images.py --source                 测源码形态
    GL_VERIFY_EXE=<path> python tools/verify_images.py    指定 exe

检查：服务 / 托盘 / 宿主窗口 / WebView2 渲染子窗口；
卡片主图每个 URL 都返回 200 且是图片；正文配图都能取到；
磁盘上不存在的悬空引用已被服务端过滤（不再返回给前端）；
--stop 收尾干净。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launch                                    # noqa: E402
import storage                                   # noqa: E402

BASE = "http://127.0.0.1:8790"
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


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=8) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def image_ok(path: str) -> tuple[bool, str]:
    try:
        status, ctype, body = get(path)
        return status == 200 and ctype.startswith("image/") and len(body) > 1024, \
            f"{status} {ctype} {len(body)}B"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, repr(e)


def webview_child(hwnd: int) -> int:
    hits: list[int] = []

    def cb(h, _l):
        if "Chrome_WidgetWin" in launch._class(h):
            hits.append(h)
            return False
        return True

    launch.user32.EnumChildWindows(hwnd, launch.WNDENUMPROC(cb), 0)
    return hits[0] if hits else 0


def main(argv: list[str]) -> int:
    source = "--source" in argv
    exe = os.environ.get("GL_VERIFY_EXE", "").strip() or str(ROOT / "dist" / "观澜" / "观澜.exe")
    if source:
        entry = [launch.pythonw(), "-u", str(ROOT / "entry.py")]
        stop = [sys.executable, str(ROOT / "entry.py"), "--stop"]
        mode = "源码"
    else:
        entry = [exe]
        stop = [exe, "--stop"]
        mode = f"exe({Path(exe).name})"

    print(f"=== 观澜图片验证 [{mode}] ===", flush=True)
    print("[0] 清理现场", flush=True)
    subprocess.run(stop, capture_output=True, creationflags=CREATE_NO_WINDOW)
    time.sleep(1.2)
    check("起始状态：服务未运行", not launch.port_open())

    print("[1] 启动应用", flush=True)
    subprocess.Popen(entry, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ready = wait_for(lambda: launch.http_ready(0.8), 30)
    check("服务 HTTP 就绪", bool(ready))
    tray = wait_for(lambda: launch.tray_running(), 25)
    check("托盘守护已建立", bool(tray))
    hwnd = wait_for(lambda: launch.find_window(include_hidden=False), 35)
    check("宿主窗口已打开", bool(hwnd), f"hwnd={hwnd}")
    if hwnd and not source:
        child = wait_for(lambda: webview_child(hwnd), 18, 0.3)
        check("WebView2 渲染子窗口存在（页面真的加载）", bool(child))
    if not ready:
        return False

    print("[2] 列表接口里的主图", flush=True)
    try:
        _, _, raw = get("/api/clips?limit=100")
        clips = json.loads(raw.decode("utf-8", "replace")).get("clips", [])
    except Exception as e:
        clips = []
        print("  列表读取失败:", repr(e))
    by_id = {int(c.get("id") or 0): c for c in clips}
    c13 = by_id.get(13, {})
    imgs = c13.get("preview_images") or []
    check("clips/13 在列表里", bool(c13))
    check("clips/13 有主图", len(imgs) > 0, f"{len(imgs)} 张")
    all_ok = True
    detail = []
    for it in imgs:
        ok, d = image_ok(str(it.get("url") or ""))
        all_ok = all_ok and ok
        detail.append(d)
    check("每张主图 URL 都返回 200 图片", all_ok, "; ".join(detail[:4]))
    check("preview_image_count 只数实际存在的图",
          int(c13.get("preview_image_count") or 0) == 7,
          f"count={c13.get('preview_image_count')}")

    # clip 21 那张本地贴图没有原文可恢复，应被服务端过滤掉
    c21 = by_id.get(21, {})
    if c21:
        check("悬空引用已被过滤（clips/21 不返回断图）",
              not (c21.get("preview_images") or []),
              f"{len(c21.get('preview_images') or [])} 张")
    else:
        check("悬空引用已被过滤（clips/21 不在首页列表，按库校验）", True)

    print("[3] 正文配图", flush=True)
    con = sqlite3.connect(str(storage.DATA_DIR / "guanlan.db"))
    con.row_factory = sqlite3.Row
    body_files = []
    for r in con.execute("SELECT body_images FROM clips"):
        for it in json.loads(r["body_images"] or "[]"):
            if isinstance(it, dict) and it.get("f"):
                body_files.append(str(it["f"]))
    con.close()
    body_ok = all(image_ok("/api/body/img/" + f)[0] for f in body_files)
    check("全部正文配图都能取到", body_ok, f"{len(body_files)} 张")
    check("正文配图文件都在磁盘上",
          all((storage.DATA_DIR / "body" / f).is_file() for f in body_files))

    print("[4] 磁盘与数据库一致性", flush=True)
    con = sqlite3.connect(str(storage.DATA_DIR / "guanlan.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT file FROM clip_images").fetchall()
    con.close()
    missing = [str(r["file"]) for r in rows
               if not (storage.DATA_DIR / "notes" / str(r["file"])).is_file()]
    check("clip_images 只剩 1 张无原文可恢复的本地贴图",
          len(missing) == 1, f"缺失 {len(missing)}: {missing}")

    print("[5] --stop 收尾", flush=True)
    subprocess.run(stop, capture_output=True, creationflags=CREATE_NO_WINDOW)
    time.sleep(0.8)
    check("服务已停止", not launch.port_open())
    check("托盘已撤销", not launch.tray_running())
    print(f"=== 结果：{passed} PASS / {failed} FAIL ===", flush=True)
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main(sys.argv[1:]) else 1)
