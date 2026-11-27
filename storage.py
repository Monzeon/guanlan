#!/usr/bin/env python
"""观澜 GUANLAN · 存储根目录（数据 + 缓存统一落在一处）

为什么单独拎出这个文件：
    以前落盘位置是散开的——数据库、上传、正文写在 ``ROOT / "data"``（跟着程序走，
    在 E 盘）；抓网页时的无头浏览器 profile 走 ``tempfile.gettempdir()``，也就是
    C 盘的 ``%LOCALAPPDATA%\\Temp``。同一份应用的东西散在两个盘上，C 盘一紧张
    就说不清哪些能删、删了会不会丢正文。

    现在所有落盘的东西只认一个根，默认 ``D:\\GuanlanData``：

        data/     库、上传、正文配图、随手记、同步状态、日志
        cache/    抓网页用的浏览器 profile（渲染缓存，删了不丢数据）
        browser/  观澜窗口专用的 Edge profile（浏览器缓存）

    换地方不用改代码，设环境变量就行：

        GL_HOME       整个根          默认 D:\\GuanlanData
        GL_DATA_DIR   数据目录        默认 <GL_HOME>\\data
        GL_CACHE_DIR  缓存目录        默认 <GL_HOME>\\cache

    D 盘不在（安卓上没有 D 盘、移动盘没插、目录写不进去）时自动退回程序目录下的
    ``data``，宁可位置不理想也不能起不来。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import app_paths                                 # 打包后 __file__ 不再是程序目录

ROOT = app_paths.APP_DIR
DEFAULT_HOME = r"D:\GuanlanData"


def _writable(path: Path) -> bool:
    """能建、能写才算可用。

    只看 exists 不够：D 盘可能是没插的移动盘，也可能是只剩几百字节的满盘，
    这两种情况下 mkdir 会成功、第一次写库才炸，那时用户已经在用了。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".gl-write-probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _pick_home() -> tuple[Path, bool]:
    """(根目录, 是不是落在预期位置)。

    环境变量优先；没设就试默认的 D 盘；都不行退回程序目录。
    """
    explicit = os.environ.get("GL_HOME")
    home = Path(explicit) if explicit else Path(DEFAULT_HOME)
    if _writable(home):
        return home, True
    return ROOT, False


HOME, EXTERNAL = _pick_home()
DATA_DIR = Path(os.environ.get("GL_DATA_DIR") or (HOME / "data"))
CACHE_DIR = Path(os.environ.get("GL_CACHE_DIR") or (HOME / "cache"))
RENDER_DIR = CACHE_DIR / "render"          # 抓正文的无头浏览器 profile
BROWSER_DIR = CACHE_DIR / "browser"        # 观澜窗口（Edge --app）专用 profile


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, RENDER_DIR, BROWSER_DIR):
        path.mkdir(parents=True, exist_ok=True)


def describe() -> str:
    return (f"数据 {DATA_DIR}" + ("" if EXTERNAL else "（注意：未落到预设根目录，"
                                                    "已退回程序目录）")
            + f" · 缓存 {CACHE_DIR}")


def prune_cache(render_days: int = 14, cdp_days: int = 1) -> int:
    """清掉陈旧的渲染 profile，返回删掉的目录数。

    两种东西会越攒越多：
      * ``render/new``、``render/old-deep`` 是故意留着的——挑战站点的 cookie
        在里面，第二次抓同一家能秒过。留，但不能永久留。
      * ``render/gl-cdp-*`` 是每次调试渲染的临时 profile，正常路径会在 finally
        里删掉；进程被强杀时漏下的那几个，过期了就该扫掉。

    按 mtime 判：还在用的目录 mtime 一直在更新，不会误删。
    """
    now = time.time()
    removed = 0
    for path in list(RENDER_DIR.glob("*")) if RENDER_DIR.is_dir() else []:
        if not path.is_dir():
            continue
        limit = (cdp_days if path.name.startswith("gl-cdp-") else render_days) * 86400
        try:
            if now - path.stat().st_mtime < limit:
                continue
        except OSError:
            continue
        try:
            import shutil
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


if __name__ == "__main__":
    ensure_dirs()
    print(describe())
