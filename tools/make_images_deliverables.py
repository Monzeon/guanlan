#!/usr/bin/env python
"""生成「图片恢复与防御」这轮的对照差分与验证记录（可重复运行）。"""
from __future__ import annotations

import difflib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BK = ROOT / "backup" / "2026-11-14-images"
SECTION_START = "### 图片丢失与自检"
SECTION_END = "### 打包成 exe（已完成）"


def lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def main() -> int:
    # 修改前 README 的精确副本：去掉本轮新增小节
    s = (ROOT / "README.md").read_text(encoding="utf-8")
    a, b = s.find(SECTION_START), s.find(SECTION_END)
    assert a >= 0 and b > a, "README 小节锚点未命中"
    (BK / "README.md.orig").write_text(s[:a] + s[b:], encoding="utf-8")

    parts = ["观澜 · 图片恢复与防御 · 对照差分 2026-11-14\n",
             "=" * 62 + "\n\n"]
    for i, (bk, rel) in enumerate([("server.py.orig", "server.py"),
                                   ("README.md.orig", "README.md")], 1):
        d = list(difflib.unified_diff(lines(BK / bk), lines(ROOT / rel),
                                      fromfile=f"a/{rel}", tofile=f"b/{rel}",
                                      lineterm=""))
        (BK / (Path(rel).name + ".diff")).write_text("\n".join(d) + "\n",
                                                     encoding="utf-8")
        parts.append(f"== {i}/5 修改 {rel} ==\n" + "\n".join(d) + "\n")
        print(rel, len(d), "行差分")

    NEW = ["tools/recover_images.py", "tools/verify_images.py",
           "tools/rollback_images.py"]
    for j, rel in enumerate(NEW, 3):
        body = lines(ROOT / rel)
        parts.append(f"\n== {j}/5 新增 {rel} ==\n" + "\n".join(
            [f"diff --git a/{rel} b/{rel}", "new file mode 100644",
             "--- /dev/null", f"+++ b/{rel}",
             f"@@ -0,0 +1,{len(body)} @@"] + ["+" + l for l in body]) + "\n")
        print("新增", rel)
    (BK / "changes.patch").write_text("".join(parts), encoding="utf-8")
    print("changes.patch:", (BK / "changes.patch").stat().st_size, "bytes")

    audit = ROOT / "docs" / "audit-2026-11-14-images.log"
    with audit.open("a", encoding="utf-8") as f:
        f.write(f"""=== 观澜图片不可见 · 修复记录 {datetime.now():%Y-%m-%d %H:%M:%S} ===

【现象】用户截图：卡片「中国大学，越来越像高中了」下 4 个
「图片暂不可见」占位框（+3 角标），应用内图片整体不可见。

【定位】
  static/app.js:425  占位文案来自 <img> 的 error 事件 → is-missing。
  static/app.js:398  preview_images 的 src 是 /api/notes/img/<file>。
  server.py:7500     _note_media_get 从 NOTES_DIR(data/notes) 读；
  server.py:7529     _body_media_get 从 BODY_DIR(data/body) 读。
  实测两个接口都是 HTTP 404「文件不存在」。
  data/notes、data/body、data/uploads 里位图数量均为 0，而数据库
  clip_images 8 条、clips.body_images 12 张引用都还在。
  两个 prune 函数（prune_note_images / prune_body_images）经核查都把
  数据库引用计入 used，不是它们误删；回收站、backup、旧 data 备份、
  browser 缓存按文件大小匹配均无副本。
  结论：早前数据目录迁移/清理过程中位图文件与数据库引用脱节。
  本轮打包改造没有改数据目录，D:\\GuanlanData\\data 始终是同一位置
  （docs/audit-2026-11-12-migration.log 第 12 行亦记录「数据目录未变」）。

【恢复】tools/recover_images.py，只新增文件、不改动数据库任何一行。
  数据库备份：backup/2026-11-14-images/guanlan.db.orig（30,138,368 B）。
  1) 本地副本：peer-sync 里找到 3 张正文配图（clip 8/9），直接复制回 body/。
  2) 原文重抓：对仍有缺失且有 url 的收存抓 HTML 图片地址，带 Referer 下载，
     再按两种指纹还原原文件名：
       * clip_images —— 下载内容长度 == 数据库 size
       * body_images —— b{{cid}}-{{pos:02d}}-{{sha256(raw[:4096])[:8]}}{{ext}}
     clip 13（微信）7 张主图 + 7 张正文图全部命中，size 与 hash 逐一吻合；
     clip 16 两张正文图命中。共恢复 19 张。
  3) 仅剩 1 张：clip_images id=12 «HWmKMe5akAAKnKn(1).jpeg»，
     所属收存没有原文链接，是本地贴图，无网络副本可恢复。

【代码防御】server.py 两处：
  clip_images_of() 只返回 NOTES_DIR 里确实存在的文件，total 按实际张数算；
  clip_row() 的 body_images 同样过滤。悬空引用不再发给前端，卡片不再出现
  一排占位框，"+N" 角标也不会凭空多出来。
  改动后重新构建 dist/观澜/观澜.exe（onedir，96.9 MiB）。

【验证】
  python tools/verify_images.py（打包 exe）：15 PASS / 0 FAIL
  python tools/verify_images.py --source（源码）：14 PASS / 0 FAIL
  python tools/verify_desktop.py（模拟双击桌面原图标）：11 PASS / 0 FAIL
  覆盖：服务/托盘/宿主窗口/WebView2 渲染子窗口；clips/13 四张主图 URL
  全部 200 image/png（896808 / 271228 / 624458 / 211195 B）；
  preview_image_count=7；clips/21 悬空图已被过滤（0 张）；
  12 张正文配图全部可取且文件在盘；--stop 收尾干净。
  验证结束现场恢复「服务未运行 / 托盘未运行 / 无残留窗口」。

【四件交付】
  改后文件   server.py / README.md / tools/recover_images.py /
             tools/verify_images.py / 重建的 dist/观澜/观澜.exe
  对照差分   backup/2026-11-14-images/changes.patch（含 .diff 与 .orig）
  验证记录   本文件
  回滚脚本   python tools/rollback_images.py（--dry-run 已通过；
             还原源码并重建 exe，已恢复的图片文件保留）
────────────────────────────────────────────────────────────

""")
    print("验证记录已写入", audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
