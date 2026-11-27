#!/usr/bin/env python
"""回滚 2026-11-14 的「HTML 原件直读」修复。

    python tools/rollback_html_inline.py

只还原本次改动的六个点，不动数据库、不动 exe 本体、不动其它静态资源：

  1. static/app.js                     ← backup 原件
  2. server.py                         ← backup 原件（源码，运行 exe 未受影响）
  3. README.md 第 23 行                ← 还原那一句
  4. dist/观澜/_internal/static/app.js ← backup 的运行实例原件
  5. 删除新增的 html-reader.html / html-reader.js（源码 + 运行实例）
  6. 桌面 观澜.url 本次未改动，脚本不去碰它（备份仍在 backup 里）
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "backup" / "2026-11-14-html-read"
RUNTIME = ROOT / "dist" / "观澜" / "_internal" / "static"
ADDED = ("html-reader.html", "html-reader.js")

OLD_LINE = ("详情页一个「打开原件」按钮，PDF / 纯文本在浏览器里直接打开，"
            "其它类型下载；")
NEW_LINE = ("详情页一个「打开原件」按钮，PDF / 纯文本在浏览器里直接打开，"
            "HTML 点「直接读」在观澜内按原版式直读（沙箱隔离、无需下载），"
            "其它类型下载；")


def say(msg: str) -> None:
    print(msg, flush=True)


def restore(src: Path, dst: Path) -> None:
    if not src.is_file():
        say(f"[SKIP] 备份缺失：{src}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    say(f"[OK]   还原 {dst.relative_to(ROOT)}")


def main() -> int:
    if not BACKUP.is_dir():
        say(f"[ERROR] 找不到备份目录 {BACKUP}，没有回滚")
        return 1

    restore(BACKUP / "app.js.orig", ROOT / "static" / "app.js")
    restore(BACKUP / "server.py.orig", ROOT / "server.py")
    restore(BACKUP / "app.js.runtime.orig", RUNTIME / "app.js")

    readme = ROOT / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        if NEW_LINE in text:
            readme.write_text(text.replace(NEW_LINE, OLD_LINE, 1), encoding="utf-8")
            say("[OK]   还原 README.md 第 23 行")
        else:
            say("[SKIP] README.md 里没有本次改动的句子，保持原样")

    for name in ADDED:
        for base in (ROOT / "static", RUNTIME):
            target = base / name
            if target.exists():
                target.unlink()
                say(f"[OK]   删除 {target}")
            else:
                say(f"[SKIP] 不存在 {target}")

    say("")
    say("回滚完成。刷新观澜页面（Ctrl+R）即可看到「打开原件」恢复为下载。")
    say("若之后重建过 exe，重建产物会带上 server.py 的回滚版本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
