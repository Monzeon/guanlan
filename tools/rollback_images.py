#!/usr/bin/env python
"""回滚「图片恢复 + 防御」这轮的代码改动（已恢复的图片文件保留）。

    python tools/rollback_images.py --dry-run      只检查
    python tools/rollback_images.py                 回滚并重新构建 exe
    python tools/rollback_images.py --no-rebuild    只还原源码，不重建 exe

还原 server.py、移除 README 新增小节、移走 3 个工具脚本（备份成
.retired），并默认重跑 build_exe.py 让 exe 与源码一致。

注意：补回的 19 张图片是数据修复成果，**不会删除**；它们仍在
data/notes 与 data/body 里，界面照样能显示。若要连数据一起回到修复前，
按 docs/audit-2026-11-14-images.log 里的清单手工删除即可，数据库备份在
backup/2026-11-14-images/guanlan.db.orig（本轮一行数据库都没改）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BK = ROOT / "backup" / "2026-11-14-images"
RETIRE = ["tools/recover_images.py", "tools/verify_images.py",
          "tools/rollback_images.py"]
SECTION_START = "### 图片丢失与自检"
SECTION_END = "### 打包成 exe（已完成）"


def say(s: str) -> None:
    print(s, flush=True)


def main(argv: list[str]) -> int:
    say("=== 回滚：图片恢复与防御 ===")
    if not (BK / "server.py.orig").exists():
        say("[ERROR] 缺 backup/2026-11-14-images/server.py.orig，未做任何修改")
        return 1

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    has_section = SECTION_START in text and SECTION_END in text

    if "--dry-run" in argv:
        say("[dry-run] 只检查，不改动任何文件")
        say(f"  OK  server.py.orig  →  server.py ({(BK / 'server.py.orig').stat().st_size} bytes)")
        say(f"  README 新增小节可定位: {has_section}")
        say(f"  待移走：{', '.join(RETIRE)}")
        say("  默认会重跑 tools/build_exe.py（--no-rebuild 可跳过）")
        say("  已恢复的 19 张图片保留，不删除")
        say("=== dry-run 通过 ===")
        return 0 if has_section else 1

    say("[1] 停掉当前进程")
    subprocess.run([sys.executable, str(ROOT / "entry.py"), "--stop"],
                   capture_output=True)

    say("[2] 还原 server.py（字节副本）")
    shutil.copy2(BK / "server.py.orig", ROOT / "server.py")
    say("  server.py  ←  server.py.orig")

    say("[3] 移除 README 新增小节")
    s = readme.read_text(encoding="utf-8")
    a, b = s.find(SECTION_START), s.find(SECTION_END)
    if a >= 0 and b > a:
        readme.write_text(s[:a] + s[b:], encoding="utf-8")
        say("  已移除")

    say("[4] 移走本轮新增脚本（备份成 .retired）")
    for rel in RETIRE:
        p = ROOT / rel
        if p.exists():
            shutil.copy2(p, BK / (p.name + ".retired"))
            say(f"  {rel}  →  {p.name}.retired")

    if "--no-rebuild" not in argv:
        say("[5] 重新构建 exe（让回滚后的 server.py 生效）")
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_exe.py")],
                           cwd=str(ROOT), capture_output=True)
        say(f"  build_exe.py rc={r.returncode}")
    else:
        say("[5] 跳过重建（exe 里仍是本轮代码，需手动重跑 build_exe.py）")

    say("=== 回滚完成 ===")
    say("已恢复的 19 张图片保留在 data/notes 与 data/body，界面仍可显示。")
    say("本轮没有改动数据库；备份在 backup/2026-11-14-images/guanlan.db.orig。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
