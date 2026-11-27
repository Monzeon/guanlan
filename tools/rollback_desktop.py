#!/usr/bin/env python
"""回滚「桌面原图标修复」，回到修复前的状态。

    python tools/rollback_desktop.py --dry-run    只检查
    python tools/rollback_desktop.py               真正回滚

还原 launch.vbs / start.cmd 的字节副本（原编码：UTF-16 / GBK），
把 README 两处说明反向替换回去，移走本次新增的验证与回滚脚本。
之前几轮改造（单入口 / exe / 托盘）全部保持不动。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BK = ROOT / "backup" / "2026-11-14-desktop"
CREATE_NO_WINDOW = 0x08000000

RESTORE = {
    "launch.vbs.orig": "launch.vbs",
    "start.cmd.orig":  "start.cmd",
}
RETIRE = ["tools/verify_desktop.py", "tools/rollback_desktop.py"]

# README 本次只改了两处，直接反向替换，不需要额外的大文件副本
README_PAIRS = [
("""桌面有两个入口：「观澜」（`观澜.url` → `launch.vbs`）与「观澜（应用版）」
（`.lnk`），两者都落到打包好的 `dist/观澜/观澜.exe`，同一份代码、同一份数据。

`launch.vbs` 优先找 `dist/` 下的 exe 直接运行（wscript 静默、不弹黑框），
找不到才回退源码形态 `entry.py`；`start.cmd` 同理。这样桌面图标不依赖系统
PATH 里是哪个 Python —— 之前 PATH 命中的是 Python 3.10，没装 pywebview，
窗口角色会 `ModuleNotFoundError: No module named 'webview'`，图标点了没窗口。

桌面入口拉起 `entry.py` / `观澜.exe` 后：""",
 """桌面有两个入口：「观澜」指向 `launch.vbs`（源码形态，回退用），
「观澜（应用版）」指向打包好的 `dist/观澜/观澜.exe`（见文末「打包成 exe」）。
两者同一份代码、同一份数据。

源码入口：`launch.vbs`（wscript 静默运行，没有黑框）拉起 `entry.py`："""),
("""产物是 **onedir**（`观澜.exe` 11 MB + `_internal/` 约 86 MB），双击 exe 即用，
不依赖系统 Python。桌面「观澜」原图标（`观澜.url` → `launch.vbs`）与
「观澜（应用版）」`.lnk` 都优先落到这个 exe；`launch.vbs` / `start.cmd`
在 exe 缺失时才回退源码形态 `entry.py`。
回归验证：`python tools/verify_desktop.py`（模拟双击原图标）。""",
 """产物是 **onedir**（`观澜.exe` 11 MB + `_internal/` 约 86 MB），双击 exe 即用，
不依赖系统 Python。桌面原有的「观澜.url」保持不动，仍指向 `launch.vbs`
走源码形态，两个入口并存。"""),
]


def say(s: str) -> None:
    print(s, flush=True)


def main(argv: list[str]) -> int:
    say("=== 回滚：桌面原图标修复 ===")
    missing = [k for k in RESTORE if not (BK / k).exists()]
    if missing:
        say(f"[ERROR] 缺原始副本：{missing}，未做任何修改")
        return 1

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    hits = sum(1 for new, _ in README_PAIRS if new in text)

    if "--dry-run" in argv:
        say("[dry-run] 只检查，不改动任何文件")
        for k, rel in RESTORE.items():
            say(f"  OK  {k}  →  {rel}  ({(BK / k).stat().st_size} bytes)")
        say(f"  README 反向锚点命中 {hits}/{len(README_PAIRS)}")
        say(f"  待移走：{', '.join(RETIRE)}")
        say("=== dry-run 通过 ===")
        return 0 if hits == len(README_PAIRS) else 1

    say("[1] 停掉当前进程")
    subprocess.run([sys.executable, str(ROOT / "entry.py"), "--stop"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)

    say("[2] 按字节还原两个启动脚本（保留原编码）")
    for k, rel in RESTORE.items():
        shutil.copy2(BK / k, ROOT / rel)
        say(f"  {rel}  ←  {k}")

    say("[3] 还原 README 两处说明")
    s = readme.read_text(encoding="utf-8")
    for new, old in README_PAIRS:
        if new in s:
            s = s.replace(new, old, 1)
    readme.write_text(s, encoding="utf-8")
    say(f"  已处理 {hits} 处")

    say("[4] 移走本次新增文件（备份成 .retired）")
    for rel in RETIRE:
        p = ROOT / rel
        if p.exists():
            shutil.copy2(p, BK / (p.name + ".retired"))
            say(f"  {rel}  →  backup/2026-11-14-desktop/{p.name}.retired")

    say("=== 回滚完成：桌面图标回到「走系统 pythonw」的形态 ===")
    say("注意：该形态在 PATH 命中 Python 3.10 时会因缺 pywebview 打不开窗口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
