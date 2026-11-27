/* 观澜 GUANLAN · 阅读中心（独立大板块）
   ------------------------------------------------------------------
   一整套阅读工作流：资料导入 → 分类书架 → 正文阅读（高亮/批注/书签/
   目录跳转/进度）→ 全文检索 → 笔记沉淀 → 知识库归档与双向引用。
   设计参考 ReadAny：多格式书架、沉浸式分栏阅读、五色高亮 + Markdown
   笔记、AI 伴读（这里走 WorkBuddy 账号云端额度）、阅读统计热力图。
   本文件复用 app.js 导出的 window.GL，不复制一套请求 / 锚定逻辑。 */
(() => {
  "use strict";

  const G = window.GL;
  if (!G) return;
  const {
    $, $$, esc, api, post, toast, glConfirm, glPrompt, markdownHtml, bindMdPreview,
    paragraphHtml, bodyBlocks, prepareAnnotations, offsetInBlock,
    ANNOTATION_COLORS, fmtDate, fmtSize, empty,
  } = G;
  const main = $("#main");

  const KIND_LABEL = { book: "书籍", doc: "文档", audio: "音频", video: "视频", web: "网页", text: "文字" };
  const STATUS_LABEL = { unread: "未读", reading: "在读", done: "读完", paused: "暂搁" };
  const SCOPE_LABEL = { chapter: "正文", annotation: "批注", note: "笔记", bookmark: "书签", meta: "卡片" };
  const NOTE_KIND_LABEL = { note: "笔记", excerpt: "摘录", digest: "提纲", ai: "AI 整理" };
  const AI_ACTIONS = [
    ["summary", "总结本章"], ["explain", "解释选中"], ["outline", "全书提纲"],
    ["translate", "翻译选中"], ["extract", "提炼要点"], ["questions", "思考题"],
  ];

  const PREF_KEY = "gl_reading_prefs";
  const loadPrefs = () => {
    try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch (_) { return {}; }
  };
  const P = Object.assign({
    fontSize: 17, lineHeight: 1.9, width: 720, paper: "paper",
    leftOpen: true,
    // 平板/窄屏先给正文让位，标注栏用右边缘的竖签随时拉出来
    rightOpen: window.innerWidth > 1100,
  }, loadPrefs());
  const savePrefs = () => {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(P)); } catch (_) { }
  };
  // 窄屏（≤900px）时左右栏是覆盖式抽屉，宽屏时是网格里的一列
  const narrow = () => window.matchMedia("(max-width: 900px)").matches;

  const S = {
    shelf: "all", kind: "", status: "", q: "",
    searchQ: "", scope: "all", searchShelf: "",
    notesQ: "", notesKind: "",
    book: null, chapterId: "", leftTab: "toc", rightTab: "annotations",
    tocOpen: {}, secAt: "",
    selection: null, anns: [], jump: null,
    aiStates: {}, aiBusy: false, aiController: null, aiRequest: 0,
    lastTick: 0, lastPos: 0,
  };

  // ── 筛选持久化：刷新后恢复，避免用户以为数据丢失 ──
  const SHELF_PREF_KEY = "gl_shelf_prefs_v1";
  function saveShelfPrefs() {
    try {
      localStorage.setItem(SHELF_PREF_KEY, JSON.stringify({
        shelf: S.shelf, kind: S.kind, status: S.status, q: S.q
      }));
    } catch (_) { }
  }
  function loadShelfPrefs() {
    try {
      const raw = localStorage.getItem(SHELF_PREF_KEY);
      if (raw) {
        const p = JSON.parse(raw);
        if (typeof p.shelf === "string") S.shelf = p.shelf;
        if (typeof p.kind === "string") S.kind = p.kind;
        if (typeof p.status === "string") S.status = p.status;
        if (typeof p.q === "string") S.q = p.q;
      }
    } catch (_) { }
  }
  loadShelfPrefs();

  let cleanups = [];
  const listen = (target, type, fn, opts) => {
    if (!target) return;
    target.addEventListener(type, fn, opts);
    cleanups.push(() => target.removeEventListener(type, fn, opts));
  };
  const interval = (fn, ms) => {
    const id = setInterval(fn, ms);
    cleanups.push(() => clearInterval(id));
    return id;
  };
  const cleanup = () => {
    cleanups.forEach((fn) => { try { fn(); } catch (_) { } });
    cleanups = [];
  };

  /* ── 板块切换 / 二级导航 ───────────────────────────── */

  function onRoute(path) {
    const reading = path.startsWith("/reading");
    const isBook = /^\/reading\/book\/\d+/.test(path);
    document.body.classList.toggle("rc-active", reading);
    document.body.classList.toggle("rc-reader", isBook);
    const vault = $("#nav-vault"), rn = $("#nav-reading");
    if (reading) {
      if (vault) vault.hidden = true;
      if (rn) rn.hidden = false;
    } else {
      // 圣经 / 事件有自己的外壳；这里不替它们提前显示收存侧栏。
      if (rn) rn.hidden = true;
      if (!path.startsWith("/bible") && !path.startsWith("/events") && vault) vault.hidden = false;
    }
    $$("#sect-switch .sect-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.sect === (reading ? "reading" : "vault")));
    cleanup();
    if (!reading) {
      rememberAI();
      S.aiController?.abort();
      S.aiBusy = false;
      S.book = null;
    }
  }

  function syncNav(path) {
    $$(".nav-item").forEach((el) => el.classList.remove("active"));
    $$(".m-item").forEach((el) => el.classList.remove("on"));
    $$("#nav-reading a.nav-item").forEach((a) => {
      if (a.hasAttribute("data-nav-ignore")) return;   // 「＋ 新建书架」不是路由项
      const href = (a.getAttribute("href") || "").replace(/^#/, "");
      a.classList.toggle("active", href === path);
    });
    if (/^\/reading\/shelf\//.test(path)) {
      $$("#reading-shelves a.nav-item").forEach((a) =>
        a.classList.toggle("active", (a.getAttribute("href") || "").replace(/^#/, "") === path));
    }
    $$(".m-item").forEach((el) => el.classList.toggle("on", el.dataset.route === "reading"));
    refreshNavCounts();
  }

  let navCache = { at: 0, data: null };
  async function refreshNavCounts(force = false) {
    if (!force && navCache.data && Date.now() - navCache.at < 20000) {
      paintNav(navCache.data);
      return;
    }
    try {
      const d = await api("/api/reading/overview");
      navCache = { at: Date.now(), data: d };
      paintNav(d);
    } catch (_) { /* 侧栏计数不是主流程 */ }
  }

  function paintNav(d) {
    if (!d || !d.stats) return;
    const n = $("#c-reading"), b = $("#c-books"), nt = $("#c-notes");
    if (n) n.textContent = d.stats.total || 0;
    if (b) b.textContent = d.stats.total || 0;
    if (nt) nt.textContent = d.stats.notes || 0;
    const host = $("#reading-shelves");
    if (host) {
      host.innerHTML = (d.shelves || []).map((s) =>
        `<a class="nav-item" href="#/reading/shelf/${esc(s.id)}"><span>${esc(s.name)}</span>` +
        `<span class="count">${s.count || 0}</span></a>`).join("");
    }
  }

  /* ── 通用 UI ───────────────────────────────────────── */

  function mount(html, cls = "rc-view") {
    main.innerHTML = `<div class="${cls} view-enter">${html}</div>`;
  }

  function head(title, sub, acts = "") {
    return `<header class="rc-head">
      <div><div class="rc-eyebrow">阅读中心 · 我的主要阅读阵地</div>
        <h1>${esc(title)}</h1><p class="rc-sub">${esc(sub)}</p></div>
      <div class="rc-head-acts">${acts}</div></header>`;
  }

  function tabs(items, current, attr) {
    return `<div class="rc-tabs">${items.map(([v, label]) =>
      `<button type="button" class="rc-tab ${String(current) === String(v) ? "on" : ""}"
        ${attr}="${esc(v)}">${esc(label)}</button>`).join("")}</div>`;
  }

  function rcModal({ title, body, okText = "保存", cancelText = "取消", onOpen, onOk, wide = false }) {
    return new Promise((resolve) => {
      const backdrop = document.createElement("div");
      backdrop.className = "gl-modal-backdrop rc-modal-backdrop";
      const box = document.createElement("div");
      box.className = "rc-modal" + (wide ? " wide" : "");
      box.innerHTML = `<div class="rc-modal-head"><strong>${esc(title || "")}</strong>
          <button type="button" class="rc-x" aria-label="关闭">×</button></div>
        <div class="rc-modal-body">${body || ""}</div>
        <div class="rc-modal-acts">
          <button type="button" class="btn small ghost" data-act="cancel">${esc(cancelText)}</button>
          ${okText ? `<button type="button" class="btn small" data-act="ok">${esc(okText)}</button>` : ""}
        </div>`;
      backdrop.appendChild(box);
      document.body.appendChild(backdrop);
      const collect = () => {
        const out = {};
        box.querySelectorAll("[name]").forEach((el) => {
          out[el.name] = el.type === "checkbox" ? el.checked : el.value;
        });
        return out;
      };
      let done = false;
      const close = (value) => {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey, true);
        backdrop.classList.add("out");
        setTimeout(() => backdrop.remove(), 150);
        resolve(value);
      };
      const onKey = (e) => {
        if (e.key === "Escape") { e.stopPropagation(); close(null); }
      };
      document.addEventListener("keydown", onKey, true);
      backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(null); });
      box.querySelector(".rc-x").addEventListener("click", () => close(null));
      box.querySelector('[data-act="cancel"]').addEventListener("click", () => close(null));
      const okBtn = box.querySelector('[data-act="ok"]');
      if (okBtn) okBtn.addEventListener("click", async () => {
        okBtn.disabled = true;
        try {
          const values = collect();
          const result = onOk ? await onOk(values, box) : true;
          if (result !== false) close(values);
        } finally { okBtn.disabled = false; }
      });
      if (onOpen) onOpen(box, close);
      const first = box.querySelector("input,textarea,select");
      if (first) setTimeout(() => first.focus(), 30);
    });
  }

  function icon(name) {
    const paths = {
      book: '<path d="M12 6.2C10.2 4.9 7.7 4.4 4.5 4.6v13.1c3.2-.2 5.7.3 7.5 1.6 1.8-1.3 4.3-1.8 7.5-1.6V4.6c-3.2-.2-5.7.3-7.5 1.6z"/><path d="M12 6.2v13.1"/>',
      search: '<circle cx="11" cy="11" r="6.4"/><path d="M15.8 15.8L20 20"/>',
      add: '<path d="M12 5v14M5 12h14"/>',
      back: '<path d="M15 5l-7 7 7 7"/>',
      download: '<path d="M12 4v11m0 0l-4-4m4 4l4-4M5 19h14"/>',
      file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
      play: '<path d="M8 5l11 7-11 7z"/>',
      foldLeft: '<path d="M13.5 6l-6 6 6 6M19.5 6l-6 6 6 6"/>',
      foldRight: '<path d="M10.5 6l6 6-6 6M4.5 6l6 6-6 6"/>',
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.file}</svg>`;
  }

  function badge(kind, fmt) {
    const label = (fmt || KIND_LABEL[kind] || kind || "资料").toUpperCase().slice(0, 5);
    return `<span class="rc-fmt rc-fmt-${esc(kind)}">${esc(label)}</span>`;
  }

  function fmtChars(n) {
    const v = Number(n) || 0;
    if (v >= 10000) return (v / 10000).toFixed(1) + " 万字";
    if (v >= 1000) return (v / 1000).toFixed(1) + "k 字";
    return v + " 字";
  }

  function progressBar(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<span class="rc-bar"><i style="width:${p.toFixed(1)}%"></i></span>`;
  }

  function bookCard(it) {
    const c = it.clip || {};
    const n = it.counts || {};
    const meta = [
      KIND_LABEL[it.kind] || it.kind,
      it.format ? it.format.toUpperCase() : "",
      it.char_count ? fmtChars(it.char_count) : "",
      c.file_size ? fmtSize(c.file_size) : "",
      (it.meta && it.meta.author) ? it.meta.author : "",
    ].filter(Boolean).join(" · ");
    return `<article class="rc-card" data-cid="${Number(it.clip_id)}">
      <div class="rc-cover rc-cover-${esc(it.kind)}" data-open="${Number(it.clip_id)}">
        <span class="rc-cover-fmt">${esc((it.format || "DOC").toUpperCase())}</span>
      </div>
      <div class="rc-card-body" data-open="${Number(it.clip_id)}">
        <h3>${esc(c.title || "未命名")}</h3>
        <div class="rc-card-meta">${esc(meta)}</div>
        <div class="rc-card-prog">${progressBar(it.progress)}
          <span class="rc-pct">${Math.round(it.progress || 0)}%</span></div>
        <div class="rc-card-foot">
          <span class="rc-status s-${esc(it.status)}">${STATUS_LABEL[it.status] || it.status}</span>
          <span class="rc-counts">标注 ${n.annotations || 0} · 笔记 ${n.notes || 0} · 书签 ${n.bookmarks || 0}</span>
        </div>
      </div>
      <div class="rc-card-acts">
        <button type="button" class="rc-icon-btn" data-act="open" data-id="${Number(it.clip_id)}" title="打开阅读">${icon("book")}</button>
        <button type="button" class="rc-icon-btn" data-act="move" data-id="${Number(it.clip_id)}" title="移到书架">↧</button>
        <button type="button" class="rc-icon-btn" data-act="remove" data-id="${Number(it.clip_id)}" title="移出阅读中心">×</button>
      </div></article>`;
  }

  /* ── 导入 ─────────────────────────────────────────── */

  const ACCEPT = ".pdf,.epub,.mobi,.azw,.azw3,.fb2,.cbz,.txt,.md,.markdown,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.html,.htm,.csv,.tsv,.json,.mp3,.m4a,.wav,.ogg,.opus,.aac,.flac,.mp4,.mkv,.webm,.mov,.avi,.m4v";

  async function uploadOne(file, shelfId) {
    const mime = file.type || ({
      ".pdf": "application/pdf", ".epub": "application/epub+zip",
      ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm",
    }[("." + file.name.split(".").pop().toLowerCase())] || "application/octet-stream");
    const r = await fetch("/api/clips/upload?name=" + encodeURIComponent(file.name), {
      method: "POST",
      headers: { "Content-Type": mime, "X-GL-Client": (() => {
        try { return sessionStorage.getItem("gl_client_id") || ""; } catch (_) { return ""; }
      })() },
      body: file,
    });
    const text = await r.text();
    let j;
    try { j = JSON.parse(text); } catch (_) { throw new Error("上传返回了非 JSON 响应"); }
    if (j.error) throw new Error(j.error);
    if (!r.ok) throw new Error("上传失败 HTTP " + r.status);
    const clip = j.clip || {};
    return post("/api/reading/items", { clip_id: clip.id, shelf_id: shelfId || null });
  }

  function openImport(shelfId, onDone) {
    let files = [];
    const shelfOptions = (window.__rcShelves || []).map((s) =>
      `<option value="${esc(s.id)}" ${String(shelfId) === String(s.id) ? "selected" : ""}>${esc(s.name)}</option>`).join("");
    rcModal({
      title: "导入阅读资料", wide: true, okText: "开始导入",
      body: `<div class="rc-drop" id="rc-drop">
          <div class="rc-drop-ico">${icon("add")}</div>
          <strong>把文件拖到这里，或点此选择</strong>
          <span>PDF / EPUB / MOBI / TXT / DOCX / PPTX / XLSX · 以及音频、视频资料</span>
          <input type="file" id="rc-files" multiple accept="${ACCEPT}" hidden>
        </div>
        <div class="rc-filelist" id="rc-filelist"></div>
        <label class="rc-field"><span>归入书架</span><select name="shelf_id">
          <option value="">未分类</option>${shelfOptions}</select></label>
        <div class="rc-hint">原件、正文与后续批注都留在观澜本机；导入后自动分章建索引。</div>
        <div class="rc-import-alt"><button type="button" class="btn small ghost" id="rc-from-vault">从已有收存挑条目</button></div>`,
      onOpen: (box) => {
        const input = box.querySelector("#rc-files");
        const drop = box.querySelector("#rc-drop");
        const list = box.querySelector("#rc-filelist");
        const paint = () => {
          list.innerHTML = files.length
            ? files.map((f, i) => `<div class="rc-file"><span>${esc(f.name)}</span>
                <span>${fmtSize(f.size)}</span><button type="button" data-i="${i}">×</button></div>`).join("")
            : `<div class="rc-faint">还没有选择文件</div>`;
          list.querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", () => {
            files.splice(Number(b.dataset.i), 1); paint();
          }));
        };
        drop.addEventListener("click", () => input.click());
        input.addEventListener("change", () => { files = [...files, ...Array.from(input.files || [])]; paint(); });
        ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => {
          e.preventDefault(); drop.classList.add("over");
        }));
        ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => {
          e.preventDefault(); drop.classList.remove("over");
        }));
        drop.addEventListener("drop", (e) => {
          files = [...files, ...Array.from(e.dataTransfer?.files || [])]; paint();
        });
        box.querySelector("#rc-from-vault").addEventListener("click", () => {
          openFromVault(shelfId, onDone);
        });
        paint();
      },
      onOk: async (v) => {
        if (!files.length) { toast("先选几个文件", "warn"); return false; }
        let ok = 0, fail = 0, msg = "";
        for (const f of files) {
          try {
            const r = await uploadOne(f, v.shelf_id || null);
            ok++;
            if (r.parse_message) msg = r.parse_message;
          } catch (e) { fail++; toast(`${f.name}：${e.message}`, "err"); }
        }
        toast(`导入 ${ok} 份${fail ? `，失败 ${fail} 份` : ""}${msg ? " · " + msg : ""}`,
          fail ? "warn" : "ok");
        navCache.at = 0;
        onDone?.();
        return true;
      },
    });
  }

  async function openFromVault(shelfId, onDone) {
    let list = [];
    try {
      const d = await api("/api/clips?limit=200");
      list = d.clips || [];
    } catch (e) { toast(e.message, "err"); return; }
    const existing = new Set((window.__rcItems || []).map((x) => Number(x.clip_id)));
    const options = list.filter((c) => !existing.has(Number(c.id))).map((c) =>
      `<label class="rc-pick"><input type="checkbox" value="${Number(c.id)}">
        <span>${esc(c.title || "未命名")}</span>
        <em>${esc(c.file_name || c.channel || "收存")}</em></label>`).join("");
    await rcModal({
      title: "从收存挑条目进阅读中心", wide: true, okText: "加入阅读中心",
      body: options ? `<div class="rc-picks">${options}</div>`
        : `<div class="rc-faint">收存里没有可加入的条目，或者都已经在阅读中心了。</div>`,
      onOk: async (v, box) => {
        const ids = [...box.querySelectorAll("input:checked")].map((el) => Number(el.value));
        if (!ids.length) { toast("先挑几条", "warn"); return false; }
        for (const id of ids) {
          try { await post("/api/reading/items", { clip_id: id, shelf_id: shelfId || null }); }
          catch (e) { toast(e.message, "err"); }
        }
        toast(`已加入 ${ids.length} 份资料`);
        navCache.at = 0;
        onDone?.();
        return true;
      },
    });
  }

  /* ── 书架 / 总览 ───────────────────────────────────── */

  async function viewShelf(match) {
    // route() 传进来的是 path.match(regex) 的结果数组，不是路径字符串
    const path = Array.isArray(match) ? match[0] : String(match || "/reading");
    const m = path.match(/^\/reading\/shelf\/(all|none|\d+)$/);
    S.shelf = m ? m[1] : "all";
    const qs = new URLSearchParams();
    if (S.shelf !== "all") qs.set("shelf", S.shelf);
    if (S.kind) qs.set("kind", S.kind);
    if (S.status) qs.set("status", S.status);
    if (S.q) qs.set("q", S.q);
    qs.set("limit", "200");
    const [d, items] = await Promise.all([
      api("/api/reading/overview"), api("/api/reading/items?" + qs.toString()),
    ]);
    window.__rcShelves = d.shelves || [];
    window.__rcItems = items.items || [];
    const st = d.stats || {};
    const shelfName = S.shelf === "none" ? "未分类"
      : S.shelf === "all" ? "全部资料"
        : (d.shelves.find((s) => String(s.id) === S.shelf)?.name || "书架");

    const statHtml = `<div class="rc-stats">
      ${[["资料", st.total || 0], ["在读", st.reading || 0], ["读完", st.done || 0],
      ["高亮批注", st.annotations || 0], ["笔记", st.notes || 0],
      ["双向关联", st.links || 0], ["阅读时长", fmtDuration(st.seconds || 0)]]
      .map(([k, v]) => `<div class="rc-stat"><strong>${esc(v)}</strong><span>${esc(k)}</span></div>`).join("")}
    </div>`;

    const shelfChips = [[
      "all", `全部 ${st.total || 0}`], ["none", "未分类"],
      ...(d.shelves || []).map((s) => [String(s.id), `${s.name} ${s.count || 0}`]),
    ].map(([v, label]) => `<a class="chip ${S.shelf === v ? "on" : ""}"
        href="${v === "all" ? "#/reading" : "#/reading/shelf/" + esc(v)}">${esc(label)}</a>`).join("");

    const recent = (d.recent || []).filter((x) => (x.progress || 0) > 0 && (x.progress || 0) < 100).slice(0, 3);
    const continueHtml = recent.length ? `<section class="rc-continue">
      <div class="rc-sec-title">继续阅读</div>
      <div class="rc-continue-row">${recent.map((it) => `
        <button type="button" class="rc-continue-card" data-open="${Number(it.clip_id)}">
          <span class="rc-cc-fmt">${esc((it.format || "DOC").toUpperCase())}</span>
          <span class="rc-cc-title">${esc(it.clip?.title || "未命名")}</span>
          ${progressBar(it.progress)}<span class="rc-cc-pct">${Math.round(it.progress)}%</span>
        </button>`).join("")}</div></section>` : "";

    const kindChips = [["", "全部类型"], ...Object.entries(KIND_LABEL)].map(([v, label]) =>
      `<button type="button" class="chip ${S.kind === v ? "on" : ""}" data-rk="${esc(v)}">${esc(label)}</button>`).join("");
    const statusChips = [["", "全部状态"], ...Object.entries(STATUS_LABEL)].map(([v, label]) =>
      `<button type="button" class="chip ${S.status === v ? "on" : ""}" data-rs="${esc(v)}">${esc(label)}</button>`).join("");

    const gridCount = items.items.length;
    const gridClass = gridCount >= 1 && gridCount <= 3 ? `rc-grid rc-grid-${gridCount}` : "rc-grid";

    mount(
      head(shelfName, "导入 → 分类 → 阅读 → 批注 → 检索 → 笔记 → 归档，一处到底", `
        <button type="button" class="btn small ghost" id="rc-add-shelf">＋ 书架</button>
        <button type="button" class="btn small" id="rc-import">${icon("add")} 导入资料</button>`)
      + statHtml
      + continueHtml
      + `<div class="rc-toolbar">
          <div class="rc-chips">${shelfChips}</div>
          <div class="rc-filters"><div class="rc-chips">${kindChips}</div>
            <div class="rc-chips">${statusChips}</div></div>
          <label class="rc-quick"><span>${icon("search")}</span>
            <input id="rc-q" type="search" placeholder="在书架里找标题 / 标签" value="${esc(S.q)}"></label>
        </div>
        <div class="${gridClass}" id="rc-grid">${items.items.length
        ? items.items.map(bookCard).join("")
        : (S.shelf !== "all" || S.kind || S.status || S.q
          ? `<div class="empty"><div class="big">没有符合当前筛选的资料</div><div class="hint">试着清除上面的筛选条件，或换一个关键词。</div><div class="empty-actions"><button type="button" class="btn small ghost" id="rc-clear-filter">清除全部筛选</button></div></div>`
          : empty("书架还是空的", "点右上「导入资料」，PDF / EPUB / 文档 / 音视频都能收进来；也可以从已有收存里挑。"))}</div>
        <div class="rc-foot-note">AI 伴读走 WorkBuddy 账号云端额度 · ${esc(d.ai?.model || "未配置")}</div>`
    );

    bindShelf(d);
  }

  function bindShelf(d) {
    $$("[data-open]").forEach((el) => el.addEventListener("click", () => {
      goBook(Number(el.dataset.open || el.getAttribute("data-open")));
    }));
    $("#rc-import")?.addEventListener("click", () =>
      openImport(S.shelf === "all" || S.shelf === "none" ? "" : S.shelf, () => G.routeNow()));
    $("#rc-add-shelf")?.addEventListener("click", () => newShelf());
    $$("[data-rk]").forEach((el) => el.addEventListener("click", () => {
      S.kind = el.dataset.rk; saveShelfPrefs(); G.routeNow();
    }));
    $$("[data-rs]").forEach((el) => el.addEventListener("click", () => {
      S.status = el.dataset.rs; saveShelfPrefs(); G.routeNow();
    }));
    const clearFilter = $("#rc-clear-filter");
    if (clearFilter) {
      clearFilter.addEventListener("click", () => {
        S.shelf = "all";
        S.kind = "";
        S.status = "";
        S.q = "";
        saveShelfPrefs();
        G.routeNow();
      });
    }
    const q = $("#rc-q");
    if (q) {
      let t = 0;
      listen(q, "input", () => {
        clearTimeout(t);
        t = setTimeout(() => { S.q = q.value.trim(); saveShelfPrefs(); G.routeNow(); }, 320);
      });
    }
    $$("[data-act]").forEach((el) => el.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = Number(el.dataset.id), act = el.dataset.act;
      if (act === "open") return goBook(id);
      if (act === "remove") {
        if (!await glConfirm("移出阅读中心？原件和收存卡片都会留着，只删这里的阅读态。", { danger: true })) return;
        try { await post(`/api/reading/items/${id}/remove`, {}); toast("已移出"); navCache.at = 0; G.routeNow(); }
        catch (err) { toast(err.message, "err"); }
      }
      if (act === "move") openMoveShelf(id, d.shelves || []);
    }));
    // 整页拖放导入
    listen(document, "dragover", (e) => { if (e.dataTransfer?.files?.length) e.preventDefault(); });
    listen(document, "drop", (e) => {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      const fs = Array.from(e.dataTransfer.files);
      (async () => {
        for (const f of fs) {
          try { await uploadOne(f, S.shelf === "all" || S.shelf === "none" ? null : S.shelf); }
          catch (err) { toast(`${f.name}：${err.message}`, "err"); }
        }
        toast(`已导入 ${fs.length} 份资料`); navCache.at = 0; G.routeNow();
      })();
    });
  }

  async function newShelf() {
    const name = await glPrompt("新书架叫什么？", "");
    if (!name || !String(name).trim()) return;
    try {
      await post("/api/reading/shelves", { name: String(name).trim() });
      toast("书架建好了"); navCache.at = 0; G.routeNow();
    } catch (e) { toast(e.message, "err"); }
  }

  async function openMoveShelf(cid, shelves) {
    await rcModal({
      title: "移到哪个书架", okText: "移动",
      body: `<label class="rc-field"><span>书架</span><select name="shelf_id">
          <option value="">未分类</option>${shelves.map((s) =>
        `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></label>`,
      onOk: async (v) => {
        try {
          await post(`/api/reading/items/${cid}`, { shelf_id: v.shelf_id || null });
          toast("已移动"); G.routeNow(); return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
  }

  function fmtDuration(sec) {
    const s = Number(sec) || 0;
    if (s < 60) return s + " 秒";
    if (s < 3600) return (s / 60).toFixed(0) + " 分";
    return (s / 3600).toFixed(1) + " 小时";
  }

  /* ── 阅读器 ───────────────────────────────────────── */

  function goBook(cid, jump) {
    if (jump) {
      try { sessionStorage.setItem("gl_read_jump", JSON.stringify({ clip_id: cid, ...jump })); }
      catch (_) { }
    } else {
      try { sessionStorage.removeItem("gl_read_jump"); } catch (_) { }
    }
    location.hash = "#/reading/book/" + cid;
  }

  function readJump() {
    try {
      const raw = sessionStorage.getItem("gl_read_jump");
      sessionStorage.removeItem("gl_read_jump");
      return raw ? JSON.parse(raw) : null;
    } catch (_) { return null; }
  }

  async function viewBook(m) {
    // 重解析等路径直接调 viewBook 而不走 route()，这里得自己回收上一轮的
    // document 级监听与 15s 进度定时器（cleanup 幂等）
    cleanup();
    const cid = Number(m[1]);
    const d = await api(`/api/reading/book/${cid}`);
    if (d.error) throw new Error(d.error);
    S.book = d;
    S.anns = prepareAnnotations(d.doc_text || "", d.annotations || []);
    S.chapterId = d.current_chapter || (d.chapters[0]?.id || "");
    S.tocOpen = {}; S.secAt = "";   // 换书时别把上一本的展开状态带过来
    S.selection = null;
    S.jump = null;
    restoreAI(cid, S.chapterId);
    const jump = readJump();
    if (jump && Number(jump.clip_id) === cid) {
      S.jump = jump;
      if (jump.chapter_id) S.chapterId = jump.chapter_id;
      if (jump.kind === "annotation") S.rightTab = "annotations";
      if (jump.kind === "note") S.rightTab = "notes";
      if (jump.kind === "bookmark") S.leftTab = "marks";
    }
    renderBookShell(d);
    bindBook(d);
    renderChapter();
    renderLeft();
    renderRight();
    if (S.jump) {
      setTimeout(() => jumpTo(S.jump), 160);
    } else if (Number(d.item.position) > 0) {
      // 续读：把上次记录的字符偏移还原成滚动位置，否则第一次定时保存会把进度抹成 0
      const at = Number(d.item.position);
      setTimeout(() => {
        const art = $("#rc-article"), center = $("#rc-center");
        if (!art || !center) return;
        for (const p of art.querySelectorAll("[data-a-start]")) {
          if (Number(p.dataset.aStart) <= at && Number(p.dataset.aEnd) >= at) {
            setScrollTop(center, revealTop(center, p));
            break;
          }
        }
      }, 90);
    }
    startProgress(d);
    document.title = `观澜 · 阅读 · ${d.clip?.title || "未命名"}`;
  }

  function currentChapter() {
    const chs = S.book?.chapters || [];
    return chs.find((c) => c.id === S.chapterId) || chs[0] || null;
  }

  /* 元素相对滚动容器顶部的距离。不用 offsetTop：外层一旦有定位祖先，
     offsetParent 变了、offsetTop 就整体偏一截，续读和跳转都会落错行。 */
  function topIn(scroller, el) {
    return el.getBoundingClientRect().top - scroller.getBoundingClientRect().top
      + scroller.scrollTop;
  }

  /* 定位到某一段时的落点：留 90px 上文；靠近章首则直接回到章头，别把标题切掉 */
  function revealTop(scroller, el) {
    const top = topIn(scroller, el);
    return top <= 170 ? 0 : top - 90;
  }

  /* .rc-center 带 scroll-behavior: smooth，直接赋 scrollTop 会播一段平滑动画
     （先回顶再滚回），重画正文后落位、续读、跳转都得瞬时到位 */
  function setScrollTop(center, top) {
    if (!center) return;
    const prev = center.style.scrollBehavior;
    center.style.scrollBehavior = "auto";
    center.scrollTop = Math.max(0, top);
    center.style.scrollBehavior = prev;
  }

  function renderBookShell(d) {
    const it = d.item, c = d.clip || {};
    const isMedia = it.kind === "audio" || it.kind === "video";
    const media = isMedia ? `<div class="rc-media-box">
        <${it.kind === "video" ? "video" : "audio"} id="rc-media" controls preload="metadata"
          src="/api/reading/media/${Number(it.clip_id)}"></${it.kind === "video" ? "video" : "audio"}>
        <div class="rc-media-acts">
          <span class="rc-faint">时间戳书签与进度按秒记录</span>
          <button type="button" class="btn small ghost" id="rc-transcript">编辑文字稿</button>
          <a class="btn small ghost" href="/api/clips/${Number(it.clip_id)}/file?dl=1">下载原件</a>
        </div></div>` : "";
    main.innerHTML = `<div class="rc-reader${P.leftOpen ? "" : " no-left"}${P.rightOpen ? "" : " no-right"}" id="rc-reader">
      <header class="rc-rhead">
        <button type="button" class="rc-back" id="rc-back" title="回到书架">${icon("back")}</button>
        <div class="rc-rhead-title"><strong>${esc(c.title || "未命名")}</strong>
          <span>${badge(it.kind, it.format)} · ${d.chapters.length} 章 · ${fmtChars(it.char_count)}</span></div>
        <div class="rc-rhead-prog">${progressBar(it.progress)}<span id="rc-head-pct">${Math.round(it.progress)}%</span></div>
        <div class="rc-rhead-acts">
          <button type="button" class="rc-icon-btn" id="rc-toggle-left" title="目录 / 书签（[）" aria-controls="rc-left" aria-expanded="true">☰</button>
          <button type="button" class="rc-icon-btn" id="rc-font-minus" title="缩小字号">A−</button>
          <button type="button" class="rc-icon-btn" id="rc-font-plus" title="放大字号">A＋</button>
          <button type="button" class="rc-icon-btn" id="rc-paper-btn" title="切换纸张">◐</button>
          <button type="button" class="rc-icon-btn" id="rc-export" title="导出 Markdown">${icon("download")}</button>
          <button type="button" class="rc-icon-btn" id="rc-toggle-right" title="标注 / 笔记 / AI / 关联（]）" aria-controls="rc-right" aria-expanded="true">▤</button>
        </div>
      </header>
      <div class="rc-rgrid">
        <aside class="rc-left" id="rc-left"></aside>
        <section class="rc-center" id="rc-center">
          ${media}
          <div class="rc-paper" id="rc-paper-surface" data-paper="${esc(P.paper)}" data-kind="${esc(it.kind)}"
            style="--rc-fs:${P.fontSize}px;--rc-lh:${P.lineHeight};--rc-w:${P.width}px">
            <div class="rc-chapter-head" id="rc-chapter-head"></div>
            <article class="rc-article" id="rc-article"></article>
            <div class="rc-chapter-nav" id="rc-chapter-nav"></div>
          </div>
        </section>
        <aside class="rc-right" id="rc-right"></aside>
      </div>
      <button type="button" class="rc-rail" id="rc-rail-left" title="展开目录（[）" aria-controls="rc-left">目录</button>
      <button type="button" class="rc-rail" id="rc-rail-right" title="展开标注（]）" aria-controls="rc-right">标注</button>
      <div class="rc-scrim" id="rc-scrim" hidden></div>
      <div class="rc-selbar" id="rc-selbar" hidden></div>
    </div>`;
  }

  /* keepScroll=true：只是刷新高亮时重画正文，把读者钉在原处，不跳回章首。
     切章 / 首次打开 / 目录跳转仍走默认，回到这一章的开头。 */
  function renderChapter(keepScroll = false) {
    const d = S.book, ch = currentChapter();
    const art = $("#rc-article"), chHead = $("#rc-chapter-head"), nav = $("#rc-chapter-nav");
    if (!art) return;
    const center = $("#rc-center");
    const keepTop = keepScroll && center ? center.scrollTop : 0;
    if (!ch) {
      chHead.innerHTML = `<h2>这份资料还没有正文</h2>`;
      art.innerHTML = empty("没有可读取的文字层",
        "音视频或扫描件可以先粘一份文字稿，之后同样能做高亮、书签与全文检索。");
      nav.innerHTML = `<button type="button" class="btn small" id="rc-transcript2">粘贴文字稿</button>`;
      $("#rc-transcript2")?.addEventListener("click", () => openTranscript());
      if (center && keepScroll) setScrollTop(center, keepTop);
      return;
    }
    const secs = ch.sections || [];
    chHead.innerHTML = `<div class="rc-ch-eyebrow">第 ${d.chapters.indexOf(ch) + 1} / ${d.chapters.length} 章${secs.length ? " · " + secs.length + " 节" : ""}${ch.page ? " · 原书第 " + ch.page + " 页" : ""}</div>
      <h2>${esc(ch.title)}</h2>`;
    const slice = (d.doc_text || "").slice(ch.start, ch.end);
    // 目录里有章、正文却是空的（扫描件只留了页码占位符）：别渲染成白屏。
    // 但不能提前 return —— 章节导航和滚动归位仍要刷新，否则沿用上一章的按钮。
    const blank = !slice.trim();
    const blocks = blank ? [] : bodyBlocks(slice).map((b) => ({
      ...b, start: b.start + ch.start, end: b.end + ch.start,
    }));
    // 小节标题按标题排版，其余仍是段落
    const headAt = new Set();
    (ch.sections || []).forEach((sec) => {
      const at = ch.start + Number(sec.offset || 0);
      for (let k = -2; k <= 2; k += 1) headAt.add(at + k);
    });
    art.innerHTML = blank
      ? empty("这一章没有可读取的文字",
          "原件可能是扫描件或图片版；可以先粘一份文字稿，再继续高亮与检索。")
      : blocks.map((b) => {
          const inner = paragraphHtml(b, S.anns);
          if (headAt.has(b.start)) {
            return `<h3 class="rc-sec" data-a-start="${b.start}" data-a-end="${b.end}">${inner.replace(/<br>/g, " ")}</h3>`;
          }
          return `<p data-a-start="${b.start}" data-a-end="${b.end}">${inner}</p>`;
        }).join("");
    const idx = d.chapters.indexOf(ch);
    nav.innerHTML = `<button type="button" class="btn small ghost" id="rc-prev" ${idx <= 0 ? "disabled" : ""}>← 上一章</button>
      <span class="rc-faint">${esc(ch.title)}</span>
      <button type="button" class="btn small ghost" id="rc-next" ${idx >= d.chapters.length - 1 ? "disabled" : ""}>下一章 →</button>`;
    $("#rc-prev")?.addEventListener("click", () => switchChapter(-1));
    $("#rc-next")?.addEventListener("click", () => switchChapter(1));
    if (center) setScrollTop(center, keepScroll ? keepTop : 0);
  }

  function switchChapter(delta) {
    const chs = S.book?.chapters || [];
    const i = chs.findIndex((c) => c.id === S.chapterId);
    const next = chs[Math.max(0, Math.min(chs.length - 1, i + delta))];
    if (!next) return;
    rememberAI();
    S.chapterId = next.id;
    restoreAI(Number(S.book.item.clip_id), next.id);
    renderChapter();
    renderLeft();
    renderRight();
    saveProgress();
  }

  function renderLeft() {
    const host = $("#rc-left");
    if (!host || !S.book) return;
    const d = S.book, chs = d.chapters || [];
    const rows = [];
    chs.forEach((c) => {
      const secs = c.sections || [];
      const lv = Math.min(3, c.level || 1);
      const open = secs.length ? (S.tocOpen[c.id] ?? c.id === S.chapterId) : false;
      const caret = secs.length
        ? `<button type="button" class="rc-toc-caret" data-open="${esc(c.id)}"
            title="${open ? "收起" : "展开"}小节" aria-expanded="${open}">${open ? "▾" : "▸"}</button>`
        : `<span class="rc-toc-caret blank" aria-hidden="true"></span>`;
      rows.push(`<div class="rc-toc-row" style="padding-left:${(lv - 1) * 10}px">${caret}
        <button type="button" class="rc-toc-item ${c.id === S.chapterId ? "on" : ""}" data-ch="${esc(c.id)}"
          title="${esc(c.title)} · 约 ${Math.round((c.chars || 0) / 100)} 百字${secs.length ? " · " + secs.length + " 节" : ""}">
          <span>${esc(c.title)}</span><em>${Math.round((c.chars || 0) / 100)}</em></button></div>`);
      if (!open) return;
      secs.forEach((sec) => {
        const at = Number(c.start || 0) + Number(sec.offset || 0);
        const on = c.id === S.chapterId && S.secAt === c.id + ":" + at;
        rows.push(`<button type="button" class="rc-toc-sec ${on ? "on" : ""}"
          data-sec="${esc(c.id)}:${at}" style="padding-left:${30 + (lv - 1) * 10}px"
          title="${esc(sec.title)}"><span>${esc(sec.title)}</span></button>`);
      });
    });
    const toc = rows.join("");
    const marks = (S.book.bookmarks || []).map((b) => `<div class="rc-mark" data-bid="${Number(b.id)}">
        <button type="button" class="rc-mark-jump" data-jump="${Number(b.offset)}" data-mk-ch="${esc(b.chapter_id || "")}">
          <strong>${esc(b.label || "书签")}</strong><span>${b.time_sec != null ? fmtClock(b.time_sec) : "@" + Math.round(b.offset)}</span></button>
        <button type="button" class="rc-x" data-delmark="${Number(b.id)}" title="删除">×</button></div>`).join("");
    host.innerHTML = `<div class="rc-panel-head">${tabs([["toc", "目录"], ["marks", "书签 " + (d.bookmarks || []).length]], S.leftTab, "data-lt")}
        <button type="button" class="rc-fold" id="rc-fold-left" title="收起目录（[）" aria-label="收起目录">${icon("foldLeft")}</button></div>
      <div class="rc-left-body">${S.leftTab === "toc"
        ? (toc || `<div class="rc-faint pad">还没有章节</div>`)
        : (marks || `<div class="rc-faint pad">还没有书签。选中正文或听音频时点「书签」。</div>`)}</div>
      <div class="rc-left-foot"><button type="button" class="btn small ghost" id="rc-add-mark">＋ 在当前位置加书签</button>
        ${S.book.item.kind === "book" || S.book.item.kind === "doc"
        ? `<button type="button" class="rc-reparse" id="rc-reparse" title="按原件重建目录与正文">重新解析</button>` : ""}</div>`;
    $("#rc-fold-left")?.addEventListener("click", () => setSide("left", false));
    // 目录长了以后，当前章可能滚出视野；收起状态或抽屉在屏外时不动，免得把页面横向拽走
    const cur = host.querySelector(".rc-toc-item.on");
    if (cur && host.getBoundingClientRect().right > 1) cur.scrollIntoView({ block: "nearest" });
    $$("[data-lt]", host).forEach((el) => el.addEventListener("click", () => {
      S.leftTab = el.dataset.lt; renderLeft();
    }));
    // 只绑目录项：书签按钮带 data-jump，用通配的 [data-ch] 会把它一起当切章处理
    $$(".rc-toc-item[data-ch]", host).forEach((el) => el.addEventListener("click", () => {
      S.secAt = "";
      rememberAI();
      S.chapterId = el.dataset.ch;
      restoreAI(Number(S.book.item.clip_id), S.chapterId);
      renderChapter(); renderLeft(); renderRight(); saveProgress();
      if (narrow()) setSide("left", false);   // 抽屉里点完章节就收起来，别挡正文
    }));
    $$("[data-open]", host).forEach((el) => el.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = el.dataset.open;
      S.tocOpen[id] = !(S.tocOpen[id] ?? id === S.chapterId);
      renderLeft();
    }));
    $$("[data-sec]", host).forEach((el) => el.addEventListener("click", () => {
      const [cid, at] = el.dataset.sec.split(":");
      S.secAt = el.dataset.sec;
      jumpTo({ chapter_id: cid, offset: Number(at) });
      if (narrow()) setSide("left", false);
    }));
    $$("[data-jump]", host).forEach((el) => el.addEventListener("click", () => {
      jumpTo({ chapter_id: el.dataset.mkCh, offset: Number(el.dataset.jump) });
      if (narrow()) setSide("left", false);
    }));
    $$("[data-delmark]", host).forEach((el) => el.addEventListener("click", async () => {
      try {
        await post(`/api/reading/bookmarks/${el.dataset.delmark}/delete`, {});
        S.book.bookmarks = S.book.bookmarks.filter((x) => String(x.id) !== el.dataset.delmark);
        renderLeft(); toast("书签已删");
      } catch (e) { toast(e.message, "err"); }
    }));
    $("#rc-add-mark")?.addEventListener("click", () => addBookmark());
    $("#rc-reparse")?.addEventListener("click", async () => {
      const cid = Number(S.book.item.clip_id);
      const ok = await glConfirm(
        "按原件重新解析这本书？\n目录会按原书的章节层级重建；正文里重复的标题会去掉，小节标题会排成标题。\n进度按百分比保留，高亮与批注按原句重新定位。",
        { confirmText: "重新解析" });
      if (!ok) return;
      try {
        const r = await post(`/api/reading/items/${cid}/reparse`, {});
        S.tocOpen = {}; S.secAt = "";
        toast(`已重建 ${r.chapters} 章 · ${r.sections} 节`);
        await viewBook([null, String(cid)]);
      } catch (e) { toast(e.message, "err"); }
    });
  }

  function renderRight(keepScroll = false) {
    const host = $("#rc-right");
    if (!host || !S.book) return;
    // 标注增删只影响「标注」tab；其余 tab 走 keepScroll 刷新会把 AI 的提问
    // 与回答（#rc-ai-q / #rc-ai-out）整体重建掉。切 tab 仍走默认 false 重绘
    if (keepScroll && S.rightTab !== "annotations") return;
    const d = S.book;
    const prevBody = host.querySelector("#rc-right-body");
    const keepTop = keepScroll && prevBody ? prevBody.scrollTop : 0;
    host.innerHTML = `<div class="rc-panel-head">${tabs([["annotations", "标注 " + d.annotations.length],
    ["notes", "笔记 " + d.notes.length], ["ai", "AI 伴读"], ["links", "关联 " + d.links.length]],
      S.rightTab, "data-rt")}
        <button type="button" class="rc-fold" id="rc-fold-right" title="收起标注栏（]）" aria-label="收起标注栏">${icon("foldRight")}</button></div>
      <div class="rc-right-body" id="rc-right-body"></div>`;
    $("#rc-fold-right")?.addEventListener("click", () => setSide("right", false));
    $$("[data-rt]", host).forEach((el) => el.addEventListener("click", () => {
      S.rightTab = el.dataset.rt; renderRight();
    }));
    const body = $("#rc-right-body");
    if (S.rightTab === "annotations") body.innerHTML = annotationsPanel(d);
    if (S.rightTab === "notes") body.innerHTML = notesPanel(d);
    if (S.rightTab === "ai") body.innerHTML = aiPanel(d);
    if (S.rightTab === "links") body.innerHTML = linksPanel(d);
    if (keepTop > 0) body.scrollTop = keepTop;
    bindRight(body);
    if (S.rightTab === "ai") {
      const state = currentAI();
      if (state.answer) bindAIResult(state.answer);
    }
  }

  function annotationsPanel(d) {
    if (!d.annotations.length) return `<div class="rc-ann-empty"><span>“</span>
      <strong>还没有标注</strong><p>选中正文任意一段，用五色谱留下高亮或批注。</p></div>`;
    return d.annotations.map((a) => {
      const color = ANNOTATION_COLORS[a.color] ? a.color : "amber";
      return `<article class="rc-ann ann-${esc(color)}" data-aid="${Number(a.id)}">
        <div class="rc-ann-head"><span>${esc(ANNOTATION_COLORS[color].label)}</span>
          <time>${esc(fmtDate(a.updated_at || a.created_at))}</time></div>
        <blockquote>${esc(a.quote || "")}</blockquote>
        ${a.note ? `<div class="rc-md">${markdownHtml(a.note)}</div>` : ""}
        <div class="rc-ann-acts">
          <button type="button" data-a-locate="${Number(a.id)}">定位</button>
          <button type="button" data-a-edit="${Number(a.id)}">编辑</button>
          <button type="button" data-a-note="${Number(a.id)}">存笔记</button>
          <button type="button" data-a-archive="${Number(a.id)}">归档</button>
          <button type="button" data-a-link="${Number(a.id)}">引用</button>
          <button type="button" class="rc-ann-del" data-a-del="${Number(a.id)}"
            title="取消这条划重点，原文不会改">取消划重点</button>
        </div></article>`;
    }).join("");
  }

  function notesPanel(d) {
    return `<div class="rc-panel-acts"><button type="button" class="btn small" id="rc-new-note">＋ 新建笔记</button>
      <a class="btn small ghost" href="#/reading/notes">全部笔记 →</a></div>` +
      (d.notes.length ? d.notes.map((n) => `<article class="rc-note" data-nid="${Number(n.id)}">
        <div class="rc-note-head"><strong>${esc(n.title || "无标题")}</strong>
          <span class="rc-tag">${NOTE_KIND_LABEL[n.kind] || n.kind}</span></div>
        ${n.source_quote ? `<blockquote>${esc(n.source_quote.slice(0, 180))}</blockquote>` : ""}
        <div class="rc-md">${markdownHtml((n.content_md || "").slice(0, 900))}</div>
        <div class="rc-note-foot">
          ${n.archived_clip_id ? `<a href="#/c/${Number(n.archived_clip_id)}">已汇入知识库 ↗</a>`
        : `<button type="button" data-n-archive="${Number(n.id)}">汇入知识库</button>`}
          <button type="button" data-n-edit="${Number(n.id)}">编辑</button>
          <button type="button" data-n-ai="${Number(n.id)}">AI 整理</button>
          <button type="button" data-n-del="${Number(n.id)}">删除</button>
        </div></article>`).join("")
        : `<div class="rc-faint pad">摘录、批注和读书笔记都沉淀在这里，可一键汇入整体知识库。</div>`);
  }

  function aiStateKey(cid = S.book?.item?.clip_id, chapter = S.chapterId) {
    return `${Number(cid)}:${chapter || "book"}`;
  }

  function currentAI() {
    return S.aiStates[aiStateKey()] || { question: "", answer: null, history: [], scope: "chapter" };
  }

  function rememberAI() {
    if (!S.book) return;
    const old = currentAI();
    const question = $("#rc-ai-q")?.value ?? old.question;
    const scope = $("#rc-ai-scope")?.value || old.scope || "chapter";
    S.aiStates[aiStateKey()] = { ...old, question, scope };
    try {
      sessionStorage.setItem("gl_reading_ai_states", JSON.stringify(S.aiStates));
    } catch (_) { /* 内存仍可保留当前阅读会话 */ }
  }

  function restoreAI(cid, chapter) {
    rememberAI();
    if (!Object.keys(S.aiStates).length) {
      try { S.aiStates = JSON.parse(sessionStorage.getItem("gl_reading_ai_states") || "{}") || {}; }
      catch (_) { S.aiStates = {}; }
    }
    if (!S.aiStates[aiStateKey(cid, chapter)]) S.aiStates[aiStateKey(cid, chapter)] = { question: "", answer: null, history: [], scope: "chapter" };
  }

  function draftOpen() {
    const text = $("#rc-ai-q")?.value.trim();
    return S.aiBusy || !!text || !!currentAI().answer;
  }

  function aiPanel(d) {
    const ai = d.ai || {}, state = currentAI();
    const channel = ai.style === "wb" ? "WorkBuddy AI 伴读" : "AI 伴读";
    return `<div class="rc-ai-quota"><span class="rc-dot ${ai.ready ? "on" : ""}"></span>
      <div><strong>${esc(channel)}</strong><span>${ai.ready
        ? `${esc(ai.model)} · ${esc(ai.quota || "账号云端额度")}` : "未就绪：" + esc(ai.config_error || "通道未配置")}</span></div></div>
      <div class="rc-ai-acts">${AI_ACTIONS.map(([v, l]) =>
        `<button type="button" class="rc-ai-quick" data-ai="${esc(v)}" ${S.aiBusy ? "disabled" : ""}>${esc(l)}</button>`).join("")}</div>
      <div class="rc-ai-chat"><label class="rc-field"><span>阅读范围</span><select id="rc-ai-scope">
        <option value="chapter" ${state.scope !== "book" ? "selected" : ""}>当前章节</option><option value="book" ${state.scope === "book" ? "selected" : ""}>全书检索（最多6个片段）</option></select></label>
        <label class="rc-field"><span>接着问（保留最近6轮）</span>
        <textarea name="ai-q" id="rc-ai-q" rows="3" placeholder="针对当前章节提问；选中文字后会带上选区">${esc(state.question || "")}</textarea></label>
        <button type="button" class="btn small" id="rc-ai-send" ${S.aiBusy ? "disabled" : ""}>发送给 AI</button>
        <button type="button" class="btn small ghost" id="rc-ai-cancel" ${S.aiBusy ? "" : "hidden"}>取消本次请求</button></div>
      <div class="rc-ai-out" id="rc-ai-out">${S.aiBusy ? `<div class="rc-ai-loading" role="status">正在读取资料并等待模型返回；可取消，不会自动保存。</div>` : (state.answer ? aiResultHtml(state.answer) : `<div class="rc-faint pad">AI 回答仅供核对；发送范围、节选与出处会标明，不会自动保存。</div>`)}</div>`;
  }

  function linksPanel(d) {
    const rows = (d.links || []).map((l) => `<div class="rc-link-row">
        <span class="rc-link-kind">${esc(l.src_kind)}</span><span class="rc-link-arrow">→</span>
        <span class="rc-link-kind">${esc(l.dst_kind)}</span>
        <span class="rc-link-title">${esc(l.dst_clip_title || l.dst_id || "")}</span>
        <span class="rc-tag">${esc(l.relation)}</span>
        <button type="button" class="rc-x" data-l-del="${Number(l.id)}" title="删除关联">×</button></div>`).join("");
    const back = (d.backlinks || []).map((l) => `<div class="rc-link-row">
        <span class="rc-link-kind">${esc(l.src_kind)}</span><span class="rc-link-arrow">↩</span>
        <span class="rc-link-title">${esc(l.src_id)}</span><span class="rc-tag">反向引用</span></div>`).join("");
    return `<div class="rc-panel-acts"><button type="button" class="btn small" id="rc-new-link">＋ 关联到收存 / 正典 / 板块</button></div>
      <div class="rc-sec-sm">从阅读中心出发</div>${rows || `<div class="rc-faint">还没有关联</div>`}
      <div class="rc-sec-sm">从知识库回溯到这里</div>${back || `<div class="rc-faint">暂无反向引用</div>`}
      <div class="rc-hint">批注、摘录、笔记经「归档」生成知识卡后，会自动留下正反两条引用。</div>`;
  }

  function bindRight(host) {
    const d = S.book, cid = Number(d.item.clip_id);
    $$("[data-a-locate]", host).forEach((el) => el.addEventListener("click", () => {
      const a = d.annotations.find((x) => String(x.id) === el.dataset.aLocate);
      if (a) jumpTo({ chapter_id: chapterOf(a.start_offset), offset: a.start_offset, ref_id: a.id, kind: "annotation" });
    }));
    $$("[data-a-edit]", host).forEach((el) => el.addEventListener("click", () => editAnnotation(
      d.annotations.find((x) => String(x.id) === el.dataset.aEdit))));
    $$("[data-a-note]", host).forEach((el) => el.addEventListener("click", () => annotationToNote(
      d.annotations.find((x) => String(x.id) === el.dataset.aNote))));
    $$("[data-a-archive]", host).forEach((el) => el.addEventListener("click", () => openArchive(
      "annotation", el.dataset.aArchive)));
    $$("[data-a-link]", host).forEach((el) => el.addEventListener("click", () => openLink(
      "annotation", el.dataset.aLink)));
    $$("[data-a-del]", host).forEach((el) => el.addEventListener("click", () => deleteAnnotations(
      [el.dataset.aDel])));
    $$("[data-n-archive]", host).forEach((el) => el.addEventListener("click", () => openArchive(
      "note", el.dataset.nArchive)));
    $$("[data-n-edit]", host).forEach((el) => el.addEventListener("click", () => openNote(
      d.notes.find((x) => String(x.id) === el.dataset.nEdit))));
    $$("[data-n-ai]", host).forEach((el) => el.addEventListener("click", () => polishNote(
      d.notes.find((x) => String(x.id) === el.dataset.nAi))));
    $$("[data-n-del]", host).forEach((el) => el.addEventListener("click", async () => {
      if (!await glConfirm("删掉这条笔记？", { danger: true })) return;
      await post(`/api/reading/notes/${el.dataset.nDel}/delete`, {});
      d.notes = d.notes.filter((x) => String(x.id) !== el.dataset.nDel);
      renderRight(); toast("已删除");
    }));
    $("#rc-new-note", host)?.addEventListener("click", () => openNote(null));
    $("#rc-new-link", host)?.addEventListener("click", () => openLink("clip", String(cid)));
    $$("[data-l-del]", host).forEach((el) => el.addEventListener("click", async () => {
      await post(`/api/reading/links/${el.dataset.lDel}/delete`, {});
      d.links = d.links.filter((x) => String(x.id) !== el.dataset.lDel);
      renderRight(); toast("关联已删");
    }));
    $$("[data-ai]", host).forEach((el) => el.addEventListener("click", () => runAI(el.dataset.ai, "")));
    $("#rc-ai-scope", host)?.addEventListener("change", rememberAI);
    $("#rc-ai-q", host)?.addEventListener("input", rememberAI);
    $("#rc-ai-cancel", host)?.addEventListener("click", () => {
      ++S.aiRequest;
      S.aiController?.abort();
      S.aiBusy = false;
      S.aiController = null;
      toast("已取消本次请求", "warn");
      renderRight();
    });
    $("#rc-ai-send", host)?.addEventListener("click", () => {
      const q = ($("#rc-ai-q")?.value || "").trim();
      if (!q) { toast("先写个问题", "warn"); return; }
      runAI("chat", q);
    });
  }

  /* ── 选区 / 高亮 / 批注 ───────────────────────────── */

  function closestP(node) {
    const el = node && node.nodeType === 1 ? node : node?.parentElement;
    return el?.closest ? el.closest("[data-a-start]") : null;
  }

  function onSelection(e) {
    const art = $("#rc-article");
    if (!art || !S.book) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) { hideSelbar(); return; }
    const range = sel.getRangeAt(0);
    const p1 = closestP(range.startContainer), p2 = closestP(range.endContainer);
    if (!p1 || !p2 || !art.contains(p1) || !art.contains(p2)) { hideSelbar(); return; }
    const o1 = offsetInBlock(p1, range.startContainer, range.startOffset);
    const o2 = offsetInBlock(p2, range.endContainer, range.endOffset);
    if (o1 === null || o2 === null) { hideSelbar(); return; }
    const a = Number(p1.dataset.aStart) + o1;
    const b = Number(p2.dataset.aStart) + o2;
    const start = Math.min(a, b), end = Math.max(a, b);
    if (end <= start) { hideSelbar(); return; }
    const text = S.book.doc_text || "";
    S.selection = {
      start, end, quote: text.slice(start, end),
      prefix: text.slice(Math.max(0, start - 80), start),
      suffix: text.slice(end, end + 80),
    };
    showSelbar(range);
  }

  /* 当前选区压住了哪些已有高亮：给工具条补「取消划重点」用。 */
  function overlapAnnotations() {
    const s = S.selection;
    if (!s) return [];
    return (S.anns || []).filter((a) => !a._orphan
      && Number(a._start) < s.end && Number(a._end) > s.start);
  }

  function showSelbar(range) {
    const bar = $("#rc-selbar");
    if (!bar) return;
    const rect = range.getBoundingClientRect();
    const overlaps = overlapAnnotations();
    const label = "取消划重点" + (overlaps.length > 1 ? `（${overlaps.length}）` : "");
    const unmark = overlaps.length
      ? `<span class="rc-sel-sep"></span><button type="button" class="rc-sel-unmark"
          data-sel="unmark" title="取消这段里的划重点，原文不会改">${label}</button>`
      : "";
    bar.innerHTML = `<div class="rc-swatches">${Object.entries(ANNOTATION_COLORS).map(([k, v]) =>
      `<button type="button" class="rc-sw ann-${esc(k)}" data-color="${esc(k)}" title="${esc(v.label)}：${esc(v.hint)}"></button>`).join("")}</div>
      <span class="rc-sel-sep"></span>
      <button type="button" data-sel="note">批注</button>
      <button type="button" data-sel="excerpt">摘录成笔记</button>
      <button type="button" data-sel="bookmark">书签</button>
      <button type="button" data-sel="ai">问 AI</button>
      <button type="button" data-sel="copy">复制</button>${unmark}`;
    bar.hidden = false;
    const w = bar.offsetWidth || 340;
    bar.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, rect.left + rect.width / 2 - w / 2)) + "px";
    bar.style.top = Math.max(8, rect.top - bar.offsetHeight - 10) + "px";
    $$("[data-color]", bar).forEach((el) => el.addEventListener("click", () => createAnnotation(el.dataset.color)));
    $$("[data-sel]", bar).forEach((el) => el.addEventListener("click", () => selAction(el.dataset.sel)));
  }

  function hideSelbar() {
    const bar = $("#rc-selbar");
    if (bar) bar.hidden = true;
    S.selection = null;
  }

  /* sel：显式传入的选区快照。批注弹窗打开期间，document 的 mousedown 监听会把
     S.selection 清空，所以弹窗路径必须带着打开那一刻的快照进来。 */
  async function createAnnotation(color, note = "", sel = null) {
    const target = sel || S.selection;
    if (!target || !S.book) return;
    const cid = Number(S.book.item.clip_id);
    try {
      const r = await post(`/api/annotations/${cid}`, { ...target, color, note });
      // 检索重建失败只丢索引，不该让标注看起来「没保存」而被重复提交
      try { await post("/api/reading/reindex", { clip_id: cid }); } catch (_) { /* 忽略 */ }
      S.book.annotations.push(r.annotation);
      S.anns = prepareAnnotations(S.book.doc_text || "", S.book.annotations);
      renderChapter(true); renderRight(true); hideSelbar();
      window.getSelection()?.removeAllRanges();
      toast(`已标为「${ANNOTATION_COLORS[color]?.label || color}」`);
    } catch (e) { toast(e.message, "err"); }
  }

  /* 取消划重点：删标注 → 重建检索 → 原位重画正文（不跳回章首）。
     右栏卡片与选区工具条共用；一段选区压住几条就一次清掉几条。 */
  async function deleteAnnotations(ids) {
    const list = [...new Set((ids || []).map(String))].filter(Boolean);
    if (!list.length || !S.book) return;
    if (!(await glConfirm(list.length > 1
      ? `取消这 ${list.length} 条划重点？原文不会改。`
      : "取消这条划重点？原文不会改。",
      { danger: true, confirmText: "取消划重点" }))) return;
    const cid = Number(S.book.item.clip_id);
    try {
      // 逐条 await 中途失败会让「后端已删」和「界面还留着」对不上，
      // 一次并发发完再按实际结果收口
      const results = await Promise.allSettled(list.map((id) =>
        post(`/api/annotations/${Number(id)}/delete`, {})));
      const okIds = list.filter((_, i) => results[i].status === "fulfilled");
      const failed = list.length - okIds.length;
      if (!okIds.length) { toast("划重点没删掉，稍后再试", "err"); return; }
      try { await post("/api/reading/reindex", { clip_id: cid }); } catch (_) { /* 检索重建失败不挡删除 */ }
      S.book.annotations = S.book.annotations.filter(
        (a) => !okIds.includes(String(a.id)));
      S.anns = prepareAnnotations(S.book.doc_text || "", S.book.annotations);
      renderChapter(true); renderRight(true); hideSelbar();
      window.getSelection()?.removeAllRanges();
      const left = failed ? `，${failed} 条没删掉，可重试` : "";
      toast(failed
        ? `已取消 ${okIds.length} 条${left}`
        : (okIds.length > 1 ? `已取消 ${okIds.length} 条划重点` : "划重点已取消"),
        failed ? "err" : "ok");
    } catch (e) { toast(e.message, "err"); }
  }

  async function selAction(act) {
    const s = S.selection;
    if (!s) return;
    if (act === "note") {
      // 弹窗打开后点「保存批注」会触发 document 的 mousedown，S.selection 会被清空，
      // 先把选区快照捏在手里，onOk 里传给 createAnnotation
      const sel = S.selection;
      const note = await rcModal({
        title: "给这段加批注", okText: "保存批注",
        body: `<blockquote class="rc-quote">${esc(s.quote.slice(0, 600))}</blockquote>
          <label class="rc-field"><span>颜色</span><select name="color">
          ${Object.entries(ANNOTATION_COLORS).map(([k, v]) =>
          `<option value="${esc(k)}">${esc(v.label)}</option>`).join("")}</select></label>
          <label class="rc-field"><span>批注（支持 Markdown）</span>
            <textarea name="note" rows="5" data-md-preview placeholder="写下你的理解、疑问或反驳"></textarea></label>`,
        onOpen: (box) => bindMdPreview(box),
        onOk: async (v) => { await createAnnotation(v.color, v.note, sel); return true; },
      });
      return note;
    }
    if (act === "excerpt") {
      await openNote(null, { source_quote: s.quote, title: `摘录 · ${s.quote.slice(0, 20)}` });
      hideSelbar();
      return;
    }
    if (act === "bookmark") {
      await addBookmark({ offset: s.start, label: s.quote.slice(0, 30) });
      hideSelbar();
      return;
    }
    if (act === "ai") {
      S.rightTab = "ai"; renderRight();
      const qbox = $("#rc-ai-q");
      if (qbox) qbox.value = `请解释这段文字：「${s.quote.slice(0, 160)}」`;
      await runAI("explain", "", s.quote);
      hideSelbar();
      return;
    }
    if (act === "unmark") {
      const ids = overlapAnnotations().map((a) => a.id);
      // 不先 hideSelbar：确认框点取消时选区与工具条得留着（成功分支里统一隐藏）
      await deleteAnnotations(ids);
      return;
    }
    if (act === "copy") {
      try { await navigator.clipboard.writeText(s.quote); toast("已复制"); }
      catch (_) { toast("复制失败", "err"); }
      hideSelbar();
    }
  }

  async function editAnnotation(a) {
    if (!a) return;
    const cid = Number(S.book.item.clip_id);
    await rcModal({
      title: "编辑高亮 / 批注", okText: "保存",
      body: `<blockquote class="rc-quote">${esc(a.quote || "")}</blockquote>
        <label class="rc-field"><span>颜色</span><select name="color">
        ${Object.entries(ANNOTATION_COLORS).map(([k, v]) =>
        `<option value="${esc(k)}" ${a.color === k ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select></label>
        <label class="rc-field"><span>批注（支持 Markdown）</span>
          <textarea name="note" rows="5" data-md-preview>${esc(a.note || "")}</textarea></label>`,
      onOpen: (box) => bindMdPreview(box),
      onOk: async (v) => {
        try {
          const r = await post(`/api/annotations/${a.id}/update`, { color: v.color, note: v.note });
          await post("/api/reading/reindex", { clip_id: cid });
          Object.assign(a, r.annotation);
          S.anns = prepareAnnotations(S.book.doc_text || "", S.book.annotations);
          renderChapter(true); renderRight(true); toast("已保存");
          return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
  }

  async function annotationToNote(a) {
    if (!a) return;
    await openNote(null, {
      title: `摘录 · ${(a.quote || "").slice(0, 20)}`,
      source_quote: a.quote, content_md: a.note || "",
      source_annotation_id: a.id, kind: "excerpt",
    });
  }

  function chapterOf(offset) {
    const chs = S.book?.chapters || [];
    const hit = chs.find((c) => c.start <= offset && offset < c.end);
    return (hit || chs[0] || {}).id || "";
  }

  /* ── 书签 / 文字稿 / 进度 ─────────────────────────── */

  async function addBookmark(extra = {}) {
    if (!S.book) return;
    const cid = Number(S.book.item.clip_id);
    const media = $("#rc-media");
    const isTime = S.book.item.position_unit === "second" && media;
    const offset = extra.offset ?? (isTime ? media.currentTime : currentPosition());
    const label = await glPrompt("书签名字", extra.label || `位置 ${Math.round(offset)}`);
    if (label === null) return;
    try {
      const r = await post(`/api/reading/bookmarks/${cid}`, {
        offset, time_sec: isTime ? media.currentTime : null,
        chapter_id: S.chapterId, label: String(label || "书签").slice(0, 60),
        note: "",
      });
      S.book.bookmarks.push(r.bookmark);
      renderLeft(); toast("书签已加");
    } catch (e) { toast(e.message, "err"); }
  }

  function fmtClock(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  async function openTranscript() {
    if (!S.book) return;
    const cid = Number(S.book.item.clip_id);
    await rcModal({
      title: "粘贴文字稿", wide: true, okText: "重建目录与索引",
      body: `<div class="rc-hint">音视频、扫描件、无文字层的 EPUB 都能用文字稿接入完整阅读能力。</div>
        <label class="rc-field"><span>文字稿（纯文本 / Markdown，用空行分段，标题行自动成章）</span>
          <textarea name="text" rows="14" placeholder="第一章 绪论&#10;&#10;正文……">${esc(S.book.doc_text || "")}</textarea></label>`,
      onOk: async (v) => {
        try {
          await post(`/api/reading/items/${cid}/transcript`, { text: v.text });
          toast("文字稿已重建"); G.routeNow(); return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
  }

  function currentPosition() {
    const d = S.book;
    if (!d) return 0;
    const media = $("#rc-media");
    if (media && !isNaN(media.currentTime)) return media.currentTime;
    const center = $("#rc-center"), art = $("#rc-article");
    if (!center || !art) return d.item.position || 0;
    const top = center.getBoundingClientRect().top + 60;
    const ps = art.querySelectorAll("[data-a-start]");
    for (const p of ps) {
      if (p.getBoundingClientRect().top >= top) return Number(p.dataset.aStart) || 0;
    }
    const ch = currentChapter();
    return ch ? ch.end : 0;
  }

  async function saveProgress() {
    const d = S.book;
    if (!d) return;
    const cid = Number(d.item.clip_id);
    const pos = currentPosition();
    const now = Date.now();
    const seconds = Math.max(0, Math.min(300, Math.round((now - (S.lastTick || now)) / 1000)));
    S.lastTick = now;
    const chars = Math.max(0, Math.round(pos - (S.lastPos || pos)));
    S.lastPos = pos;
    try {
      const r = await post(`/api/reading/progress/${cid}`, {
        position: pos, seconds, chars,
        duration: $("#rc-media")?.duration || d.item.duration || 0,
      });
      if (r && typeof r.progress === "number") {
        d.item.progress = r.progress; d.item.status = r.status;
        const bar = $("#rc-rhead-prog .rc-bar i");
        if (bar) bar.style.width = r.progress.toFixed(1) + "%";
        const pct = $("#rc-head-pct");
        if (pct) pct.textContent = Math.round(r.progress) + "%";
      }
    } catch (_) { /* 进度写失败不打断阅读 */ }
  }

  function startProgress(d) {
    S.lastTick = Date.now();
    S.lastPos = Number(d.item.position || 0);
    const center = $("#rc-center");
    if (center) {
      let t = 0;
      listen(center, "scroll", () => {
        clearTimeout(t);
        t = setTimeout(() => { /* 滚动位置留给定时保存，避免刷接口 */ }, 400);
      });
    }
    interval(saveProgress, 15000);
    const media = $("#rc-media");
    if (media) {
      listen(media, "loadedmetadata", () => {
        post(`/api/reading/progress/${Number(d.item.clip_id)}`, {
          position: 0, seconds: 0, duration: media.duration || 0,
        }).catch(() => { });
      });
      listen(media, "timeupdate", () => {
        const t = Math.floor(media.currentTime);
        if (t % 10 === 0 && media.dataset.lastSave !== String(t)) {
          media.dataset.lastSave = String(t);
          saveProgress();
        }
      });
    }
  }

  function jumpTo(j) {
    if (!j || !S.book) return;
    if (j.chapter_id) {
      const exists = (S.book.chapters || []).some((c) => c.id === j.chapter_id);
      if (exists) S.chapterId = j.chapter_id;
    }
    renderChapter(); renderLeft();
    const art = $("#rc-article"), center = $("#rc-center");
    if (!art || !center) return;
    const target = Number(j.offset || 0);
    let el = null;
    for (const p of art.querySelectorAll("[data-a-start]")) {
      if (Number(p.dataset.aStart) <= target && Number(p.dataset.aEnd) >= target) { el = p; break; }
    }
    if (!el && j.ref_id && j.kind === "annotation") {
      const mark = art.querySelector(`mark[data-ann-id="${Number(j.ref_id)}"]`);
      el = mark?.closest("p");
    }
    if (el) {
      setTimeout(() => {
        setScrollTop(center, revealTop(center, el));
        el.classList.add("rc-flash");
        setTimeout(() => el.classList.remove("rc-flash"), 1600);
      }, 40);
    }
  }

  /* ── 笔记 / 归档 / 关联 / AI ──────────────────────── */

  async function openNote(note, preset = {}) {
    if (!S.book) return;
    const cid = Number(S.book.item.clip_id);
    const n = note || {};
    // tags 存的是 JSON 数组字符串；按原样回填会显示成 "主题", "作者" 并在保存后污染标签
    let tagsText = "";
    try {
      const arr = JSON.parse(n.tags || "[]");
      tagsText = Array.isArray(arr) ? arr.join(", ") : String(n.tags || "");
    } catch (_) { tagsText = String(n.tags || "").replace(/^\[|\]$/g, ""); }
    const v = await rcModal({
      title: note ? "编辑笔记" : "新建笔记 / 摘录", wide: true, okText: "保存",
      body: `<label class="rc-field"><span>标题</span><input name="title" value="${esc(n.title ?? preset.title ?? "")}"></label>
        <label class="rc-field"><span>类型</span><select name="kind">
          ${Object.entries(NOTE_KIND_LABEL).map(([k, l]) =>
        `<option value="${esc(k)}" ${(n.kind || preset.kind || "note") === k ? "selected" : ""}>${esc(l)}</option>`).join("")}
        </select></label>
        <label class="rc-field"><span>原文摘录</span><textarea name="source_quote" rows="3">${esc(n.source_quote ?? preset.source_quote ?? "")}</textarea></label>
        <label class="rc-field"><span>笔记（Markdown）</span><textarea name="content_md" rows="9" data-md-preview>${esc(n.content_md ?? preset.content_md ?? "")}</textarea></label>
        <label class="rc-field"><span>标签（逗号分隔）</span><input name="tags" value="${esc(tagsText)}"></label>`,
      onOpen: (box) => bindMdPreview(box),
      onOk: async (val, box) => {
        const payload = {
          clip_id: cid, title: val.title, content_md: val.content_md,
          source_quote: val.source_quote, kind: val.kind, tags: val.tags,
          chapter_id: S.chapterId,
          source_annotation_id: n.source_annotation_id || preset.source_annotation_id || null,
        };
        try {
          if (note) {
            const r = await post(`/api/reading/notes/${n.id}`, payload);
            Object.assign(note, r.note);
          } else {
            const r = await post("/api/reading/notes", payload);
            S.book.notes.unshift(r.note);
          }
          S.rightTab = "notes"; renderRight(); toast("笔记已保存");
          return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
    return v;
  }

  async function polishNote(n) {
    if (!n) return;
    S.rightTab = "ai"; renderRight();
    const out = await runAI("polish", "", "", n.content_md || "");
    if (!out) return;
    await rcModal({
      title: "AI 整理稿 · 原稿保留", wide: true, okText: "另存为整理稿",
      body: `<p class="rc-hint">AI 不会直接覆盖原笔记。请核对后另存，原稿保持不变。</p>
        <div class="rc-polish-compare"><section><h3>原稿</h3><div class="rc-md">${markdownHtml(n.content_md || "")}</div></section>
        <section><h3>AI 整理稿</h3><div class="rc-md">${markdownHtml(out)}</div></section></div>`,
      onOk: async () => {
        try {
          const r = await post("/api/reading/notes", {
            clip_id: Number(S.book.item.clip_id), title: `${n.title || "阅读笔记"} · AI 整理`,
            content_md: out, kind: "ai", chapter_id: n.chapter_id || S.chapterId,
            source_quote: n.source_quote || "", source_annotation_id: n.source_annotation_id || null,
            tags: "AI伴读",
          });
          S.book.notes.unshift(r.note);
          S.rightTab = "notes"; renderRight(); toast("已另存整理稿，原稿未改");
          return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
  }

  async function openArchive(sourceKind, sourceId) {
    if (!S.book) return;
    const cid = Number(S.book.item.clip_id);
    const boards = S.book.boards || [];
    const src = sourceKind === "annotation"
      ? S.book.annotations.find((a) => String(a.id) === String(sourceId))
      : S.book.notes.find((n) => String(n.id) === String(sourceId));
    const defaultTitle = src ? (src.title || `摘录 · ${String(src.quote || src.source_quote || "").slice(0, 20)}`) : "阅读摘录";
    await rcModal({
      title: "汇入整体知识库", okText: "生成知识卡",
      body: `<div class="rc-hint">会在收存里生成一张可检索的知识卡，并自动留下正反两条引用，
          从卡片可以回溯到这条${sourceKind === "annotation" ? "批注" : "笔记"}。</div>
        <label class="rc-field"><span>卡片标题</span><input name="title" value="${esc(defaultTitle)}"></label>
        <label class="rc-field"><span>归入板块</span><select name="board_id">
          <option value="">不指定</option>${boards.map((b) =>
        `<option value="${esc(b.id)}">${esc(b.name)}${b.kind === "archive" ? " ▣" : ""}</option>`).join("")}
        </select></label>
        <label class="rc-field"><span>补充标签</span><input name="tags" placeholder="主题, 作者, 年份"></label>`,
      onOk: async (v) => {
        try {
          const r = await post("/api/reading/archive", {
            clip_id: cid, source_kind: sourceKind, source_id: Number(sourceId),
            board_id: v.board_id || null, title: v.title, tags: v.tags,
            chapter_id: S.chapterId,
          });
          toast(r.duplicate ? "这条已经汇入过知识库" : "已生成知识卡并建立双向引用");
          const fresh = await api(`/api/reading/book/${cid}`);
          S.book.links = fresh.links; S.book.notes = fresh.notes;
          renderRight(); navCache.at = 0;
          return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
  }

  async function openLink(srcKind, srcId) {
    if (!S.book) return;
    const cid = Number(S.book.item.clip_id);
    let clips = [];
    try { clips = (await api("/api/clips?limit=200")).clips || []; } catch (_) { }
    const opts = [
      ["clip", "收存卡片", clips.map((c) => [String(c.id), c.title || "未命名"])],
      ["claim", "正典命题", (S.book.claims || []).map((c) => [c.code, c.statement])],
      ["board", "板块", (S.book.boards || []).map((b) => [String(b.id), b.name])],
    ];
    await rcModal({
      title: "建立双向引用", okText: "关联",
      body: `<label class="rc-field"><span>关联到</span><select name="kind" id="rc-link-kind">
          ${opts.map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join("")}</select></label>
        <label class="rc-field"><span>目标</span><select name="id" id="rc-link-id"></select></label>
        <label class="rc-field"><span>关系</span><select name="relation">
          <option value="refs">引用</option><option value="cites">引证</option>
          <option value="supports">支持</option><option value="derived_from">源自</option>
          <option value="belongs_to">归属</option></select></label>`,
      onOpen: (box) => {
        const kind = box.querySelector("#rc-link-kind"), id = box.querySelector("#rc-link-id");
        const fill = () => {
          const found = opts.find(([k]) => k === kind.value);
          id.innerHTML = (found ? found[2] : []).map(([v, l]) =>
            `<option value="${esc(v)}">${esc(String(l).slice(0, 70))}</option>`).join("");
        };
        kind.addEventListener("change", fill); fill();
      },
      onOk: async (v) => {
        if (!v.id) { toast("先选一个目标", "warn"); return false; }
        try {
          await post("/api/reading/links", {
            src_kind: srcKind, src_id: String(srcId), dst_kind: v.kind,
            dst_id: v.id, relation: v.relation, clip_id: cid,
          });
          const fresh = await api(`/api/reading/book/${cid}`);
          S.book.links = fresh.links; S.book.backlinks = fresh.backlinks;
          S.rightTab = "links"; renderRight(); toast("双向引用已建立");
          return true;
        } catch (e) { toast(e.message, "err"); return false; }
      },
    });
  }

  function aiResultHtml(r) {
    const c = r.context || {};
    const scope = c.scope === "book_outline"
      ? `逐章节选 · 覆盖 ${c.covered_chapters || 0}/${c.total_chapters || 0} 章`
      : (c.scope === "book" ? "全书检索片段" : (c.scope === "note" ? "仅本笔记" : "当前章节"));
    const trunc = c.truncated ? ` · 节选（发送 ${c.sent_chars || 0} / 原文 ${c.chapter_chars || 0} 字）` : "";
    const usage = r.usage?.total_tokens ? ` · ${r.usage.total_tokens} tokens` : "";
    const cost = `${r.latency_ms || 0}ms${usage}${r.cached ? " · 已复用缓存，未调用模型" : ""}`;
    return `<div class="rc-ai-meta">${esc(r.channel || "AI")} · ${esc(r.model || "")} · ${esc(scope)}${esc(trunc)}<br>${esc(cost)} · AI 生成，请核对原文</div>
      <div class="rc-md">${markdownHtml(r.text || "")}</div>
      ${(r.sources || []).length ? `<details class="rc-ai-sources"><summary>原文依据 ${r.sources.length} 段</summary>
        ${r.sources.map((s) => `<button type="button" class="rc-ai-source" data-ai-source="${esc(s.id)}"><strong>${esc(s.id)} · ${esc(s.chapter_title || "当前章节")}</strong><span>${esc((s.quote || "").slice(0, 240))}</span></button>`).join("")}</details>` : ""}
      <div class="rc-ai-acts2"><button type="button" class="btn small ghost" id="rc-ai-save">存为笔记</button></div>`;
  }

  function bindAIResult(r) {
    $$("[data-ai-source]").forEach((el) => el.addEventListener("click", () => {
      const source = (r.sources || []).find((s) => s.id === el.dataset.aiSource);
      if (source) {
        rememberAI();
        jumpTo({ chapter_id: source.chapter_id, offset: source.offset, kind: "chapter" });
      }
    }));
    $("#rc-ai-save")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (button.disabled) return;
      button.disabled = true;
      try {
        const response = await post("/api/reading/notes", {
          clip_id: Number(S.book.item.clip_id),
          title: `AI · ${AI_ACTIONS.find(([k]) => k === r.action)?.[1] || "伴读"} · ${currentChapter()?.title || ""}`,
          content_md: r.text, kind: "ai", chapter_id: S.chapterId, tags: "AI伴读",
        });
        S.book.notes.unshift(response.note);
        toast("已存为笔记");
      } catch (e) { toast(e.message, "err"); button.disabled = false; }
    });
  }

  async function runAI(action, question, selection = "", contentMd = "") {
    if (!S.book || S.aiBusy) return null;
    if (action === "outline") {
      const count = Math.min(24, S.book.chapters.length);
      const ok = await glConfirm(`全书提纲将逐章读取节选并调用模型（最多 ${count} 章），再合并一次。仅本次手动触发，是否继续？`, { confirmText: "生成全书提纲" });
      if (!ok) return null;
    }
    const state = currentAI();
    const request = ++S.aiRequest;
    const context = { cid: Number(S.book.item.clip_id), chapter: S.chapterId,
      scope: $("#rc-ai-scope")?.value || state.scope || "chapter", selection: selection || S.selection?.quote || "" };
    const controller = new AbortController();
    S.aiController = controller;
    S.aiBusy = true;
    S.rightTab = "ai";
    renderRight();
    try {
      const r = await api("/api/ai/reading", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ clip_id: context.cid, action, chapter_id: context.chapter,
          scope: context.scope, question,
          selection: context.selection, content_md: contentMd, history: state.history || [] }),
      });
      if (request !== S.aiRequest || context.cid !== Number(S.book?.item?.clip_id) || context.chapter !== S.chapterId) return null;
      state.answer = r;
      if (action === "chat") state.history = [...(state.history || []), { question, answer: r.text }].slice(-6);
      S.aiStates[aiStateKey(context.cid, context.chapter)] = state;
      renderRight();
      bindAIResult(r);
      return r.text;
    } catch (e) {
      if (e.name !== "AbortError" && request === S.aiRequest && context.chapter === S.chapterId) {
        const out = $("#rc-ai-out");
        if (out) out.innerHTML = `<div class="rc-ai-error">${esc(e.message)}</div>`;
        else toast(e.message, "err");
      }
      return null;
    } finally {
      if (request === S.aiRequest) {
        S.aiBusy = false;
        S.aiController = null;
        if (S.rightTab === "ai") {
          renderRight();
        }
      }
    }
  }

  /* 左右两栏的开合：宽屏收成一列宽，窄屏收成抽屉 */
  function applySide(side) {
    const r = $("#rc-reader");
    if (!r) return;
    const open = side === "left" ? P.leftOpen : P.rightOpen;
    r.classList.toggle(side === "left" ? "no-left" : "no-right", !open);
    const panel = $(side === "left" ? "#rc-left" : "#rc-right");
    const shown = narrow() ? r.classList.contains("show-" + side) : open;
    if (panel) {
      panel.inert = !shown;
      panel.setAttribute("aria-hidden", shown ? "false" : "true");
    }
    const btn = $(side === "left" ? "#rc-toggle-left" : "#rc-toggle-right");
    if (btn) btn.setAttribute("aria-expanded", shown ? "true" : "false");
    const scrim = $("#rc-scrim");
    if (scrim) scrim.hidden = !(narrow() &&
      (r.classList.contains("show-left") || r.classList.contains("show-right")));
  }

  function applySides() { applySide("left"); applySide("right"); }

  /* 折列会改正文栏宽、整篇重排；收放前后把「视线所在的那一段」钉住，
     免得读者一收目录就丢了刚才读到哪。 */
  function bookAnchor() {
    const center = $("#rc-center");
    if (!center) return null;
    const top = center.scrollTop;
    for (const p of center.querySelectorAll("#rc-article [data-a-start]")) {
      const rel = topIn(center, p);
      if (rel + p.offsetHeight > top + 2) {
        return { p, delta: rel - top };
      }
    }
    return null;
  }

  function restoreAnchor(anchor) {
    const center = $("#rc-center");
    if (!anchor || !center || !anchor.p.isConnected) return;
    const prev = center.style.scrollBehavior;
    center.style.scrollBehavior = "auto";
    center.scrollTop = Math.max(0, topIn(center, anchor.p) - anchor.delta);
    center.style.scrollBehavior = prev;
  }

  let sideFixTimer = 0;

  function setSide(side, open) {
    const r = $("#rc-reader");
    if (!r) return;
    if (narrow()) {
      r.classList.toggle("show-" + side, !!open);
      if (open) r.classList.remove("show-" + (side === "left" ? "right" : "left"));
    } else {
      const anchor = bookAnchor();
      if (side === "left") P.leftOpen = !!open; else P.rightOpen = !!open;
      savePrefs();
      applySides();
      // 连点两下时只留最后一次落位，别让两个计时器互相拽
      clearTimeout(sideFixTimer);
      if (anchor) sideFixTimer = setTimeout(() => restoreAnchor(anchor), 300);   // 等列宽过渡走完
      return;
    }
    applySides();
  }

  function toggleSide(side) {
    const r = $("#rc-reader");
    if (!r) return;
    const open = narrow()
      ? r.classList.contains("show-" + side)
      : (side === "left" ? P.leftOpen : P.rightOpen);
    setSide(side, !open);
  }

  function bindBook(d) {
    const cid = Number(d.item.clip_id);
    $("#rc-back")?.addEventListener("click", () => { location.hash = "#/reading"; });
    $("#rc-toggle-left")?.addEventListener("click", () => toggleSide("left"));
    $("#rc-toggle-right")?.addEventListener("click", () => toggleSide("right"));
    $("#rc-rail-left")?.addEventListener("click", () => setSide("left", true));
    $("#rc-rail-right")?.addEventListener("click", () => setSide("right", true));
    $("#rc-scrim")?.addEventListener("click", () => { setSide("left", false); setSide("right", false); });
    applySides();
    // 跨过 900px 断点时，抽屉态与折列态互相归位，别把上一档的状态带过去
    const mq = window.matchMedia("(max-width: 900px)");
    listen(mq, "change", () => {
      $("#rc-reader")?.classList.remove("show-left", "show-right");
      applySides();
    });
    /* 纸面容器和工具栏按钮曾经共用 #rc-paper：$ 就是 querySelector，
       只拿到 DOM 里靠前的那个按钮，于是字号/行距/宽度/纸张全被写到按钮上，
       正文纸面纹丝不动 —— A− / A＋ / ◐ 点了像没点。两个 id 分开，各归各位。 */
    const PAPER_NAME = { paper: "白纸", sepia: "牛皮纸", night: "夜间纸" };
    const applyType = () => {
      const paper = $("#rc-paper-surface");
      if (paper) {
        paper.style.setProperty("--rc-fs", P.fontSize + "px");
        paper.style.setProperty("--rc-lh", P.lineHeight);
        paper.style.setProperty("--rc-w", P.width + "px");
        paper.dataset.paper = P.paper;
      }
      const minus = $("#rc-font-minus"), plus = $("#rc-font-plus"), paperBtn = $("#rc-paper-btn");
      if (minus) {
        minus.title = `缩小字号（当前 ${P.fontSize}px）`;
        minus.setAttribute("aria-label", minus.title);
      }
      if (plus) {
        plus.title = `放大字号（当前 ${P.fontSize}px）`;
        plus.setAttribute("aria-label", plus.title);
      }
      if (paperBtn) {
        paperBtn.title = `切换纸张（当前：${PAPER_NAME[P.paper] || P.paper}）`;
        paperBtn.setAttribute("aria-label", paperBtn.title);
      }
      savePrefs();
    };
    /* 工具栏反馈只是即时状态，1600ms 就收，别占着右下角 */
    $("#rc-font-plus")?.addEventListener("click", () => {
      if (P.fontSize >= 26) { toast("已是最大字号 26px", "ok", "", 1600); return; }
      P.fontSize += 1; applyType();
    });
    $("#rc-font-minus")?.addEventListener("click", () => {
      if (P.fontSize <= 13) { toast("已是最小字号 13px", "ok", "", 1600); return; }
      P.fontSize -= 1; applyType();
    });
    $("#rc-paper-btn")?.addEventListener("click", () => {
      const order = ["paper", "sepia", "night"];
      P.paper = order[(order.indexOf(P.paper) + 1) % order.length];
      P.lineHeight = P.lineHeight > 2.1 ? 1.8 : +(P.lineHeight + 0.1).toFixed(1);
      P.width = P.width >= 880 ? 680 : P.width + 100;
      applyType();
      toast("纸张 · " + (PAPER_NAME[P.paper] || P.paper), "ok", "", 1600);
    });
    applyType();   // 首次进入：把偏好（含 localStorage 恢复值）同步到纸面与按钮
    $("#rc-export")?.addEventListener("click", async () => {
      try {
        const r = await api(`/api/reading/export/${cid}`);
        const blob = new Blob([r.markdown], { type: "text/markdown;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = r.filename || "阅读笔记.md";
        a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);
        toast("已导出 Markdown");
      } catch (e) { toast(e.message, "err"); }
    });
    $("#rc-transcript")?.addEventListener("click", () => openTranscript());
    const article = $("#rc-article");
    if (article) {
      listen(article, "mouseup", onSelection);
      listen(article, "touchend", onSelection);
      // 手机/平板上拖选择柄时不会在正文上触发 touchend，靠 selectionchange 兜底
      let selTimer = 0;
      listen(document, "selectionchange", () => {
        clearTimeout(selTimer);
        selTimer = setTimeout(() => {
          const sel = window.getSelection();
          if (!sel || sel.isCollapsed) return;
          const node = sel.anchorNode;
          if (node && article.contains(node.nodeType === 3 ? node.parentNode : node)) onSelection();
        }, 180);
      });
      listen($("#rc-center"), "scroll", () => { if (S.selection) hideSelbar(); });
      listen(article, "click", (e) => {
        const mark = e.target.closest?.("mark[data-ann-id]");
        if (!mark) return;
        const a = d.annotations.find((x) => String(x.id) === mark.dataset.annId);
        if (a) editAnnotation(a);
      });
    }
    listen(document, "mousedown", (e) => {
      // 点在弹窗 / 工具条内不算「落到正文外」：批注弹窗点「保存」时不能先把选区清掉
      if (e.target.closest?.(".gl-modal-backdrop, .rc-modal-backdrop, .pal-backdrop, #rc-selbar")) return;
      const bar = $("#rc-selbar");
      if (bar && !bar.hidden && !bar.contains(e.target) &&
        !e.target.closest?.("[data-a-start]")) hideSelbar();
    });
    listen(document, "keydown", (e) => {
      if (e.key === "Escape") {
        const r = $("#rc-reader");
        if (r && narrow() && (r.classList.contains("show-left") || r.classList.contains("show-right"))) {
          setSide("left", false); setSide("right", false);
        }
        hideSelbar();
      }
      if ($(".gl-modal-backdrop, .pal-backdrop")) return;
      if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (e.key === "[") { e.preventDefault(); toggleSide("left"); }
      if (e.key === "]") { e.preventDefault(); toggleSide("right"); }
      if (e.key === "ArrowRight") switchChapter(1);
      if (e.key === "ArrowLeft") switchChapter(-1);
    });
  }

  /* ── 全文检索 ─────────────────────────────────────── */

  async function viewSearch() {
    const qs = new URLSearchParams({
      q: S.searchQ, scope: S.scope, limit: "120",
    });
    if (S.searchShelf) qs.set("shelf", S.searchShelf);
    const d = await api("/api/reading/search?" + qs.toString());
    const scopes = [["all", "全部"], ...Object.entries(SCOPE_LABEL)];
    mount(head("全文检索", "正文、批注、笔记、书签一次搜完；每条都能跳回原位置", `
        <a class="btn small ghost" href="#/reading">← 书架</a>`)
      + `<div class="rc-searchbar">
          <label class="rc-bigsearch">${icon("search")}
            <input id="rc-sq" type="search" placeholder="搜正文里的一句话、一条批注、一段笔记…" value="${esc(S.searchQ)}"></label>
          <button type="button" class="btn small" id="rc-sgo">检索</button>
        </div>
        <div class="rc-chips rc-scope">${scopes.map(([v, l]) =>
        `<button type="button" class="chip ${S.scope === v ? "on" : ""}" data-sc="${esc(v)}">${esc(l)}</button>`).join("")}</div>
        <div class="rc-search-meta">${S.searchQ
        ? `命中 ${d.total} 条 · 引擎 ${esc(d.engine || "")}` : "输入关键词开始；中文 3 字以上走 FTS5 三元组索引"}</div>
        <div class="rc-results">${d.results.length ? d.results.map(resultCard).join("")
        : (S.searchQ ? empty("没有命中", "换个词，或把范围切到「全部」") : empty("还没开始搜", "批注和笔记里的字也一样能搜到"))}</div>`);
    const input = $("#rc-sq");
    const go = () => { S.searchQ = input.value.trim(); G.routeNow(); };
    $("#rc-sgo")?.addEventListener("click", go);
    input?.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    $$("[data-sc]").forEach((el) => el.addEventListener("click", () => {
      S.scope = el.dataset.sc; G.routeNow();
    }));
    $$("[data-hit]").forEach((el) => el.addEventListener("click", () => {
      const r = d.results[Number(el.dataset.hit)];
      if (!r) return;
      goBook(r.clip_id, { chapter_id: r.chapter_id, offset: r.offset, ref_id: r.ref_id, kind: r.kind });
    }));
  }

  function resultCard(r, i) {
    return `<article class="rc-result" data-hit="${i}">
      <div class="rc-result-top"><span class="rc-tag k-${esc(r.kind)}">${SCOPE_LABEL[r.kind] || r.kind}</span>
        <strong>${esc(r.book_title)}</strong><span class="rc-faint">${esc(r.chapter_title || "")}</span></div>
      <div class="rc-result-snip">${esc(r.snippet)}</div>
      <div class="rc-result-foot">跳转 · ${esc(r.chapter_title || "正文")} @ ${Math.round(r.offset)}</div>
    </article>`;
  }

  /* ── 笔记沉淀 ─────────────────────────────────────── */

  async function viewNotes() {
    const qs = new URLSearchParams({ q: S.notesQ, kind: S.notesKind });
    const d = await api("/api/reading/notes?" + qs.toString());
    mount(head("笔记沉淀", "摘录、批注转写、AI 整理都汇在这里，可再归档进知识库", `
        <a class="btn small ghost" href="#/reading">← 书架</a>`)
      + `<div class="rc-toolbar">
          <label class="rc-quick">${icon("search")}<input id="rc-nq" type="search" placeholder="搜笔记" value="${esc(S.notesQ)}"></label>
          <div class="rc-chips">${[["", "全部"], ...Object.entries(NOTE_KIND_LABEL)].map(([v, l]) =>
        `<button type="button" class="chip ${S.notesKind === v ? "on" : ""}" data-nk="${esc(v)}">${esc(l)}</button>`).join("")}</div>
        </div>
        <div class="rc-note-grid">${d.notes.length ? d.notes.map((n) => `
          <article class="rc-note" data-gn="${Number(n.id)}">
            <div class="rc-note-head"><strong>${esc(n.title || "无标题")}</strong>
              <span class="rc-tag">${NOTE_KIND_LABEL[n.kind] || n.kind}</span></div>
            <div class="rc-faint">${esc(n.book_title || "")} · ${esc(fmtDate(n.created_at))}</div>
            ${n.source_quote ? `<blockquote>${esc(n.source_quote.slice(0, 160))}</blockquote>` : ""}
            <div class="rc-md">${markdownHtml((n.content_md || "").slice(0, 700))}</div>
            <div class="rc-note-foot">
              <button type="button" data-g-open="${Number(n.clip_id)}">打开原书</button>
              ${n.archived_clip_id ? `<a href="#/c/${Number(n.archived_clip_id)}">知识卡 ↗</a>`
        : `<button type="button" data-g-archive="${Number(n.id)}" data-g-cid="${Number(n.clip_id)}">汇入知识库</button>`}
            </div></article>`).join("")
        : empty("还没有笔记", "阅读时选中文字点「摘录成笔记」，或在右侧栏新建")}</div>`);
    const q = $("#rc-nq");
    let t = 0;
    listen(q, "input", () => {
      clearTimeout(t); t = setTimeout(() => { S.notesQ = q.value.trim(); G.routeNow(); }, 320);
    });
    $$("[data-nk]").forEach((el) => el.addEventListener("click", () => {
      S.notesKind = el.dataset.nk; G.routeNow();
    }));
    $$("[data-g-open]").forEach((el) => el.addEventListener("click", () => goBook(Number(el.dataset.gOpen))));
    $$("[data-g-archive]").forEach((el) => el.addEventListener("click", async () => {
      const cid = Number(el.dataset.gCid), nid = Number(el.dataset.gArchive);
      try {
        await post("/api/reading/archive", { clip_id: cid, source_kind: "note", source_id: nid });
        toast("已汇入知识库"); G.routeNow();
      } catch (e) { toast(e.message, "err"); }
    }));
  }

  /* ── 知识库归档 ───────────────────────────────────── */

  async function viewArchive() {
    const d = await api("/api/reading/links");
    const rows = (d.links || []).slice(0, 200);
    mount(head("知识库归档", "批注 / 摘录 / 笔记 → 知识卡 → 可检索、可回溯的知识网络", `
        <a class="btn small ghost" href="#/reading">← 书架</a>`)
      + `<div class="rc-flow">
          ${["① 阅读时高亮、批注、摘录", "② 笔记沉淀成 Markdown", "③ 一键汇入收存生成知识卡", "④ 正反双向引用自动建立"]
        .map((s) => `<div class="rc-flow-step">${esc(s)}</div>`).join("")}
        </div>
        <div class="rc-sec-title">关系网 <span class="rc-faint">共 ${rows.length} 条边</span></div>
        <div class="rc-links">${rows.length ? rows.map((l) => `<div class="rc-link-row">
            <span class="rc-link-kind">${esc(l.src_kind)}</span>
            <span class="rc-link-title">${esc(l.src_title || l.src_id)}</span>
            <span class="rc-link-arrow">→</span>
            <span class="rc-link-kind">${esc(l.dst_kind)}</span>
            <span class="rc-link-title">${esc(l.dst_title || l.dst_id)}</span>
            <span class="rc-tag">${esc(l.relation)}</span>
            ${l.dst_kind === "clip" ? `<a class="rc-link-go" href="#/c/${esc(l.dst_id)}">打开 ↗</a>` : ""}
          </div>`).join("") : empty("关系网还是空的", "在右侧栏对批注或笔记点「归档 / 引用」即可织网")}</div>`);
  }

  /* ── 阅读统计 ─────────────────────────────────────── */

  async function viewStats() {
    const d = await api("/api/reading/stats");
    const days = d.days || [];
    const map = new Map(days.map((x) => [x.day, x]));
    const today = new Date();
    const cells = [];
    for (let i = 118; i >= 0; i--) {
      const dt = new Date(today.getTime() - i * 86400000);
      const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      const v = map.get(key);
      const sec = v?.seconds || 0;
      const lv = sec === 0 ? 0 : sec < 300 ? 1 : sec < 1200 ? 2 : sec < 3600 ? 3 : 4;
      cells.push(`<span class="rc-heat l${lv}" title="${key} · ${fmtDuration(sec)}"></span>`);
    }
    let streak = 0;
    for (let i = 0; i < 120; i++) {
      const dt = new Date(today.getTime() - i * 86400000);
      const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      if ((map.get(key)?.seconds || 0) > 0) streak++;
      else if (i > 0) break;
    }
    mount(head("阅读统计", "只记本机真实阅读时长与进度，不上传", `
        <a class="btn small ghost" href="#/reading">← 书架</a>`)
      + `<div class="rc-stats">
          <div class="rc-stat"><strong>${streak}</strong><span>连续天数</span></div>
          <div class="rc-stat"><strong>${fmtDuration(days.reduce((a, b) => a + (b.seconds || 0), 0))}</strong><span>累计时长</span></div>
          <div class="rc-stat"><strong>${days.reduce((a, b) => a + (b.chars || 0), 0).toLocaleString()}</strong><span>累计字数</span></div>
          <div class="rc-stat"><strong>${(d.by_kind || []).reduce((a, b) => a + b.n, 0)}</strong><span>资料总数</span></div>
        </div>
        <div class="rc-sec-title">最近 17 周</div>
        <div class="rc-heatmap">${cells.join("")}</div>
        <div class="rc-two">
          <div><div class="rc-sec-title">按类型</div>${(d.by_kind || []).map((x) =>
        `<div class="rc-kv"><span>${KIND_LABEL[x.kind] || x.kind}</span><strong>${x.n}</strong></div>`).join("") || `<div class="rc-faint">暂无</div>`}</div>
          <div><div class="rc-sec-title">按状态</div>${(d.by_status || []).map((x) =>
        `<div class="rc-kv"><span>${STATUS_LABEL[x.status] || x.status}</span><strong>${x.n}</strong></div>`).join("") || `<div class="rc-faint">暂无</div>`}</div>
        </div>
        <div class="rc-sec-title">读得最久</div>
        <div class="rc-top">${(d.top || []).map((x) => `<button type="button" data-open="${Number(x.clip_id)}">
          <span>${esc(x.title)}</span><strong>${fmtDuration(x.read_seconds)}</strong></button>`).join("") || `<div class="rc-faint">暂无</div>`}</div>`);
    $$("[data-open]").forEach((el) => el.addEventListener("click", () => goBook(Number(el.dataset.open))));
  }

  /* ── 注册路由与全局绑定 ───────────────────────────── */

  G.addRoute(/^\/reading$/, viewShelf);
  G.addRoute(/^\/reading\/shelf\/(all|none|\d+)$/, viewShelf);
  G.addRoute(/^\/reading\/book\/(\d+)$/, (m) => viewBook(m));
  G.addRoute(/^\/reading\/notes$/, viewNotes);
  G.addRoute(/^\/reading\/search$/, viewSearch);
  G.addRoute(/^\/reading\/archive$/, viewArchive);
  G.addRoute(/^\/reading\/stats$/, viewStats);

  /* 收存详情页的反向引用：从知识卡一侧也能回到阅读中心的批注 / 笔记 / 摘录。
     由 app.js 的 route() 在 #/c/{id} 渲染完成后调用，不侵入原详情页。 */
  async function decorateClip(cid) {
    try {
      const d = await api(`/api/reading/links?dst_kind=clip&dst_id=${encodeURIComponent(cid)}`);
      const links = (d.links || []).filter((x) => x.clip_id);
      if (!links.length) return;
      const box = document.createElement("section");
      box.className = "rc-backlink view-enter";
      box.innerHTML = `<div class="rc-sec-title">阅读中心 · 回溯到原文
          <span class="rc-faint">${links.length} 条引用</span></div>
        <div class="rc-hint">这些批注、摘录与笔记引用了这张卡片，点「打开原书」可回到当时的阅读位置。</div>
        ${links.map((l, i) => `<div class="rc-link-row">
            <span class="rc-link-kind">${esc(l.src_kind)}</span>
            <span class="rc-link-title">${esc(l.src_title || l.src_id)}</span>
            <span class="rc-tag">${esc(l.relation)}</span>
            <button type="button" class="btn small ghost" data-bl="${i}">打开原书 ↗</button>
          </div>`).join("")}`;
      $("#main")?.appendChild(box);
      $$("[data-bl]", box).forEach((el) => el.addEventListener("click", () => {
        const l = links[Number(el.dataset.bl)];
        if (l) goBook(Number(l.clip_id), { kind: l.src_kind, ref_id: l.src_id });
      }));
    } catch (_) { /* 反向引用是加分项，取不到不影响详情页 */ }
  }

  window.GLReading = { onRoute, syncNav, decorateClip, draftOpen };

  $$("#sect-switch .sect-btn").forEach((btn) => btn.addEventListener("click", () => {
    const sect = btn.dataset.sect;
    location.hash = sect === "reading" ? "#/reading"
      : (sect === "bible" ? "#/bible" : (sect === "events" ? "#/events" : "#/"));
  }));
  $("#reading-shelf-add")?.addEventListener("click", (e) => {
    e.preventDefault();
    newShelf();
  });

  // 首屏就落在阅读中心时，app.js 的 route() 已经在 setTimeout 里等这些路由了。
})();
