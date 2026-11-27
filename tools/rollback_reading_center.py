#!/usr/bin/env python
"""回滚「阅读中心」这次改动（安全模式：默认只预览，不写盘）。

    python tools/rollback_reading_center.py --dry-run
    python tools/rollback_reading_center.py --apply
    python tools/rollback_reading_center.py --apply --drop-data

做了什么：
  1. 删除新增文件（reading.py / reading_schema.sql / reading.js / reading.css / 两份文档）
  2. 把 server.py / static/app.js / README.md 里这次加的片段按精确片段还原
  3. 把 static/index.html 还原成改动前的完整原文
  4. --drop-data 时额外 DROP 掉 7 张阅读表 + FTS 虚表（数据不可恢复，需显式指定）

注意：只还原本次改动的片段，不会用 git checkout 覆盖你更早的未提交修改。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEW_FILES = [
    "reading.py", "reading_schema.sql",
    "static/reading.js", "static/reading.css",
    "docs/阅读中心-设计与实现.md", "docs/阅读中心-变更差分.md",
    "docs/阅读中心.patch",
]

# (文件, 改动后的片段, 改动前的片段) —— 空字符串表示「删掉这段」
REVERTS: list[tuple[str, str, str]] = [
    ("server.py",
     'SCHEMA = ROOT / "schema.sql"\nREADING_SCHEMA = ROOT / "reading_schema.sql"   # 阅读中心独立大板块的表结构\nAPP_LOG = ROOT / "data" / "app.log"',
     'SCHEMA = ROOT / "schema.sql"\nAPP_LOG = ROOT / "data" / "app.log"'),
    ("server.py",
     '        conn.executescript(SCHEMA.read_text(encoding="utf-8"))\n'
     '        if READING_SCHEMA.is_file():\n'
     '            conn.executescript(READING_SCHEMA.read_text(encoding="utf-8"))\n'
     '        migrate_clips(conn)',
     '        conn.executescript(SCHEMA.read_text(encoding="utf-8"))\n'
     '        migrate_clips(conn)'),
    ("server.py",
     '''        # 阅读中心的结构化正文自己持久化（reading_items.doc_text），启动后回填，
        # 否则 clip_annotations 的字符偏移会在重启后全部失锚。
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='reading_items'").fetchone():
            conn.execute("""UPDATE clips SET content_text =
                              (SELECT ri.doc_text FROM reading_items ri WHERE ri.clip_id=clips.id)
                            WHERE source_type='file'
                              AND EXISTS (SELECT 1 FROM reading_items ri2
                                          WHERE ri2.clip_id=clips.id)""")
''', ''),
    ("server.py",
     '''# 正文批注的五种语义色；颜色不是装饰，侧栏会用同一套词（阅读中心与收存共用）。
ANNOTATION_COLORS = {
    "amber": "重点",
    "sage": "论据",
    "blue": "疑问",
    "rose": "反驳",
    "violet": "联想",
}''',
     '''# 正文批注的四种语义色；颜色不是装饰，侧栏会用同一套词。
ANNOTATION_COLORS = {
    "amber": "重点",
    "sage": "论据",
    "blue": "疑问",
    "rose": "反驳",
}'''),
    ("server.py",
     '''    # 阅读中心：虚表没有外键，删条目之前手工收走目录/书签/笔记/关联/索引
    try:
        import reading as _reading_mod
        _reading_mod.purge_clip(conn, int(cid))
    except Exception as exc:
        print(f"[reading] 清理关联数据失败 cid={cid}: {type(exc).__name__}", flush=True)
''', ''),
    ("server.py",
     '''            try:
                import reading as _reading_mod
                _reading_mod.purge_clip(conn, cid)
            except Exception as exc:
                print(f"[reading] 批量清理失败 cid={cid}: {type(exc).__name__}", flush=True)
''', ''),
    ("server.py",
     '''        reading_media = re.fullmatch(r"/api/reading/media/(\\d+)", path)
        if reading_media:
            return self._reading_media(int(reading_media.group(1)))
''', ''),
    ("static/app.js",
     '''  /* 正文批注：五种颜色各有语义，侧栏和正文共用同一套词（阅读中心同步）。 */
  const ANNOTATION_COLORS = {
    amber: { label: "重点", hint: "值得留下" },
    sage: { label: "论据", hint: "可作支撑" },
    blue: { label: "疑问", hint: "待核实" },
    rose: { label: "反驳", hint: "有异议" },
    violet: { label: "联想", hint: "由此及彼" },
  };''',
     '''  /* 正文批注：四种颜色各有语义，侧栏和正文共用同一套词。 */
  const ANNOTATION_COLORS = {
    amber: { label: "重点", hint: "值得留下" },
    sage: { label: "论据", hint: "可作支撑" },
    blue: { label: "疑问", hint: "待核实" },
    rose: { label: "反驳", hint: "有异议" },
  };'''),
    ("static/app.js",
     '''    "insight": "洞察", "review": "回顾", "more": "设置", "reading": "阅读中心",''',
     '''    "insight": "洞察", "review": "回顾", "more": "设置",'''),
    ("static/app.js",
     '''    const path = currentPath();
    // 阅读中心是平级大板块：切侧栏、切整页布局、清掉上一视图的计时器。
    window.GLReading?.onRoute?.(path);
    const seq = ++routeSeq;''',
     '''    const path = currentPath();
    const seq = ++routeSeq;'''),
    ("static/app.js",
     '''    if (path.startsWith("/c/") || path.startsWith("/reading") || path === "/more") return true;''',
     '''    if (path.startsWith("/c/") || path === "/more") return true;'''),
    ("static/app.js",
     '''    if (key === "reading") {
      // 阅读中心有自己的二级导航，高亮交给它自己算
      window.GLReading?.syncNav?.(path);
    } else {
      $$(".nav-item").forEach((el) => {
        el.classList.toggle("active", el.dataset.route === navKey);
      });
      // 手机底栏跟着同一个 key 亮
      $$(".m-item").forEach((el) => {
        el.classList.toggle("on", el.dataset.route === navKey);
      });
    }''',
     '''    $$(".nav-item").forEach((el) => {
      el.classList.toggle("active", el.dataset.route === navKey);
    });
    // 手机底栏跟着同一个 key 亮
    $$(".m-item").forEach((el) => {
      el.classList.toggle("on", el.dataset.route === navKey);
    });'''),
    ("static/app.js",
     '''    if (seq !== routeSeq) return;
    // 收存详情页挂一条「阅读中心 → 这里」的反向引用面板（不改动详情页本身）
    const clipHit = path.match(/^\\/c\\/(\\d+)$/);
    if (clipHit) window.GLReading?.decorateClip?.(clipHit[1]);
    // 单条页面也归在「收存」下面，导航高亮跟着走''',
     '''    if (seq !== routeSeq) return;
    // 单条页面也归在「收存」下面，导航高亮跟着走'''),
    ("static/app.js",
     '''    // 二级导航用 <a href> 直达子路由，别被这里改写成一级路由
    if (el.dataset.route === undefined || el.tagName === "A") return;
    location.hash = "#/" + el.dataset.route;
  }));
  bindBoardAdd();
  window.addEventListener("hashchange", route);
  if (!location.hash) location.hash = "#/";
  // 让 reading.js 先注册完路由再跑首屏；否则刷新在 #/reading 上会被弹回收存
  setTimeout(route, 0);''',
     '''    if (el.dataset.route === undefined) return;
    location.hash = "#/" + el.dataset.route;
  }));
  bindBoardAdd();
  window.addEventListener("hashchange", route);
  if (!location.hash) location.hash = "#/";
  route();'''),
]

# 大块删除：按起止标记切掉（顺序执行）
BLOCK_CUTS: list[tuple[str, str, str]] = [
    ("server.py",
     "# ── 阅读中心：独立大板块的路由装配 ──────────────────────",
     "class Handler(BaseHTTPRequestHandler):"),
    ("server.py",
     "    def _reading_media(self, cid: int):",
     "    def _clip_file(self, cid: int, download: bool = False):"),
    ("static/app.js",
     "  /* ── 给阅读中心暴露的共用能力 ────────────────────────",
     "  /* ── 启动 ──────────────────────────────────────────── */"),
]

ORIGINAL_INDEX_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f5f3ee">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="观澜">
<meta name="format-detection" content="telephone=no">
<title>观澜 · 收存</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="stylesheet" href="/style.css">
<link rel="stylesheet" href="/extra.css">
<link rel="stylesheet" href="/mobile.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230e1a2e'/%3E%3Cpath d='M4 20c3-6 6-6 9 0s6 6 9 0 6-6 6 0' stroke='%23c9973b' stroke-width='2.4' fill='none' stroke-linecap='round'/%3E%3Ccircle cx='24' cy='9' r='2.6' fill='%23efcb84'/%3E%3C/svg%3E">
</head>
<body>

<div class="shell">
  <aside class="side">
    <div class="brand">
      <div class="brand-mark">
        <svg viewBox="0 0 34 34" aria-hidden="true">
          <rect width="34" height="34" rx="7.5" fill="#0e1a2e"/>
          <path d="M5 21c3.2-6.4 6.4-6.4 9.6 0s6.4 6.4 9.6 0 4.8-4.8 4.8 0" stroke="#c9973b" stroke-width="2.6" fill="none" stroke-linecap="round"/>
          <circle cx="24.5" cy="9" r="2.8" fill="#efcb84"/>
        </svg>
      </div>
      <div>
        <div class="brand-name">观澜</div>
        <div class="brand-sub">观水有术，必观其澜 <span class="bs-src">——《孟子》</span></div>
      </div>
    </div>

    <nav class="nav" id="nav">
      <div class="nav-item" data-route=""><span>全部</span><span class="count" id="c-clips">—</span></div>

      <div id="board-list"></div>
      <div class="nav-item nav-add" id="board-add"><span>＋ 新建板块</span></div>
      <div class="board-new" id="board-new">
        <input type="text" id="board-new-input" maxlength="20" placeholder="板块名，回车创建">
        <label class="board-new-kind" title="资料库用来放原件、原始数据和权威原文">
          <input type="checkbox" id="board-new-archive"><span>建成资料库</span></label>
      </div>

      <div class="nav-group">工具</div>
      <div class="nav-item" data-route="ask"><span>问</span></div>
      <div class="nav-item" data-route="insight"><span>洞察</span></div>
      <div class="nav-item" data-route="search"><span>检索</span></div>
      <div class="nav-item" data-route="verdicts"><span>判断</span></div>
      <div class="nav-item" data-route="review"><span>回顾</span></div>
      <div class="nav-item" data-route="more"><span>设置</span></div>
    </nav>

    <div class="side-foot">
      <div><span class="k">AI</span> <span class="v" id="f-ai">检测中</span></div>
      <div><span class="k">库</span> <span class="v" id="f-db">—</span></div>
      <div class="side-acts">
        <button class="side-k" id="side-k" type="button" title="快速跳转 / 搜索（Ctrl+K）">
          <span class="kbd">Ctrl</span><span class="kbd">K</span><span>快速跳转</span></button>
        <button class="theme-toggle" id="theme-toggle" type="button"
          title="切换深色 / 浅色">浅色</button>
      </div>
    </div>
  </aside>

  <main class="main" id="main">
    <div class="loading">载入中…</div>
  </main>
</div>

<nav class="m-bar" id="m-bar" aria-label="主导航">
  <a class="m-item" data-route="" href="#/">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h9"/></svg>
    <span>收存</span></a>
  <a class="m-item" data-route="ask" href="#/ask">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4.4c4.5 0 8.1 2.8 8.1 6.3s-3.6 6.3-8.1 6.3c-.85 0-1.67-.1-2.44-.3l-3.36 2.2.7-3.1C5 14.64 3.9 12.82 3.9 10.7c0-3.5 3.6-6.3 8.1-6.3z"/></svg>
    <span>问</span></a>
  <a class="m-item" data-route="insight" href="#/insight">
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7.6"/><circle cx="12" cy="12" r="2.6"/><path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2"/></svg>
    <span>洞察</span></a>
  <a class="m-item" data-route="search" href="#/search">
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.4"/><path d="M15.8 15.8L20 20"/></svg>
    <span>检索</span></a>
  <a class="m-item" data-route="verdicts" href="#/verdicts">
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.2"/><path d="M8.4 12.2l2.4 2.4 4.8-4.9"/></svg>
    <span>判断</span></a>
  <a class="m-item" data-route="review" href="#/review">
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.2"/><path d="M12 7.6v4.7l3 1.9"/></svg>
    <span>回顾</span></a>
  <a class="m-item" data-route="more" href="#/more">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.4h16M4 15.6h16"/><circle cx="9" cy="8.4" r="2.1" fill="#fffefb"/><circle cx="15" cy="15.6" r="2.1" fill="#fffefb"/></svg>
    <span>设置</span></a>
</nav>

<script src="/app.js"></script>

</body>
</html>
'''

def drop_reading_tables() -> None:
    db = ROOT / "data" / "guanlan.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript("""
            DROP TABLE IF EXISTS reading_fts;
            DROP TABLE IF EXISTS reading_sessions;
            DROP TABLE IF EXISTS reading_links;
            DROP TABLE IF EXISTS reading_notes;
            DROP TABLE IF EXISTS reading_bookmarks;
            DROP TABLE IF EXISTS reading_shelves;
            DROP TABLE IF EXISTS reading_items;
        """)
        conn.commit()
        print("已 DROP 阅读中心全部表")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--drop-data", action="store_true", help="连同阅读数据一起删除")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    print("模式：", "APPLY（写盘）" if apply else "DRY-RUN（只预览）")
    for rel in NEW_FILES:
        p = ROOT / rel
        print(("删除 " if apply else "将删除 ") + rel + ("（存在）" if p.exists() else "（不存在）"))
        if apply and p.exists():
            p.unlink()

    cache: dict[str, str] = {}
    for rel, new, old in REVERTS:
        p = ROOT / rel
        text = cache.get(rel, p.read_text(encoding="utf-8") if p.exists() else "")
        if new in text:
            text = text.replace(new, old, 1)
            print(f"还原片段 OK  {rel}: {new.strip().splitlines()[0][:52]}")
        else:
            print(f"还原片段 SKIP（未找到，可能已回滚）  {rel}: {new.strip().splitlines()[0][:52]}")
        cache[rel] = text

    for rel, start, end in BLOCK_CUTS:
        text = cache.get(rel, (ROOT / rel).read_text(encoding="utf-8"))
        i, j = text.find(start), text.find(end)
        if i >= 0 and j > i:
            text = text[:i] + text[j:]
            print(f"移除代码块 OK  {rel}: {start[:44]}")
        else:
            print(f"移除代码块 SKIP  {rel}: {start[:44]}")
        cache[rel] = text

    # README：删掉新增的「阅读中心」章节
    rp = ROOT / "README.md"
    rtext = rp.read_text(encoding="utf-8")
    m = re.search(r"\n## 阅读中心\n.*?(?=\n## )", rtext, re.S)
    if m:
        rtext = rtext[:m.start()] + rtext[m.end():]
        rtext = rtext.replace(
            "两个平级大板块：收存与阅读中心。侧栏顶部的切换器换的是整套导航，不是加一个菜单项。\n\n| 板块 | 干什么 |",
            "三个页面，没有第四个：\n\n| 页面 | 干什么 |")
        rtext = re.sub(r"^\| \*\*阅读中心\*\* \|.*\|\n", "", rtext, flags=re.M)
        rtext = rtext.replace("- **五种语义色**：重点、论据、疑问、反驳、联想。",
                              "- **四种语义色**：重点、论据、疑问、反驳。")
        print("还原 README 章节 OK")
    else:
        print("还原 README 章节 SKIP")
    cache["README.md"] = rtext

    # index.html 直接整体还原
    cache["static/index.html"] = ORIGINAL_INDEX_HTML
    print("还原 static/index.html 全文 OK")

    if apply:
        for rel, text in cache.items():
            (ROOT / rel).write_text(text, encoding="utf-8")
        if args.drop_data:
            drop_reading_tables()
        print("\n回滚完成。重启观澜服务后生效：python launch.py --stop && python launch.py")
    else:
        print("\n预览结束，未写入任何文件。确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
