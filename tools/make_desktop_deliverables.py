#!/usr/bin/env python
"""生成本次「桌面原图标修复」的对照差分与验证记录（可重复运行）。"""
from __future__ import annotations

import difflib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BK = ROOT / "backup" / "2026-11-14-desktop"

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


def lines(p: Path, enc: str) -> list[str]:
    return p.read_bytes().decode(enc, errors="replace").splitlines()


def main() -> int:
    # 1) 修复前 README 的精确副本
    s = (ROOT / "README.md").read_text(encoding="utf-8")
    for new, old in README_PAIRS:
        assert new in s, f"README 锚点未命中: {new[:50]!r}"
        s = s.replace(new, old, 1)
    (BK / "README.md.orig").write_text(s, encoding="utf-8")

    # 2) 三个修改文件的差分（按各自编码读）
    specs = [
        ("launch.vbs.orig", "launch.vbs", "utf-16", "utf-8"),
        ("start.cmd.orig",  "start.cmd",  "gbk",    "utf-8"),
        ("README.md.orig",  "README.md",  "utf-8",  "utf-8"),
    ]
    parts = [f"观澜 · 桌面原图标修复 · 对照差分 {datetime.now():%Y-%m-%d}\n",
             "=" * 62 + "\n\n"]
    for i, (bk, rel, old_enc, new_enc) in enumerate(specs, 1):
        a = lines(BK / bk, old_enc)
        b = lines(ROOT / rel, new_enc)
        d = list(difflib.unified_diff(a, b, fromfile=f"a/{rel}",
                                      tofile=f"b/{rel}", lineterm=""))
        (BK / (Path(rel).name + ".diff")).write_text("\n".join(d) + "\n",
                                                     encoding="utf-8")
        parts.append(f"== {i}/5 修改 {rel} ==\n" + "\n".join(d) + "\n")

    # 3) 两个新增文件
    for i, rel in enumerate(["tools/verify_desktop.py",
                             "tools/rollback_desktop.py"], 4):
        body = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        parts.append(f"\n== {i}/5 新增 {rel} ==\n" + "\n".join(
            [f"diff --git a/{rel} b/{rel}", "new file mode 100644",
             "--- /dev/null", f"+++ b/{rel}",
             f"@@ -0,0 +1,{len(body)} @@"] + ["+" + l for l in body]) + "\n")
    (BK / "changes.patch").write_text("".join(parts), encoding="utf-8")

    # 4) 验证记录
    audit = ROOT / "docs" / "audit-2026-11-14-desktop.log"
    with audit.open("a", encoding="utf-8") as f:
        f.write(f"""=== 桌面原图标修复 {datetime.now():%Y-%m-%d %H:%M:%S} ===

【现象】用户反馈桌面原本的「观澜」图标点了打不开（点了没窗口）。

【根因】11:26:20 的 entry.log 显示桌面双击实际用的是
  C:\\\\Users\\\\<user>\\AppData\\Local\\Programs\\Python\\Python310\\pythonw.exe
（PATH 里第一个 pythonw），而 pywebview/pythonnet 装在 WorkBuddy 的
Python 3.13 环境。native.log 里窗口角色直接
  ModuleNotFoundError: No module named 'webview'
服务与托盘都起来了，只有窗口角色崩溃，于是表现为「点了没反应」。
上一轮的所有验证脚本都用 launch.pythonw()（3.13、有依赖）启动，
掩盖了桌面入口与 PATH 的差异。

【修复】不改用户桌面的 观澜.url（仍指向 launch.vbs），改两个入口脚本：
  launch.vbs  优先遍历 dist/ 子目录找 .exe 直接运行（wscript 静默），
              找不到才回退 pythonw/pyw entry.py 源码形态；
  start.cmd   同样优先 dist 下的 exe，stop/status 仍走 python entry.py。
两个脚本内容全部改成 ASCII，规避 UTF-16/GBK 与中文 exe 路径的编码问题；
用遍历而不是写死中文路径，编码与目录名都无关。
打包 exe 自带 Python 运行时与 WebView2 依赖，因此不再挑剔系统 Python。

【验证】
  python tools/verify_desktop.py（wscript 真实模拟双击）
    11 PASS / 0 FAIL：托盘 / 宿主窗口 / 服务 HTTP / 窗口类名
    WindowsForms10.Window.8... / WebView2 渲染子窗口（非白屏）/
    entry.log 本次 launcher frozen=True（走打包 exe）/
    本轮无 webview 导入失败 / --stop 收尾干净。
  start.cmd status 与 start.cmd 默认启动：均正常，默认启动能开出窗口。
  验证结束现场恢复「服务未运行 / 托盘未运行 / 无残留窗口」。

【四件交付】
  改后文件   launch.vbs / start.cmd / README.md / tools/verify_desktop.py
  对照差分   backup/2026-11-14-desktop/changes.patch（含各 .diff 与 .orig）
  验证记录   本文件
  回滚脚本   python tools/rollback_desktop.py（--dry-run 已通过）
────────────────────────────────────────────────────────────

""")
    print("已生成：README.md.orig / 3 个 .diff / changes.patch / 验证记录")
    print("changes.patch:", (BK / "changes.patch").stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
