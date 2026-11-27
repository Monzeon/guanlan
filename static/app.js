/* 观澜 GUANLAN · 前端（精简版）
   三个页面：收存 · 问 · 设置。无框架、无构建、hash 路由、手写 SVG。
   原则只有一条：东西收进来，AI 只读你已经收下的东西。 */

(() => {
  "use strict";

  // 同一个客户端发出的写请求会带这个标记。服务端把它原样放进 SSE，
  // 用来跳过“自己刚写完，又被自己的回声刷新一遍”。
  const clientId = (() => {
    try {
      const saved = sessionStorage.getItem("gl_client_id");
      if (saved) return saved;
      const made = (crypto.randomUUID && crypto.randomUUID())
        || `gl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      sessionStorage.setItem("gl_client_id", made);
      return made;
    } catch (_) {
      return `gl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    }
  })();

  /* ── 主题：第一件事就是套深浅色，别让页面闪一下 ──
     CSP 是 script-src 'self'，不能放内联脚本；本文件在 body 末尾，
     DOM 已就绪，这里套上和 CSS 首帧绘制之间没有可感知的间隔。 */
  const currentThemePref = () => {
    try {
      const t = localStorage.getItem("gl_theme");
      if (t === "dark" || t === "light") return t;
    } catch (_) { /* 隐私模式等拿不到就算了 */ }
    return (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches)
      ? "dark" : "light";
  };
  if (currentThemePref() === "dark") document.documentElement.dataset.theme = "dark";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const main = $("#main");

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const api = async (p, opts = {}) => {
    const slow = p.startsWith("/api/ai/") || p === "/api/ask" || p === "/api/clips/file"
      || p.endsWith("/extract") || p === "/api/insights/run";
    const ctrl = new AbortController();
    const timeout = opts.timeout || (slow ? 250000 : 45000);
    const fetchOpts = {
      ...opts,
      headers: { ...(opts.headers || {}), "X-GL-Client": clientId },
    };
    delete fetchOpts.timeout;
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const r = await fetch(p, { ...fetchOpts, signal: opts.signal || ctrl.signal });
      const text = await r.text();
      let j;
      try { j = text ? JSON.parse(text) : {}; }
      catch (_) { throw new Error(`服务返回了非 JSON 响应（HTTP ${r.status}）`); }
      if (j && j.error) throw new Error(j.ref ? `${j.error}（ref: ${j.ref}）` : j.error);
      if (!r.ok) throw new Error(`请求失败（HTTP ${r.status}）`);
      return j;
    } catch (e) {
      if (e.name === "AbortError") {
        throw new Error(`等太久了（${Math.round(timeout / 1000)}s）。已存的东西不会丢，可以重试。`);
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  };

  const post = (p, body) => api(p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

  async function withBusy(button, work) {
    if (!button || button.disabled) return;
    const old = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { return await work(); }
    finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      if (button.isConnected) button.textContent = old;
    }
  }

  /* 提示条：一句一条，贴着右下角。
     连点同一个动作时不叠罗汉——800ms 内同类型的那条直接换字，
     否则连点三下「权威/参考/资讯」会摞成三块黑板。
     每条自带销毁定时器：早先全模块共用一个 timer，后一条 toast 把前一条的
     clearTimeout 掉，前一条就永远挂在右下角，而宿主是 pointer-events:none，点也点不掉。 */
  const toastState = { el: null, at: 0, type: "" };
  const toastTimers = new WeakMap();

  function dismissToast(item) {
    if (!item || !item.isConnected) return;
    clearTimeout(toastTimers.get(item));
    toastTimers.delete(item);
    if (item.classList.contains("out")) return;
    item.classList.add("out");
    setTimeout(() => item.remove(), 190);
  }

  function armToast(item, ms) {
    clearTimeout(toastTimers.get(item));
    toastTimers.set(item, setTimeout(() => dismissToast(item), ms));
  }

  function toast(message, type = "ok", tone = "", ms = 4200) {
    let host = $("#toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    const now = Date.now();
    const cls = `toast ${type}${tone ? " tone-" + tone : ""}`;
    const reuse = toastState.el && toastState.el.isConnected
      && !toastState.el.classList.contains("out")
      && now - toastState.at < 800 && toastState.type === type;

    let item;
    if (reuse) {
      item = toastState.el;
      item.className = cls;
      item.querySelector(".toast-msg").textContent = String(message ?? "");
      // 让动画重放一遍：先摘掉再放回去
      item.style.animation = "none";
      void item.offsetWidth;
      item.style.animation = "";
    } else {
      item = document.createElement("div");
      item.className = cls;
      item.title = "点击关闭";
      const dot = document.createElement("span");
      dot.className = "toast-dot";
      const msg = document.createElement("span");
      msg.className = "toast-msg";
      msg.textContent = String(message ?? "");
      item.append(dot, msg);
      item.addEventListener("click", () => dismissToast(item));
      host.appendChild(item);
    }

    // 最多三条，最老的先走
    const live = $$(".toast", host).filter((el) => !el.classList.contains("out"));
    while (live.length > 3) dismissToast(live.shift());

    // 定时器归这条自己，后续提示不再顺手清掉别人的
    armToast(item, ms);
    toastState.el = item;
    toastState.at = now;
    toastState.type = type;
  }

  function downloadJSON(data, name) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const today = () => new Date().toISOString().slice(0, 10);

  /* 信源分级：跟着旧版的正典层级走——T3/T4 只当线索，不单独作证据 */
  const TIERS = [
    ["T0", "一手数据", "原始文件、统计公报、法规原文、当事人原话"],
    ["T1", "机构共识", "官方口径、权威机构报告、大媒体调查"],
    ["T2", "专业分析", "专业媒体、研究者的分析"],
    ["T3", "自媒体观点", "个人评论、公众号、视频"],
    ["T4", "传闻未证", "道听途说、截图、二手转述"],
  ];
  const TIER_NAME = Object.fromEntries(TIERS.map(([k, n]) => [k, n]));
  const tierBadge = (t) => t
    ? `<span class="badge ${esc(t.toLowerCase())}" title="${esc(TIER_NAME[t] || t)}">${esc(t)}</span>` : "";
  const tierOptions = (selected, blank = "未分级") => `<option value="">${blank}</option>`
    + TIERS.map(([k, n]) => `<option value="${k}" ${selected === k ? "selected" : ""}>${k} ${n}</option>`).join("");

  /* 材料级：和信源分级是两条正交的轴。
     tier 说「谁说的、能信到什么程度」；grade 说「属于库里的哪一层、能不能当依据」。 */
  const GRADES = [
    ["canon", "权威", "法规、公报、报告、论文：可以当依据"],
    ["ref", "参考", "深度报道、专业分析：可引用，算分析"],
    ["daily", "资讯", "每日新闻、快讯、推送：只作时效背景"],
  ];
  const GRADE_NAME = Object.fromEntries(GRADES.map(([k, n]) => [k, n]));
  const gradeName = (g) => GRADE_NAME[g] || "未定";
  const gradeBadge = (g) => (g && GRADE_NAME[g]
    ? `<span class="g-badge g-${esc(g)}" title="${esc(GRADES.find((x) => x[0] === g)[2])}">${esc(GRADE_NAME[g])}</span>`
    : `<span class="g-badge g-unset" title="还没判：AI 会引用，但会标明未定">未定</span>`);
  // 分段控件：data-grade 的值，'' 表示未定
  const gradeSegs = (current, opts = {}) => {
    const all = [["", "未定"], ...GRADES.map(([k, n]) => [k, n])];
    return `<span class="g-segs${opts.small ? " small" : ""}" ${opts.id ? `id="${opts.id}"` : ""}>`
      + all.map(([k, n]) => `<span class="g-seg ${k ? `g-${k}` : "g-unset"} ${(current || "") === k ? "on" : ""}"
          data-grade-pick="${k}" title="${esc(k ? GRADES.find((x) => x[0] === k)[2] : "还没判：AI 仍会引用，只是标明未定")}">${n}</span>`).join("")
      + `</span>`;
  };

  const fmtDate = (s) => {
    if (!s) return "";
    const d = String(s).slice(0, 16).replace("T", " ");
    const now = new Date();
    const same = d.slice(0, 7) === `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    // 同月也带上月份：光秃秃的「02 21:37」分不清是几号还是几月
    return same ? d.slice(5) : d.slice(0, 10);
  };

  const fmtSize = (bytes) => {
    if (!bytes) return "0 B";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / 1048576).toFixed(1)} MB`;
  };

  /* ── 像笔记软件一样收图 ────────────────────────────
     一条收存可以带多张图：收存框里先看到缩略图，收下后卡片和详情页直接显示。
     只放行浏览器能安全内联展示的位图；SVG 不进白名单。 */
  const CAP_IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp"]);
  const CAP_IMAGE_MIME = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
    gif: "image/gif", webp: "image/webp",
  };
  const CAP_IMAGE_EXT_BY_MIME = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
    "image/gif": "gif", "image/webp": "webp",
  };
  const MAX_CAP_IMAGES = 12;
  const MAX_CAP_IMAGE_BYTES = 15 * 1024 * 1024;
  let capImageSeq = 0;
  let capText = "";
  let capImages = [];   // {id, file, url}；url 是收下前的本地预览
  let capAutoExpand = null;   // 收存框折叠时，贴图后自动展开，避免缩略图被藏住

  const capImageExt = (name) => {
    const m = String(name || "").match(/\.([a-z0-9]+)$/i);
    return m ? m[1].toLowerCase() : "";
  };

  function normalizeCapImage(file) {
    if (!file) return null;
    const mime = String(file.type || "").toLowerCase().split(";")[0].trim();
    const fromName = capImageExt(file.name);
    const ext = CAP_IMAGE_EXTS.has(fromName) ? fromName : (CAP_IMAGE_EXT_BY_MIME[mime] || "");
    if (!ext) return null;
    const type = CAP_IMAGE_MIME[ext];
    if (fromName === ext && mime === type) return file;
    const base = String(file.name || "图片").replace(/\.[^./\\]+$/, "").slice(0, 80) || "图片";
    if (typeof File === "undefined") return file;
    return new File([file], `${base}.${ext}`, {
      type, lastModified: file.lastModified || Date.now(),
    });
  }

  const isSupportedImageFile = (file) => !!normalizeCapImage(file);

  function makePreviewUrl(file) {
    return new Promise((resolve) => {
      if (window.URL && URL.createObjectURL) {
        resolve(URL.createObjectURL(file));
        return;
      }
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => resolve("");
      reader.readAsDataURL(file);
    });
  }

  function releasePreviewUrl(url) {
    if (url && url.startsWith("blob:") && window.URL && URL.revokeObjectURL) {
      URL.revokeObjectURL(url);
    }
  }

  function clearCapImages(render = true) {
    capImages.forEach((it) => releasePreviewUrl(it.url));
    capImages = [];
    if (render) renderCapImages();
  }

  function renderCapImages() {
    const host = $("#cap-images");
    if (!host) return;
    if (!capImages.length) {
      host.innerHTML = "";
      host.classList.remove("on");
      return;
    }
    host.classList.add("on");
    host.innerHTML = `<div class="cap-images-head">
        <span>待收图片 ${capImages.length}/${MAX_CAP_IMAGES}</span>
        <span class="cap-images-tip">Ctrl+V 贴截图，或继续拖图片进来</span>
      </div><div class="cap-images-grid">${capImages.map((it) => `
        <div class="cap-image">
          <button type="button" class="cap-image-open" data-cap-image-open="${esc(it.id)}"
            title="点开看大图" aria-label="查看 ${esc(it.file.name)}">
            <img src="${esc(it.url)}" alt="" draggable="false">
          </button>
          <span class="cap-image-name" title="${esc(it.file.name)}">${esc(it.file.name)}</span>
          <span class="cap-image-size">${esc(fmtSize(it.file.size))}</span>
          <button type="button" class="cap-image-x" data-cap-image-drop="${esc(it.id)}"
            title="移掉这张">×</button>
        </div>`).join("")}</div>`;
    $$("[data-cap-image-drop]", host).forEach((el) => el.addEventListener("click", () => {
      const item = capImages.find((x) => x.id === el.dataset.capImageDrop);
      if (item) releasePreviewUrl(item.url);
      capImages = capImages.filter((x) => x.id !== el.dataset.capImageDrop);
      renderCapImages();
    }));
    $$("[data-cap-image-open]", host).forEach((el) => el.addEventListener("click", (e) => {
      e.preventDefault();
      const item = capImages.find((x) => x.id === el.dataset.capImageOpen);
      if (!item) return;
      openNoteFilePreview({
        file: "", name: item.file.name, size: item.file.size,
        src: item.url, download: item.url, forceImage: true,
      }, el).catch((err) => toast(err.message || "图片打不开", "err"));
    }));
    capAutoExpand?.();
  }

  async function addCapImages(files) {
    const raw = Array.from(files || []);
    if (!raw.length) return;
    const room = MAX_CAP_IMAGES - capImages.length;
    if (room <= 0) { toast(`一条最多带 ${MAX_CAP_IMAGES} 张图`, "warn"); return; }
    let added = 0, skipped = 0, tooLarge = 0;
    for (const rawFile of raw) {
      if (added >= room) { skipped++; continue; }
      const file = normalizeCapImage(rawFile);
      if (!file) { skipped++; continue; }
      if (file.size > MAX_CAP_IMAGE_BYTES) { tooLarge++; continue; }
      const url = await makePreviewUrl(file);
      if (!url) { skipped++; continue; }
      capImages.push({ id: `cap-img-${++capImageSeq}`, file, url });
      added++;
    }
    renderCapImages();
    if (tooLarge) toast(`有 ${tooLarge} 张图超过 15MB，没有加入`, "warn");
    if (skipped) toast(`有 ${skipped} 个文件不是支持的图片，或已到 ${MAX_CAP_IMAGES} 张上限`, "warn");
    if (added) toast(`已加入 ${added} 张图，点「收下」存进库`, "ok");
  }

  function collectClipboardImages(e) {
    const out = [], seen = new Set();
    const push = (file) => {
      if (!file) return;
      const key = `${file.name}|${file.size}|${file.lastModified}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push(file);
    };
    Array.from(e.clipboardData?.files || []).forEach(push);
    for (const item of Array.from(e.clipboardData?.items || [])) {
      if (item.kind === "file" && /^image\//i.test(item.type || "")) push(item.getAsFile());
    }
    return out;
  }

  async function uploadCapImage(file) {
    const q = new URLSearchParams({ name: file.name || "截图.png" });
    const res = await fetch("/api/notes/media?" + q.toString(), {
      method: "POST",
      body: file,
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-GL-Client": clientId,
      },
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* 非 JSON 就是服务端炸了 */ }
    if (!res.ok || data.error) throw new Error(data.error || `图片没传上去（HTTP ${res.status}）`);
    if (data.kind !== "image") throw new Error("这个文件没有被识别成图片");
    return {
      file: data.file,
      name: data.name || file.name || "图片",
      size: Number(data.size) || file.size || 0,
    };
  }

  function isImageFileClip(c) {
    if (!c || c.source_type !== "file") return false;
    const mime = String(c.mime_type || "").toLowerCase().split(";")[0].trim();
    const ext = capImageExt(c.file_name);
    return CAP_IMAGE_EXTS.has(ext)
      || (mime.startsWith("image/") && CAP_IMAGE_EXTS.has(CAP_IMAGE_EXT_BY_MIME[mime] || ""));
  }

  function clipImages(c, limit = 4) {
    const primary = (Array.isArray(c.preview_images) ? c.preview_images : []).slice(0, limit)
      .map((it) => {
        const src = String(it.url || "/api/notes/img/" + encodeURIComponent(it.file || ""));
        return {
          src, download: src + (src.includes("?") ? "&" : "?") + "dl=1",
          file: String(it.file || ""), name: String(it.name || it.file || "图片"),
          size: Number(it.size) || 0,
        };
      });
    if (primary.length) return primary;
    if (isImageFileClip(c)) {
      const src = `/api/clips/${c.id}/file`;
      return [{
        src, download: src + "?dl=1", file: "",
        name: String(c.file_name || c.title || "图片"), size: Number(c.file_size) || 0,
      }];
    }
    return [];
  }

  function imageTile(it, badge = "", detail = false) {
    return `<button type="button" class="cc-image"
        data-image-src="${esc(it.src)}" data-image-download="${esc(it.download)}"
        data-image-file="${esc(it.file)}" data-image-name="${esc(it.name)}"
        data-image-size="${Number(it.size) || 0}" title="点开看大图"
        aria-label="查看 ${esc(it.name)}">
      <img src="${esc(it.src)}" alt="" loading="lazy" decoding="async" draggable="false">
      <span class="cc-image-fallback">图片暂不可见</span>
      ${badge ? `<span class="cc-image-more">${esc(badge)}</span>` : ""}
      ${detail ? `<span class="cc-image-name">${esc(it.name)} · ${esc(fmtSize(it.size))}</span>` : ""}
    </button>`;
  }

  function imageGallery(c, opts = {}) {
    const detail = !!opts.detail;
    const list = clipImages(c, detail ? 24 : 4);
    if (!list.length) return "";
    const total = Math.max(Number(c.preview_image_count) || 0, list.length);
    const more = Math.max(0, total - list.length);
    const tiles = list.map((it, i) =>
      imageTile(it, i === list.length - 1 && more > 0 ? `+${more}` : "", detail)).join("");
    if (!detail) return `<div class="cc-images" aria-label="收存的图片">${tiles}</div>`;
    return `<section class="panel clip-gallery">
      <div class="panel-head"><div>
        <div class="panel-title">图片</div>
        <div class="panel-sub">${total} 张 · 点开看原图</div>
      </div></div>
      <div class="panel-body"><div class="cc-images is-detail">${tiles}</div></div>
    </section>`;
  }

  function bindImagePreviews(root = document) {
    $$("[data-image-src]", root).forEach((el) => {
      if (el.dataset.imageBound) return;
      el.dataset.imageBound = "1";
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        openNoteFilePreview({
          file: el.dataset.imageFile || "",
          name: el.dataset.imageName || "图片",
          size: Number(el.dataset.imageSize) || 0,
          src: el.dataset.imageSrc,
          download: el.dataset.imageDownload || el.dataset.imageSrc,
          forceImage: true,
        }, el).catch((err) => toast(err.message || "图片打不开", "err"));
      });
    });
    $$("[data-image-src] img", root).forEach((img) => {
      if (img.dataset.imageBound) return;
      img.dataset.imageBound = "1";
      img.addEventListener("error", () => img.closest("[data-image-src]")?.classList.add("is-missing"));
    });
  }

  window.addEventListener("beforeunload", () => capImages.forEach((it) => releasePreviewUrl(it.url)));

  const head = (title, meta) =>
    `<div class="head"><h1>${esc(title)}</h1><div class="meta">${esc(meta)}</div></div>`;

  const panel = (title, sub, body, flush) => `
    <div class="panel">
      ${title ? `<div class="panel-head"><div class="panel-title">${esc(title)}</div>
        <div class="panel-sub">${esc(sub || "")}</div></div>` : ""}
      <div class="panel-body${flush ? " flush" : ""}">${body}</div>
    </div>`;

  const empty = (big, hint) =>
    `<div class="empty"><div class="big">${esc(big)}</div><div class="hint">${esc(hint)}</div></div>`;

  /* ── 板块隔离：阅读中心的条目不算收存 ─────────────────
     书、正文与笔记都挂在 clips 上（删了书就没了），所以只在这里隐藏，
     不改数据。每次路由/实时同步都刷新一次集合，保证加入或移出立即生效。 */
  let readingItemsCache = null;
  async function readingItems() {
    if (readingItemsCache) return readingItemsCache;
    try {
      const d = await api("/api/reading/items?limit=200");
      readingItemsCache = (d.items || []).map((x) => ({
        id: Number(x.clip_id),
        board: x.clip?.board_id == null || x.clip?.board_id === "" ? null : String(x.clip.board_id),
      }));
    } catch (_) { readingItemsCache = []; }
    return readingItemsCache;
  }
  async function readingClipIds() {
    return new Set((await readingItems()).map((x) => x.id));
  }
  const invalidateReadingIds = () => { readingItemsCache = null; };

  // 从 /api/boards 的计数里扣掉已在阅读中心的条目，侧栏与列表口径一致。
  function boardsWithoutReading(data, items) {
    const per = {};
    let loose = 0;
    items.forEach((x) => {
      if (x.board) per[x.board] = (per[x.board] || 0) + 1;
      else loose++;
    });
    return {
      ...data,
      total: Math.max(0, Number(data.total || 0) - items.length),
      none: Math.max(0, Number(data.none || 0) - loose),
      boards: (data.boards || []).map((b) =>
        ({ ...b, count: Math.max(0, Number(b.count || 0) - (per[String(b.id)] || 0)) })),
    };
  }

  /* ── 路由 ──────────────────────────────────────────── */

  let routeSeq = 0;
  const VIEW_TITLES = {
    "": "收存", "ask": "问", "search": "检索", "verdicts": "判断",
    "insight": "洞察", "review": "回顾", "more": "设置", "reading": "阅读中心",
    "bible": "圣经阅读地", "events": "事件",
  };
  const ROUTES = [
    [/^\/?$/, viewVault],
    [/^\/b\/(\d+|none)$/, viewVault],
    [/^\/ask$/, viewAsk],
    [/^\/search$/, viewSearch],
    [/^\/verdicts$/, viewVerdicts],
    [/^\/insight$/, viewInsight],
    [/^\/review$/, viewReview],
    [/^\/more$/, viewMore],
    [/^\/c\/(\d+)$/, (m) => viewClip(m[1])],
  ];

  function currentPath() {
    const raw = location.hash.replace(/^#/, "");
    return raw.startsWith("/") ? raw : "/" + raw;
  }

  async function route() {
    const path = currentPath();
    // 从阅读中心加入/移出一本书会改变收存的可见集合，换路由时重新取一遍。
    invalidateReadingIds();
    // 阅读中心是平级大板块：切侧栏、切整页布局、清掉上一视图的计时器。
    window.GLReading?.onRoute?.(path);
    // 圣经阅读地是第三个平级大板块，后执行以便覆盖侧栏显隐。
    window.GLBible?.onRoute?.(path);
    // 事件是第四个平级板块，最后统一它的桌面侧栏与选中态。
    window.GLEvents?.onRoute?.(path);
    const seq = ++routeSeq;
    if (!path.startsWith("/c/")) {
      detailAbort?.abort();
      detailAbort = null;
      document.body.classList.remove("reader-focus");
    }
    // 批量选择只在列表里有意义，离开列表就收起
    if (!/^\/(b\/\d+|b\/none)?$/.test(path)) {
      picked.clear();
      $("#pick-bar")?.remove();
    }
    const hit = ROUTES.find(([re]) => re.test(path));
    if (!hit) { location.hash = "#/"; return; }
    try {
      await hit[1](path.match(hit[0]));
    } catch (e) {
      if (seq === routeSeq) {
        main.innerHTML = `<div class="err">${esc(e.message)}</div>`;
      }
    }
    if (seq !== routeSeq) return;
    // 各板块独立：收存详情页不注入阅读中心或事件板块的任何内容。
    // 阅读、事件各自在自己的板块内管理。
    // 单条页面也归在「收存」下面，导航高亮跟着走
    const key = path.split("/")[1] || "";
    const navKey = key === "c" ? "" : key;
    if (key === "reading") {
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
    }
    // 窗口标题跟着视图走：Edge --app 窗口和任务栏里才分得清哪个在看什么
    let docLabel = VIEW_TITLES[key] || "";
    if (key === "b") {
      const bid = path.split("/")[2];
      const b = (boardsCache || []).find((x) => String(x.id) === bid);
      docLabel = bid === "none" ? "未分类" : (b ? b.name : "收存");
    } else if (key === "c") {
      docLabel = docClipTitle || "条目";
    }
    document.title = docLabel ? `观澜 · ${docLabel}` : "观澜";
    refreshSidebar();
  }

  /* ── 多端实时同步 ────────────────────────────────────
     服务端只在写成功后推一个版本号，不把整页 HTML 塞进事件里。
     这里收到别的客户端的变化，就在用户没写草稿、没开弹窗时重走当前路由；
     正在输入就先记 pending，焦点离开后再追平。 */
  const liveSync = {
    es: null,
    version: 0,
    ready: false,
    pending: false,
    refreshing: false,
    timer: 0,
  };

  function liveDraftOpen() {
    if (document.querySelector(".gl-modal-backdrop,.pal-backdrop")) return true;
    const path = currentPath();
    // 不再对详情页/阅读页/圣经整页判「草稿」——否则 SSE 的 change 永远不重取，
    // 后台 PDF 抽取完成后详情页不会自动刷新出正文。真正在编辑的判断交给下面
    // 的字段检查（有非空输入才算草稿）；详情页与阅读页平时浏览是安全的，应允许刷新。
    const draftFields = [
      "#cap-body", "#cap-tags", "#note-text", "#sum-text", "#tag-input",
      "#verdict-text", "#verdict-q", "#verdict-ref", "#ai-instruct",
      "#board-new-input", "#clip-title",
    ];
    for (const sel of draftFields) {
      const el = $(sel);
      if (el && el.offsetParent !== null && String(el.value || "").trim()) return true;
    }
    if (window.GLEvents?.draftOpen?.()) return true;
    if (window.GLReading?.draftOpen?.()) return true;
    const askOut = $("#ask-out");
    if (askOut && askOut.textContent.trim()) return true;
    const reviewOut = $("#rv-out");
    if (reviewOut && reviewOut.textContent.trim()) return true;
    return false;
  }

  function liveRefreshSoon(delay = 180) {
    liveSync.pending = true;
    clearTimeout(liveSync.timer);
    liveSync.timer = setTimeout(liveRefresh, delay);
  }

  async function liveRefresh() {
    if (liveSync.refreshing) {
      liveSync.pending = true;
      return;
    }
    if (document.hidden || liveDraftOpen()) {
      liveSync.pending = true;
      return;
    }
    liveSync.pending = false;
    liveSync.refreshing = true;
    const top = window.scrollY;
    try {
      await route();
      requestAnimationFrame(() => window.scrollTo({ top, left: 0, behavior: "instant" }));
    } finally {
      liveSync.refreshing = false;
      if (liveSync.pending) liveRefreshSoon(260);
    }
  }

  const peerSync = { busy: false, lastAt: 0 };

  async function peerSyncTick(force = false) {
    if (peerSync.busy || document.hidden) return;
    if (!force && liveDraftOpen()) return;
    if (!force && Date.now() - peerSync.lastAt < 5000) return;
    peerSync.busy = true;
    peerSync.lastAt = Date.now();
    try {
      const result = await api("/api/sync/peer", { timeout: 150000 });
      if (!result || result.available === false) return;
      if (result.last_action === "pulled" || result.last_action === "conflict_pulled") {
        if (result.last_action === "conflict_pulled") {
          try { sessionStorage.setItem("gl_peer_conflict", "1"); } catch (_) { }
          toast("两端都改过，已采用最新版；本机改动留了冲突备份", "warn");
        }
        liveRefreshSoon(80);
        return;
      }
      if (result.last_action === "pushed") toast("已同步到电脑");
    } catch (_) {
      // 没开电脑、不同 WiFi、电脑睡眠都算正常离线状态，不打断用户。
    } finally {
      peerSync.busy = false;
    }
  }

  /* ── 云同步（WorkBuddy 云主机，公网可用） ──────────────
     和局域网同步同一套协议；run 是后台线程，这里触发后轮询状态。 */
  const cloudSync = { lastAt: 0, running: false };

  async function cloudSyncTick(force = false) {
    if (cloudSync.running || document.hidden) return;
    if (!force && liveDraftOpen()) return;
    if (!force && Date.now() - cloudSync.lastAt < 4 * 60 * 1000) return;
    cloudSync.running = true;
    cloudSync.lastAt = Date.now();
    try {
      const started = await post("/api/sync/cloud/run", {}).catch(() => null);
      if (!started || started.available === false) return;
      for (let i = 0; i < 200; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const st = await api("/api/sync/cloud").catch(() => null);
        if (!st) break;
        // 前两次轮询容忍线程还没抢到锁
        if (!st.running && i > 2) {
          if (st.last_action === "pulled" || st.last_action === "conflict_pulled") {
            if (st.last_action === "conflict_pulled") {
              try { sessionStorage.setItem("gl_peer_conflict", "1"); } catch (_) { }
              toast("两端都改过，已采用云端最新版；本机改动留了冲突备份", "warn");
            }
            liveRefreshSoon(80);
          }
          break;
        }
      }
    } catch (_) {
      // 云端不可达是常态（没网/服务维护），不打断用户。
    } finally {
      cloudSync.running = false;
    }
  }

  function startLiveSync() {
    if (!window.EventSource) return;
    const es = new EventSource("/api/events");
    liveSync.es = es;

    es.addEventListener("hello", (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data || "{}"); } catch (_) { return; }
      const version = Number(data.version) || 0;
      if (!liveSync.ready) {
        liveSync.ready = true;
        liveSync.version = version;
        return;
      }
      if (version !== liveSync.version) {
        liveSync.version = version;
        liveRefreshSoon(80);
      }
    });

    es.addEventListener("change", (ev) => {
      let data = {};
      try { data = JSON.parse(ev.data || "{}"); } catch (_) { return; }
      const version = Number(data.version) || 0;
      if (version === liveSync.version) return;
      liveSync.version = version;
      if (data.source && data.source === clientId) return;  // 自己写的，页面已经在处理
      liveRefreshSoon();
    });
  }

  // 正在写字/开会话时先不打断；一离开输入框就把挂起的更新追平。
  document.addEventListener("focusout", () => {
    if (!liveSync.pending) return;
    setTimeout(() => {
      if (!document.hidden && !liveDraftOpen()) liveRefresh();
    }, 180);
  }, true);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    setTimeout(() => cloudSyncTick(true), 300);
    if (liveSync.pending) {
      setTimeout(() => { if (!liveDraftOpen()) liveRefresh(); }, 120);
      return;
    }
    // 手机后台会冻结 EventSource；回到前台主动核一次版本，避免漏事件。
    setTimeout(async () => {
      try {
        const state = await api("/api/sync");
        if (Number(state.version) !== liveSync.version) {
          liveSync.version = Number(state.version) || 0;
          liveRefreshSoon(80);
        }
      } catch (_) { /* 网络没回来时 EventSource 会自己重连 */ }
      setTimeout(peerSyncTick, 520);
    }, 260);
  });

  // 真离开才关；进 bfcache 关掉的话，回来 EventSource 就再也不动了
  window.addEventListener("pagehide", (e) => { if (e.persisted) return; liveSync.es?.close(); }, { once: true });
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    try { liveSync.es?.close(); } catch (_) { /* 旧连接可能已经死了 */ }
    liveSync.es = null;
    liveSync.ready = false;
    startLiveSync();
  });

  async function refreshSidebar() {
    try {
      const [raw, st, reading] = await Promise.all([
        api("/api/boards"), api("/api/ai/status"), readingItems(),
      ]);
      const data = boardsWithoutReading(raw, reading);
      boardsCache = data.boards;
      $("#c-clips").textContent = data.total;
      $("#f-db").textContent = `${data.total} 条`;
      $("#f-ai").textContent = st.ready ? st.model : "未配置";
      $("#f-ai").style.color = st.ready ? "" : "var(--red)";
      renderBoards(data);
    } catch (_) { /* 侧栏状态失败不影响正文 */ }
  }

  function renderBoards(data) {
    const host = $("#board-list");
    if (!host) return;
    const cur = boardOfPath(currentPath());
    const item = (b) => `
      <div class="nav-item board-item ${cur === String(b.id) ? "active" : ""}
        ${b.kind === "archive" ? "arch" : ""}" data-board="${b.id}">
        <span class="board-name">${b.kind === "archive" ? `<span class="arch-mark">▣</span>` : ""}${esc(b.name)}</span>
        ${b.count ? `<span class="count">${b.count}</span>` : ""}</div>`;
    const archives = data.boards.filter((b) => b.kind === "archive");
    const normal = data.boards.filter((b) => b.kind !== "archive");
    const group = (label, list) => (list.length
      ? `<div class="nav-group board-group">${label}</div>${list.map(item).join("")}` : "");
    const loose = data.none
      ? `<div class="nav-item board-item ${cur === "none" ? "active" : ""}" data-board="none">
          <span class="board-name">未分类</span><span class="count">${data.none}</span></div>` : "";
    let shown = normal;
    if (!boardsExpanded && normal.length > 6) {
      shown = normal.slice(0, 6);
      const curBoard = normal.find((b) => String(b.id) === cur);
      if (curBoard && !shown.includes(curBoard)) shown = [...shown, curBoard];
    }
    const hidden = normal.length - shown.length;
    const more = normal.length > 6
      ? `<div class="nav-item nav-more" id="board-more"><span>${boardsExpanded ? "收起板块" : "更多板块"}</span>${!boardsExpanded && hidden ? `<span class="count">+${hidden}</span>` : ""}</div>`
      : "";
    host.innerHTML = group("资料库", archives) + group("板块", shown) + more + loose;
    $("#board-more")?.addEventListener("click", () => {
      boardsExpanded = !boardsExpanded;
      renderBoards(data);
    });
    $$("#board-list [data-board]").forEach((el) => el.addEventListener("click", () => {
      location.hash = `#/b/${el.dataset.board}`;
    }));
  }

  function bindBoardAdd() {
    const trigger = $("#board-add");
    const box = $("#board-new");
    const input = $("#board-new-input");
    const archBox = $("#board-new-archive");
    if (!trigger || !box || !input) return;
    trigger.addEventListener("click", () => {
      const open = box.classList.toggle("on");
      if (!open) return;
      input.value = "";
      if (archBox) archBox.checked = false;
      // 刚脱离 display:none 的元素在同一帧里 focus() 会落空，等一拍再聚焦
      setTimeout(() => input.focus(), 30);
    });
    input.addEventListener("keydown", async (e) => {
      if (e.key === "Escape") { box.classList.remove("on"); return; }
      if (e.key !== "Enter") return;
      const name = input.value.trim();
      if (!name) return;
      const kind = archBox && archBox.checked ? "archive" : "";
      try {
        const r = await post("/api/boards", { name, kind });
        input.value = "";
        if (archBox) archBox.checked = false;
        box.classList.remove("on");
        toast(kind === "archive" ? `资料库「${r.board.name}」建好了` : `「${r.board.name}」建好了`);
        await refreshSidebar();
        location.hash = `#/b/${r.board.id}`;
      } catch (err) { toast(err.message, "err"); }
    });
  }

  /* ══ 收存 ═══════════════════════════════════════════ */

  const PAGE = 24;
  let filter = { q: "", tag: "", offset: 0, board: "", tier: "", grade: "", hideOld: false };
  let picked = new Set();   // 批量定级时选中的 id；换页/换视图就清空
  let boardsCache = [];
  let boardsExpanded = false;   // 侧栏板块默认只露 6 个，点「更多板块」再展开
  let fabObserver = null;   // 详情的浮按钮观察器：换一条就换一个
  let notice = null;      // 重渲染后还要留在原地的一行提示（导入结果、重复定位…）
  let searchState = { q: "" };   // 检索词跟着会话走
  let docClipTitle = "";  // 当前打开的条目标题，给窗口标题用
  let detailAbort = null; // 详情页挂在 document 上的事件，用它统一收掉


  const boardOfPath = (path) => {
    const m = path.match(/^\/b\/(\d+|none)$/);
    return m ? m[1] : "";
  };

  const queryStr = () => {
    const q = new URLSearchParams();
    if (filter.q) q.set("q", filter.q);
    if (filter.tag) q.set("tag", filter.tag);
    if (filter.board) q.set("board", filter.board);
    if (filter.tier) q.set("tier", filter.tier);
    if (filter.grade) q.set("grade", filter.grade);
    if (filter.hideOld) q.set("hide_old", "1");
    q.set("limit", PAGE);
    q.set("offset", filter.offset);
    return q.toString();
  };

  // 收存列表统一入口：先按筛选条件取全量，剔掉已在阅读中心的条目，
  // 再按当前页切片。后端单次 limit 上限 100，超过就循环翻页，
  // 这样列表总数、分页数与侧栏计数都跟「收存实际可见的条目」一致。
  async function vaultList() {
    const base = new URLSearchParams(queryStr());
    base.set("limit", "100");
    base.set("offset", "0");
    const ids = await readingClipIds();
    const all = [];
    let offset = 0;
    for (;;) {
      base.set("offset", String(offset));
      const data = await api(`/api/clips?${base}`);
      const rows = data.clips || [];
      for (const c of rows) if (!ids.has(Number(c.id))) all.push(c);
      offset += rows.length;
      if (!data.has_more || !rows.length) break;
    }
    const total = all.length;
    const start = filter.offset;
    return {
      clips: all.slice(start, start + PAGE),
      total, limit: PAGE, offset: start,
      has_more: start + PAGE < total,
      _all: all,
    };
  }

  // 当前看的这个抽屉是不是资料库：决定卡片按「档案」还是按「摘要」排
  const archiveBoard = () => boardsCache.find(
    (b) => String(b.id) === filter.board && b.kind === "archive") || null;

  function card(c) {
    const tags = c.tags.slice(0, 4)
      .map((t) => `<span class="chip mini tag-hit" data-tag="${esc(t)}" title="看同标签的条目">${esc(t)}</span>`)
      .join("");
    const arch = archiveBoard();
    const body = arch
      ? (c.source_type === "file"
        ? `原件 · ${esc(c.file_name || "文件")}${c.file_size ? " · " + fmtSize(c.file_size) : ""}`
        : (c.has_content ? `正文 ${c.content_chars} 字 · 已抓下来` : "只存了链接，正文还没抓"))
      : (c.summary || c.excerpt
        || (c.has_content ? "" : (c.url ? "只存了链接，正文没抓到；点进去可以重试。" : "")));
    // 历史条目可能是「空正文 + stored + fetch_error」，也要标出来；
    // 只认 failed 会让它们一直装作已入库。
    const flag = (c.fetch_status === "failed" || (!c.has_content && c.fetch_error))
      ? `<span class="badge contradicted meta-flag" title="${esc(c.fetch_error || "正文没抓到")}">没抓到</span>` : "";
    return `<div class="clip-card g-${c.grade || "unset"}${arch ? " arch-card" : ""}${picked.has(c.id) ? " on" : ""}" data-id="${c.id}">
      <span class="pick-box${picked.has(c.id) ? " on" : ""}" data-pick="${c.id}"
        title="选中，批量定级 / 挪板块"></span>
      <div class="cc-title"><a href="#/c/${c.id}">${esc(c.title)}</a></div>
      <div class="cc-meta">
        ${c.board_name && !arch ? `<span class="cc-board">${esc(c.board_name)}</span>` : ""}
        ${gradeBadge(c.grade)}
        ${tierBadge(c.tier)}
        <span class="cc-domain">${c.domain ? esc(c.domain) : (c.file_name ? "文件" : "手记")}</span>
        <span class="sep">·</span><span>${esc(fmtDate(c.created_at))}</span>${flag}
      </div>
      ${imageGallery(c)}
      ${body ? `<div class="cc-excerpt">${esc(mdPlain(body, 300))}</div>` : ""}
      ${tags ? `<div class="cc-tags">${tags}</div>` : ""}
      <div class="cc-foot">
        ${c.annotation_count ? `<span class="cc-annotations">批注 ${c.annotation_count}</span>` : ""}
        ${c.note_count ? `<span class="cc-note">随手记 ${c.note_count}</span>` : ""}
        <span class="grow"></span><span class="cc-act">打开</span>
      </div>
    </div>`;
  }

  function gridMarkup(list) {
    if (!list.clips.length) {
      const narrowed = filter.q || filter.tag || filter.grade;
      const byGrade = filter.grade === "material" ? "材料层还空着：把权威/参考级的条目提上来，它就归这里。"
        : filter.grade === "daily" ? "资讯层还空着。每日新闻、快讯落在这里。"
        : filter.grade === "unset" ? "没有待判的条目——全都定过级了。"
        : "";
      return empty(narrowed ? "没有符合条件的条目" : "还空着",
        narrowed ? (byGrade || "清掉搜索或标签再看看")
          : "上面那个框里贴链接、写一段话、Ctrl+V 贴截图，或者拖图片/文件进来");
    }
    const pages = Math.max(1, Math.ceil(list.total / list.limit));
    const page = Math.floor(list.offset / list.limit) + 1;
    return `<div class="list-meta">共 ${list.total} 条 · 第 ${page}/${pages} 页</div>
      <div class="clip-grid">${list.clips.map(card).join("")}</div>
      ${pages > 1 ? `<div class="pager">
        <button class="btn small ghost" data-page="prev" ${list.offset <= 0 ? "disabled" : ""}>上一页</button>
        <button class="btn small ghost" data-page="next" ${!list.has_more ? "disabled" : ""}>下一页</button>
      </div>` : ""}`;
  }

  async function viewVault() {
    const seq = routeSeq;
    filter.board = boardOfPath(currentPath());
    // 隐私模式 / 禁用站点数据时 localStorage 会直接抛 SecurityError，
    // 不能让一个「是否隐藏回顾卡」的偏好把整个收存页打白屏。
    let hideResurface = false;
    try { hideResurface = !!localStorage.getItem("gl_resurface_hidden"); } catch (_) { /* 存不了就当没隐藏 */ }
    const wantResurface = !filter.q && !filter.tag && !filter.tier && !filter.board
      && !filter.grade && !filter.hideOld
      && filter.offset === 0 && !hideResurface;
    const [tags, list, st, rawBoards, resurf, gstat, reading] = await Promise.all([
      api("/api/clips/tags"), vaultList(), api("/api/ai/status"),
      api("/api/boards"),
      wantResurface ? api("/api/resurface?n=3").catch(() => null) : Promise.resolve(null),
      api("/api/grades" + (filter.board ? `?board=${encodeURIComponent(filter.board)}` : ""))
        .catch(() => null),
      readingItems(),
    ]);
    if (seq !== routeSeq) return;
    const boardData = boardsWithoutReading(rawBoards, reading);
    boardsCache = boardData.boards;
    // 翻页翻过头（别处删了条目）：退回第一页，别留一个空列表
    if (filter.offset > 0 && (filter.offset >= Number(list.total || 0) || !list.clips?.length)) {
      filter.offset = 0;
      return viewVault();
    }

    const current = boardData.boards.find((b) => String(b.id) === filter.board);
    const isArch = !!(current && current.kind === "archive");
    const viewTitle = current ? current.name
      : (filter.board === "none" ? "未分类" : "全部收存");
    const viewMeta = current
      ? (isArch ? `${list.total} 条 · 原件、原始数据与权威原文`
        : `${list.total} 条 · 这个抽屉里的东西`)
      : (filter.board === "none" ? `${list.total} 条 · 还没放进板块的`
        : `${list.total} 条 · 存下来的东西，AI 只读这里`);
    // 收存时的默认落位：正在哪个板块，就存进哪个；否则未分类
    let capBoard = current ? String(current.id) : "";
    const capChips = [["", "未分类"], ...boardData.boards.map((b) => [String(b.id), b.name])]
      .map(([value, label]) => {
        const b = boardData.boards.find((x) => String(x.id) === value);
        const mark = b && b.kind === "archive" ? "▣ " : "";
        return `<span class="chip ${capBoard === value ? "on" : ""}" data-cap-board="${esc(value)}">${mark}${esc(label)}</span>`;
      })
      .join("");

    const chips = tags.tags.map((t) => `
      <span class="chip ${filter.tag === t.tag ? "on" : ""}" data-tag="${esc(t.tag)}">${esc(t.tag)}<span class="chip-n">${t.n}</span></span>`).join("");

    /* ── 材料级的题头：这是「分开」，不是「筛选」 ── */
    // 材料级统计也按「剔除阅读中心后的可见条目」重算，口径与列表一致。
    const gradeCount = (g) => list._all.filter((c) => (c.grade || "") === g).length;
    const gcounts = {
      all: list.total,
      canon: gradeCount("canon"), ref: gradeCount("ref"),
      material: gradeCount("canon") + gradeCount("ref"),
      daily: gradeCount("daily"), unset: gradeCount(""),
    };
    const stale = (gstat && gstat.stale) || 0;
    // 手机上没有左侧栏，板块换成一条横滑的胶囊
    const mBoards = `<div class="m-boards">
      <a class="m-board ${filter.board === "" ? "on" : ""}" href="#/">全部<span>${boardData.total}</span></a>
      ${boardData.boards.map((b) => `<a class="m-board ${filter.board === String(b.id) ? "on" : ""}"
        href="#/b/${b.id}">${b.kind === "archive" ? "▣ " : ""}${esc(b.name)}<span>${b.count}</span></a>`).join("")}
      <a class="m-board ${filter.board === "none" ? "on" : ""}"
        href="#/b/none">未分类<span>${boardData.none || 0}</span></a>
    </div>`;

    const gTabs = [
      ["", "全部", gcounts.all, "所有收存"],
      ["material", "材料", gcounts.material, `${gcounts.canon} 条权威 · ${gcounts.ref} 条参考：能当依据的`],
      ["daily", "资讯", gcounts.daily, "每日新闻、快讯：只作时效背景"],
      ["unset", "未定", gcounts.unset, "还没判的：AI 仍会引用，只是标明未定"],
    ];
    const gTabsHtml = `<div class="g-head">
      <span class="g-label" title="这一层回答的是「它属于库里的哪一层」，和右边那个 T0–T4 信源分级是两回事">材料级</span>
      <div class="g-tabs">${gTabs.map(([value, label, n, hint]) => `
        <span class="g-tab ${filter.grade === value ? "on" : ""}" data-grade-tab="${value}"
          title="${esc(hint)}">${label}<span class="g-n">${n || 0}</span></span>`).join("")}</div>
      ${current ? `<span class="g-scope">只算这个板块</span>` : ""}
    </div>`;
    const staleBar = (filter.grade === "daily" && (stale || filter.hideOld)) ? `
      <div class="filters stale-bar">
        <span class="faint">${stale ? `其中 ${stale} 条已过 60 天` : "旧资讯已收起"}</span>
        <span class="grow"></span>
        <span class="cc-act" id="old-toggle">${filter.hideOld ? "显示旧资讯" : "收起旧资讯"}</span>
      </div>` : "";

    const resurface = (resurf && resurf.items && resurf.items.length) ? `
      <div class="resurface" id="resurface">
        <div class="rf-head">
          <span class="f-label">该重看</span>
          <span class="rf-hint">翻出来的旧东西，不是新推荐</span>
          <span class="grow"></span>
          <span class="cc-act" id="rf-next">换一批</span>
          <span class="cc-act" id="rf-hide">收起</span>
        </div>
        ${resurf.items.map((it) => `
          <a class="rf-row" href="#/c/${it.id}">
            <span class="rf-title">${esc(it.title)}</span>
            <span class="rf-why">${esc(it.reason)}</span>
          </a>`).join("")}
      </div>` : "";

    main.innerHTML = head(viewTitle, viewMeta)
      + mBoards
      + gTabsHtml
      + staleBar
      + resurface
      + (current ? (isArch
        ? `<div class="filters board-acts arch-acts">
            <span class="arch-tag">资料库</span>
            <span class="faint">原件、原始数据、权威原文放一处</span>
            <span class="grow"></span>
            <button class="btn small ghost" id="board-collect"
              title="把标成「权威」「参考」的材料整批挪进来">收进权威与参考${boardData.material ? `（${boardData.material}）` : ""}</button>
            <button class="btn small ghost" id="board-rename">重命名</button>
            <button class="btn small ghost" id="board-kind" data-kind="">改成普通板块</button>
            <button class="btn small ghost danger" id="board-del">删除板块</button>
          </div>`
        : `<div class="filters board-acts">
            <span class="faint">板块 · ${current.count} 条</span>
            <span class="grow"></span>
            <button class="btn small ghost" id="board-kind" data-kind="archive"
              title="变成资料库：专门放原件与权威原文">设为资料库</button>
            <button class="btn small ghost" id="board-rename">重命名</button>
            <button class="btn small ghost danger" id="board-del">删除板块</button>
          </div>`) : "")
      + panel("", "", `
        <div class="cap" id="cap">
          <div class="cap-top">
            <span class="cap-label">收存</span>
            <span class="cap-hint">贴链接、写一段话，或 Ctrl+V 贴截图</span>
            <span class="grow"></span>
            <button class="btn small ghost" id="cap-more" type="button" aria-expanded="false">更多</button>
            <button class="btn" id="cap-save-top" type="button">收下</button>
          </div>
          <textarea id="cap-body" data-md-preview spellcheck="false" placeholder="贴一条链接、写下一段话，或 Ctrl+V 贴截图。Ctrl+Enter 收下。"></textarea>
          <div class="cap-extra" id="cap-extra">
            <div class="cap-images" id="cap-images" aria-live="polite"></div>
            <div class="cap-foot">
              <input type="text" id="cap-tags" placeholder="标签，逗号分隔（可留空）">
              <label class="btn small ghost file-btn" title="把文件作为独立原件收存，图片会直接在卡片上显示">导入文件
                <input type="file" id="cap-files" multiple
                  accept=".txt,.md,.markdown,.csv,.tsv,.json,.log,.url,.html,.htm,.docx,.pdf,.pptx,.xlsx,.epub,.png,.jpg,.jpeg,.gif,.webp,image/png,image/jpeg,image/gif,image/webp"></label>
              <label class="btn small ghost file-btn" title="把图片贴进这条笔记，收下后和文字一起显示">插图
                <input type="file" id="cap-photos" multiple
                  accept=".png,.jpg,.jpeg,.gif,.webp,image/png,image/jpeg,image/gif,image/webp"></label>
              ${st.ready ? `<label class="check-label"><input type="checkbox" id="cap-ai" checked> 收下后 AI 看一眼</label>` : ""}
              <span class="grow"></span>
              <button class="btn" id="cap-save">收下</button>
            </div>
            <div class="cap-boards" id="cap-boards">
              <span class="f-label">存入</span>${capChips}
              <span class="f-label grade-label">材料级</span>
              ${gradeSegs("", { id: "cap-grade", small: true })}
              <span class="f-label tier-label">信源</span>
              <select id="cap-tier" class="compact-select tier-select" title="信源分级">${tierOptions("")}</select>
            </div>
            <div class="faint cap-msg" id="cap-msg" role="status"></div>
          </div>
        </div>`)
      + `<div class="filters">
          <input type="search" id="v-search" value="${esc(filter.q)}"
            placeholder="搜标题、正文、摘要、标签…" style="flex:1;min-width:200px">
          <select id="v-tier" class="compact-select" title="信源分级">
            <option value="">全部信源</option>
            <option value="none" ${filter.tier === "none" ? "selected" : ""}>未分级</option>
            ${TIERS.map(([k, n]) => `<option value="${k}" ${filter.tier === k ? "selected" : ""}>${k} ${n}</option>`).join("")}
          </select>
        </div>`
      + (filter.tag ? `<div class="filters tag-active">
          <span class="f-label">正在按标签看</span>
          <span class="chip on" id="tag-clear" title="点一下取消筛选">${esc(filter.tag)} ×</span>
          <span class="faint">${list.total} 条</span>
        </div>` : "")
      + (chips ? `<div class="filters filter-scroll tag-row"><span class="f-label">标签</span><span class="tag-set" id="tag-set">${chips}</span>${tags.tags.length > 10 ? `<button type="button" class="tag-more" id="tag-more">全部 ${tags.tags.length}</button>` : ""}</div>` : "")
      + `<div class="panel"><div class="panel-body" id="grid-host">${gridMarkup(list)}</div></div>`;

    const body = $("#cap-body");
    body.value = capText;
    body.addEventListener("input", () => { capText = body.value; });
    bindMdPreview(body.parentElement);
    renderCapImages();

    // 桌面端默认把收存框收成一行；手机上保持原来的展开状态，避免多一步点击。
    const cap = $("#cap");
    const capExtra = $("#cap-extra");
    const capMore = $("#cap-more");
    const capDesktop = window.matchMedia("(min-width: 901px)").matches;
    let capOpen = !capDesktop;
    const syncCap = () => {
      if (!cap || !capExtra) return;
      cap.classList.toggle("is-open", capOpen);
      capExtra.hidden = !capOpen;
      if (capMore) {
        capMore.textContent = capOpen ? "收起" : "更多";
        capMore.setAttribute("aria-expanded", String(capOpen));
      }
    };
    syncCap();
    capAutoExpand = () => {
      if (capImages.length && !capOpen) { capOpen = true; syncCap(); }
    };
    if (capImages.length) capAutoExpand();
    capMore?.addEventListener("click", () => {
      capOpen = !capOpen;
      syncCap();
    });

    const tagMore = $("#tag-more");
    if (tagMore) {
      tagMore.addEventListener("click", () => {
        const set = $("#tag-set");
        const open = set.classList.toggle("open");
        tagMore.textContent = open ? "收起" : `全部 ${tags.tags.length}`;
      });
    }

    // 只在干净的默认状态下抢焦点，翻页/筛标签时不打断输入
    if (!filter.q && !filter.tag && filter.offset === 0 && !capImages.length) body.focus();

    // 提示要能活过一次重渲染，否则"导入完成"刚写上就被刷掉了
    if (notice) {
      const host = $("#cap-msg");
      if (host) host.innerHTML = notice;
      notice = null;
    }

    // fromPath：调用方开始时的路由，路由已经切走就不再重渲染
    const refresh = async (fromPath) => {
      filter.offset = 0;
      if (fromPath && currentPath() !== fromPath) return;
      await viewVault();
      refreshSidebar();
    };

    let saving = false;

    async function save() {
      if (saving) return;
      // 视图可能在这些 await 之间被路由/实时同步换掉：先把节点和起始路由抓在手里
      const startPath = currentPath();
      const msgEl = $("#cap-msg");
      const tagsEl = $("#cap-tags");
      const tierEl = $("#cap-tier");
      const gradeEl = $("#cap-grade .g-seg.on");
      const alive = () => (msgEl ? msgEl.isConnected : body.isConnected);
      const text = body.value.trim();
      capText = body.value;
      if (!text && !capImages.length) { body.focus(); return toast("写点什么，或者贴张图", "warn"); }
      saving = true;
      try {
        const pending = capImages.slice();
        const media = [];
        for (let i = 0; i < pending.length; i++) {
          const item = pending[i];
          if (msgEl) msgEl.textContent = `上传图片 ${i + 1}/${pending.length}：${item.file.name}`;
          try {
            media.push(await uploadCapImage(item.file));
          } catch (e) {
            toast(`${item.file.name} 没传上去：${e.message}`, "err");
          }
          if (!alive()) return;
        }
        if (!text && !media.length) {
          if (msgEl) msgEl.textContent = "";
          return toast("图片没传上去，先重试一次", "err");
        }
        const tags = (tagsEl?.value || "").split(/[,，]/).map((s) => s.trim()).filter(Boolean);
        const isUrl = /^https?:\/\/\S+$/i.test(text);
        const boardId = capBoard ? Number(capBoard) : null;
        const tier = tierEl?.value || "";
        const grade = gradeEl?.dataset.gradePick || "";
        const boardLabel = capBoard
          ? (boardsCache.find((b) => String(b.id) === capBoard) || {}).name : "";
        const stamp = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
        const fallbackTitle = pending.length > 1 ? `图片 · ${pending.length} 张 · ${stamp}` : `图片 · ${stamp}`;
        const payload = isUrl
          ? { url: text, tags, board_id: boardId, tier, grade }
          : { excerpt: text,
              // 标题取第一行，但要把 Markdown 记号摘掉：别让卡片标题写着「# 」
              title: text ? mdPlain(text.split("\n").find((l) => l.trim()) || text, 60) : fallbackTitle,
              tags, source_type: "text", board_id: boardId, tier, grade };
        if (media.length) payload.images = media;
        if (msgEl) msgEl.textContent = isUrl ? "抓正文并收下…" : "收下…";
        let saved;
        try {
          saved = await post("/api/clips", payload);
        } catch (e) {
          if (msgEl) msgEl.innerHTML = `<span class="err-text">${esc(e.message)}</span>`;
          return;
        }
        // 条目已经写库；视图被切走时也要把草稿清掉，否则回来会重复提交
        if (!alive()) { capText = ""; clearCapImages(); return; }
        const cid = saved.clip.id;
        const where = boardLabel ? ` → ${boardLabel}` : "";
        if (saved.duplicate) {
          toast(media.length ? `这条已经在库里（#${cid}），图片已补到原条目`
            : `这条已经在库里（#${cid}），没有重复收`, "ok");
        } else if (saved.import_status === "saved_fetch_failed") {
          toast(`已存 #${cid}${where}，但网页正文没抓到`, "warn");
        } else {
          toast(media.length ? `收下 #${cid}${where} · ${media.length} 张图` : `收下 #${cid}${where}`);
        }

        // 收下就让 AI 看一眼：一句话简介 + 标签，然后正文在服务端丢掉
        const worthDigest = isUrl || text.length >= 60;
        if ($("#cap-ai")?.checked && !saved.duplicate && worthDigest) {
          if (msgEl) msgEl.textContent = "AI 看一眼，写简介和标签…";
          try {
            const d = await post("/api/ai/digest", { id: cid });
            // 只是建议：点进那条才看得到「采纳」，这里只提醒一句
            if (d && d.grade_hint) {
              toast(`AI 判为${d.grade_hint_label}：${d.grade_hint_reason || ""}`, "ok");
            }
          } catch (e) {
            if (!/不足 20 字/.test(e.message)) toast(`简介没写成：${e.message}`, "warn");
          }
          if (!alive()) { capText = ""; clearCapImages(); return; }
        }
        capText = "";
        body.value = "";
        clearCapImages();
        await refresh(startPath);
      } finally {
        saving = false;
      }
    }

    $("#cap-save").addEventListener("click", (ev) => withBusy(ev.currentTarget, save));
    $("#cap-save-top")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, save));
    body.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); save(); }
    });

    // 存入哪个板块：点一下换一个，不重排页面
    $$("#cap-boards [data-cap-board]").forEach((el) => el.addEventListener("click", () => {
      capBoard = el.dataset.capBoard;
      $$("#cap-boards [data-cap-board]").forEach((x) =>
        x.classList.toggle("on", x.dataset.capBoard === capBoard));
    }));

    $("#board-rename")?.addEventListener("click", async () => {
      const name = await glPrompt("板块新名字", current.name);
      if (!name || name.trim() === current.name) return;
      try {
        await post(`/api/boards/${current.id}`, { name: name.trim() });
        toast("改好了");
        viewVault();
        refreshSidebar();
      } catch (e) { toast(e.message, "err"); }
    });
    $("#board-del")?.addEventListener("click", async () => {
      if (!(await glConfirm(`删除「${current.name}」？里面的 ${current.count} 条会退回未分类，不会被删。`,
        { danger: true, confirmText: "删除" }))) return;
      try {
        await post(`/api/boards/${current.id}/delete`, {});
        toast("板块删了，条目退回未分类");
        location.hash = "#/";
      } catch (e) { toast(e.message, "err"); }
    });

    // 普通板块 ⇄ 资料库：只改性格，不动条目
    $("#board-kind")?.addEventListener("click", async (ev) => {
      const next = ev.currentTarget.dataset.kind || "";
      try {
        await post(`/api/boards/${current.id}`, { kind: next });
        toast(next === "archive" ? "设成资料库了" : "改回普通板块了");
        await refreshSidebar();
        viewVault();
      } catch (e) { toast(e.message, "err"); }
    });

    // 一键把权威 / 参考级材料归到资料库
    $("#board-collect")?.addEventListener("click", async (ev) => {
      const n = boardData.material || 0;
      if (!n) { toast("库里还没有标成权威或参考的材料", "warn"); return; }
      if (!(await glConfirm(`把 ${n} 条标成「权威 / 参考」的材料挪进「${current.name}」？\n它们会从原来的板块移走，条目本身不动。`,
        { confirmText: "挪过去" }))) return;
      try {
        const r = await post(`/api/boards/${current.id}/collect`, {});
        toast(r.moved ? `${r.moved} 条收进来了` : "已经在库里了", "ok");
        await refreshSidebar();
        viewVault();
      } catch (e) { toast(e.message, "err"); }
    });

    /* 导入文件：按钮、拖拽走同一条路。
       走 /api/clips/upload 的原始二进制通道——不 base64、不塞 JSON，
       所以没有 10MB 请求体那种天花板，服务端边收边写盘。 */
    function uploadFile(file, meta, onProgress) {
      return new Promise((resolve, reject) => {
        const q = new URLSearchParams();
        q.set("name", file.name);
        if (meta.tags && meta.tags.length) q.set("tags", meta.tags.join(","));
        if (meta.board) q.set("board", String(meta.board));
        if (meta.tier) q.set("tier", meta.tier);
        if (meta.grade) q.set("grade", meta.grade);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/clips/upload?" + q.toString());
        xhr.setRequestHeader("X-GL-Client", clientId);
        xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
        xhr.timeout = 0;                       // 大文件不设上限，交给用户自己取消
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total, e.loaded, e.total);
        };
        xhr.onload = () => {
          // 200 响应头 + 0 字节 body 也要算失败：服务端写回时客户端已断开
          // （BrokenPipeError）就会出现这种组合。旧写法 `responseText || "{}"`
          // 把空串兜底成 "{}"，JSON.parse 不抛错、data={}，于是假成功。
          if (!xhr.responseText) {
            reject(new Error(`上传失败：服务端没有返回数据（HTTP ${xhr.status}）`));
            return;
          }
          let data = null;
          try { data = JSON.parse(xhr.responseText); } catch (_) { /* 非 JSON 就是服务端炸了 */ }
          // HTML 错误页 / "null" / 数组也都不是成功的上传结果，不能让前端
          // 把原件从待上传列表里划掉。
          const okShape = data && typeof data === "object" && !Array.isArray(data);
          if (xhr.status >= 200 && xhr.status < 300 && okShape && !data.error) resolve(data);
          else reject(new Error((okShape && data.error) || `上传失败（HTTP ${xhr.status}）`));
        };
        xhr.onerror = () => reject(new Error("网络断了，上传没完成"));
        xhr.ontimeout = () => reject(new Error("上传超时"));
        xhr.send(file);
      });
    }

    async function importFiles(files) {
      // 截图有时没有扩展名；补成 .png 之类，服务端才会按图片内联展示。
      files = Array.from(files || []).map((f) => normalizeCapImage(f) || f);
      if (!files.length) return;
      const input = $("#cap-files");
      const msg = $("#cap-msg");
      const say = (text, bad) => {
        if (!msg) return;
        msg.innerHTML = bad ? `<span class="err-text">${esc(text)}</span>` : esc(text);
      };
      const tierEl = $("#cap-tier");
      const tagEl = $("#cap-tags");
      const meta = {
        tags: tagEl ? tagEl.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean) : [],
        board: capBoard || "",
        tier: tierEl ? tierEl.value || "" : "",
        grade: $("#cap-grade .g-seg.on")?.dataset.gradePick || "",
      };
      if (input) input.disabled = true;

      let ok = 0, dup = 0, fail = 0, dupNote = "", dupLink = "";
      try {
        for (let i = 0; i < files.length; i++) {
          const file = files[i];
          say(`导入 ${i + 1}/${files.length}：${file.name}`);
          try {
            const r = await uploadFile(file, meta, (ratio, loaded, total) => {
              const pct = Math.round(ratio * 100);
              say(`上传 ${file.name} · ${pct}%（${fmtSize(loaded)} / ${fmtSize(total)}）`);
            });
            if (r.duplicate) {
              dup++;
              const hit = r.clip || {};
              dupNote = `「${esc(hit.title || file.name)}」已经在库里`;
              dupLink = `#/c/${hit.id}`;
              toast(`这条已经收过了：${hit.title || file.name}`, "ok");
            } else ok++;
            if (r.parse_message) toast(`${file.name}：${r.parse_message}`, "warn");
          } catch (e) {
            fail++;
            toast(`${file.name} 没进得来：${e.message}`, "err");
          }
        }
      } finally {
        if (input) { input.disabled = false; input.value = ""; }
      }
      const head = `导入完成：新增 ${ok}，重复 ${dup}，失败 ${fail}`;
      const dupHtml = dupNote
        ? ` · ${dupNote}${dupLink ? ` <a href="${dupLink}">打开</a>` : ""}`
        : "";
      notice = esc(head) + dupHtml;
      await refresh();
    }

    $("#cap-files").addEventListener("change", (ev) => {
      importFiles(Array.from(ev.currentTarget.files || []));
    });
    $("#cap-photos").addEventListener("change", (ev) => {
      addCapImages(Array.from(ev.currentTarget.files || []));
      ev.currentTarget.value = "";
    });

    // 截图直接 Ctrl+V：有文字时让文字照常粘进来，图片同时进待收托盘。
    body.addEventListener("paste", (e) => {
      const images = collectClipboardImages(e);
      if (!images.length) return;
      const text = e.clipboardData?.getData("text/plain") || "";
      if (!text.trim()) e.preventDefault();
      addCapImages(images);
    });

    // 拖进来也能收：纯图片贴进这条笔记，其它文件仍按独立原件导入。
    const zone = $(".cap");
    if (zone) {
      const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
      zone.addEventListener("dragover", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        zone.classList.add("drag-on");
      });
      zone.addEventListener("dragleave", (e) => {
        if (e.target === zone || !zone.contains(e.relatedTarget)) zone.classList.remove("drag-on");
      });
      zone.addEventListener("drop", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        zone.classList.remove("drag-on");
        const dropped = Array.from(e.dataTransfer.files || []);
        if (dropped.length && dropped.every(isSupportedImageFile)) {
          addCapImages(dropped);
          return;
        }
        importFiles(dropped);
      });
    }

    $("#rf-next")?.addEventListener("click", () => viewVault());
    $("#rf-hide")?.addEventListener("click", () => {
      try { localStorage.setItem("gl_resurface_hidden", "1"); } catch (_) { /* 只影响本次会话 */ }
      $("#resurface")?.remove();
    });

    $$("[data-tag]").forEach((el) => el.addEventListener("click", () => {
      filter.tag = filter.tag === el.dataset.tag ? "" : el.dataset.tag;
      filter.offset = 0;
      if (filter.tag) filter.q = "";       // 换标签时清掉文字搜索，结果才对得上
      viewVault();
    }));
    $("#tag-clear")?.addEventListener("click", () => {
      filter.tag = ""; filter.offset = 0; viewVault();
    });
    $("#v-tier").addEventListener("change", (e) => {
      filter.tier = e.target.value; filter.offset = 0; viewVault();
    });

    /* ── 材料级：题头、收存栏、批量定级 ── */

    $$("[data-grade-tab]").forEach((el) => el.addEventListener("click", () => {
      filter.grade = el.dataset.gradeTab;
      filter.offset = 0;
      filter.hideOld = false;
      picked.clear();
      viewVault();
    }));
    $("#old-toggle")?.addEventListener("click", () => {
      filter.hideOld = !filter.hideOld;
      filter.offset = 0;
      viewVault();
    });
    $$("#cap-grade .g-seg").forEach((el) => el.addEventListener("click", () => {
      $$("#cap-grade .g-seg").forEach((x) => x.classList.toggle("on", x === el));
    }));

    renderPickedBar();

    let timer = null;
    let searchSeq = 0;
    const runSearch = async () => {
      const input = $("#v-search");
      if (!input || !input.isConnected) return;
      const seqAtStart = routeSeq;
      const mine = ++searchSeq;
      filter.q = input.value.trim();
      filter.offset = 0;
      try {
        const result = await vaultList();
        if (mine !== searchSeq || seqAtStart !== routeSeq) return;
        const host = $("#grid-host");
        if (!host || !host.isConnected) return;   // 翻页/重渲染已经换掉了这一块
        host.innerHTML = gridMarkup(result);
        bindGrid();
      } catch (e) { if (mine === searchSeq) toast(e.message, "err"); }
    };
    $("#v-search").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(runSearch, 320); });
    $("#v-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(timer); runSearch(); } });

    function bindGrid() {
      $$(".clip-card", $("#grid-host")).forEach((el) => el.addEventListener("click", (e) => {
        if (e.target.closest("a") || e.target.closest("[data-tag]")
          || e.target.closest("[data-pick]") || e.target.closest("[data-image-src]")) return;
        location.hash = `#/c/${el.dataset.id}`;
      }));
      bindImagePreviews($("#grid-host"));
      $$("[data-pick]", $("#grid-host")).forEach((box) => box.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const cid = Number(box.dataset.pick);
        if (picked.has(cid)) picked.delete(cid); else picked.add(cid);
        box.classList.toggle("on", picked.has(cid));
        box.closest(".clip-card")?.classList.toggle("on", picked.has(cid));
        renderPickedBar();
      }));
      $$("[data-page]", $("#grid-host")).forEach((button) => button.addEventListener("click", () => {
        filter.offset = Math.max(0, filter.offset + (button.dataset.page === "next" ? PAGE : -PAGE));
        viewVault();
      }));
    }

    function renderPickedBar() {
      let bar = $("#pick-bar");
      if (!picked.size) { bar?.remove(); return; }
      if (!bar) {
        bar = document.createElement("div");
        bar.id = "pick-bar";
        bar.className = "pick-bar";
        document.body.appendChild(bar);
      }
      bar.innerHTML = `<span class="pb-count">已选 ${picked.size} 条</span>
        <span class="f-label">设为</span>
        ${gradeSegs("", { small: true, id: "pick-grade" })}
        <select id="pick-move" class="compact-select" title="挪到别的板块">
          <option value="">移到…</option>
          <option value="0">未分类（移出）</option>
          ${boardsCache.map((b) => `<option value="${b.id}">${b.kind === "archive" ? "▣ " : ""}${esc(b.name)}</option>`).join("")}
        </select>
        <span class="grow"></span>
        <span class="cc-act" id="pick-clear">取消</span>`;
      $("#pick-move")?.addEventListener("change", async (ev) => {
        const value = ev.target.value;
        if (!value) return;
        const target = value === "0" ? "未分类"
          : ((boardsCache.find((b) => String(b.id) === value) || {}).name || "板块");
        try {
          const r = await post("/api/clips/batch",
            { ids: [...picked], action: "move", board: value === "0" ? null : Number(value) });
          picked.clear();
          toast(`${r.changed} 条移到「${target}」`, "ok");
          await refresh();
        } catch (e) { toast(e.message, "err"); }
      });
      $$("#pick-grade .g-seg").forEach((el) => el.addEventListener("click", async () => {
        try {
          const r = await post("/api/clips/grade", { ids: [...picked], grade: el.dataset.gradePick });
          picked.clear();
          toast(`${r.changed} 条改成「${r.grade_label}」`, "ok", r.grade || "unset");
          await refresh();
        } catch (e) { toast(e.message, "err"); }
      }));
      $("#pick-clear")?.addEventListener("click", () => {
        picked.clear();
        viewVault();
      });
    }

    bindGrid();
  }

  /* ══ 单条 ═══════════════════════════════════════════ */

  /* 正文批注：五种颜色各有语义，侧栏和正文共用同一套词（阅读中心同步）。 */
  const ANNOTATION_COLORS = {
    amber: { label: "重点", hint: "值得留下" },
    sage: { label: "论据", hint: "可作支撑" },
    blue: { label: "疑问", hint: "待核实" },
    rose: { label: "反驳", hint: "有异议" },
    violet: { label: "联想", hint: "由此及彼" },
  };
  const annColor = (color) => (ANNOTATION_COLORS[color] ? color : "amber");

  /* 正文重抓后字符位置会漂：先按前文/后文找最像的原句，找不到再标成“原文有变”。 */
  function prepareAnnotations(text, raw) {
    const source = String(text || "");
    return (Array.isArray(raw) ? raw : []).map((item) => {
      const a = { ...item };
      let start = Math.max(0, Number(a.start_offset ?? a.start) || 0);
      let end = Math.max(start, Number(a.end_offset ?? a.end) || 0);
      const quote = String(a.quote || "");
      let orphan = false;
      if (quote && source.slice(start, end) !== quote) {
        const hits = [];
        let at = Math.max(0, source.indexOf(quote));
        while (at >= 0 && hits.length < 80) {
          hits.push(at);
          at = source.indexOf(quote, at + Math.max(1, quote.length));
        }
        let best = null;
        let bestScore = -Infinity;
        for (const candidate of hits) {
          const around = source.slice(Math.max(0, candidate - 40), candidate);
          const after = source.slice(candidate + quote.length, candidate + quote.length + 40);
          let score = -Math.abs(candidate - start) / 80;
          if (a.prefix && around.endsWith(String(a.prefix).slice(-24))) score += 24;
          if (a.suffix && after.startsWith(String(a.suffix).slice(0, 24))) score += 24;
          if (score > bestScore) { bestScore = score; best = candidate; }
        }
        if (best === null) orphan = true;
        else { start = best; end = best + quote.length; }
      }
      return { ...a, color: annColor(a.color), _start: start, _end: end, _orphan: orphan };
    }).sort((a, b) => (a._start - b._start) || (Number(a.id) - Number(b.id)));
  }

  function bodyBlocks(text) {
    const source = String(text || "");
    const paras = source.split(/\n{2,}/);
    let cursor = 0;
    return paras.map((para, index) => {
      const found = source.indexOf(para, cursor);
      const start = found >= 0 ? found : cursor;
      cursor = start + para.length;
      return { text: para, start, end: start + para.length, index };
    });
  }

  function paragraphHtml(block, annotations) {
    const source = String(block.text || "");
    const hits = annotations
      .filter((a) => !a._orphan && a._start < block.end && a._end > block.start)
      .map((a) => ({
        ...a,
        _from: Math.max(block.start, a._start),
        _to: Math.min(block.end, a._end),
      }))
      .filter((a) => a._to > a._from)
      .sort((a, b) => (a._from - b._from) || (a._to - b._to));
    let pos = block.start;
    let html = "";
    for (const a of hits) {
      if (a._from < pos) continue;                 // 旧数据若重叠，先画前一条，别生成坏标签
      const before = block.text.slice(pos - block.start, a._from - block.start);
      const marked = block.text.slice(a._from - block.start, a._to - block.start);
      const color = annColor(a.color);
      const label = ANNOTATION_COLORS[color].label;
      const hint = a.note ? " · 有批注" : " · 点按编辑";
      html += esc(before).replace(/\n/g, "<br>");
      html += `<mark class="ann-mark ann-${color}" data-ann-id="${Number(a.id)}"
        tabindex="0" role="button" aria-label="${esc(label + hint)}"
        title="${esc(label + hint)}">${esc(marked).replace(/\n/g, "<br>")}</mark>`;
      pos = a._to;
    }
    html += esc(block.text.slice(pos - block.start)).replace(/\n/g, "<br>");
    return html;
  }

  /* 正文：段落保留原始字符区间，图片仍按后端记的段落位置插回。
     data-a-start / data-a-end 是正文坐标，选中文字后靠它精确落位。 */
  function readerHtml(text, images, annotations = []) {
    const source = String(text || "");
    const paras = bodyBlocks(source);
    const list = (Array.isArray(images) ? images : []).filter((it) => it && it.f);
    const byAt = new Map();
    list.forEach((it) => {
      const at = Math.max(0, Math.min(paras.length, Number(it.at) || 0));
      if (!byAt.has(at)) byAt.set(at, []);
      byAt.get(at).push(it);
    });
    const fig = (it) => {
      const url = "/api/body/img/" + encodeURIComponent(it.f);
      const safeUrl = esc(url);
      const w = Number(it.w) || 0;
      const h = Number(it.h) || 0;
      const size = w && h ? ` width="${w}" height="${h}"` : "";
      const cap = it.n || it.c
        ? `<figcaption>${esc(it.n || "")}${it.c
          ? `<span class="fig-credit">${esc(it.c)}</span>` : ""}</figcaption>`
        : "";
      return `<figure class="body-fig"><a href="${safeUrl}" target="_blank" rel="noreferrer">
        <img src="${safeUrl}" alt="${esc(it.n || "")}" loading="lazy"${size}></a>${cap}</figure>`;
    };
    let out = (byAt.get(0) || []).map(fig).join("");
    paras.forEach((para, i) => {
      if (para.text.trim()) {
        out += `<p data-a-start="${para.start}" data-a-end="${para.end}">${
          paragraphHtml(para, annotations)}</p>`;
      }
      out += (byAt.get(i + 1) || []).map(fig).join("");
    });
    return out;
  }

  function blockOfNode(node) {
    const el = node && node.nodeType === 1 ? node : node && node.parentElement;
    return el && el.closest ? el.closest("p[data-a-start]") : null;
  }

  function textUnits(node) {
    if (!node) return 0;
    if (node.nodeType === 3) return node.nodeValue.length;
    if (node.nodeName === "BR") return 1;
    let n = 0;
    node.childNodes.forEach((child) => { n += textUnits(child); });
    return n;
  }

  /* 选区的 DOM 坐标 → 段落内的正文坐标。BR 算一个换行，mark 只当透明容器。 */
  function offsetInBlock(root, node, offset) {
    if (!root || !node || !root.contains(node)) return null;
    let total = 0;
    let done = false;
    function visit(cur) {
      if (done) return;
      if (cur === node) {
        if (cur.nodeType === 3) {
          total += Math.min(offset, cur.nodeValue.length);
          done = true;
          return;
        }
        const kids = Array.from(cur.childNodes);
        for (let i = 0; i < Math.min(offset, kids.length); i++) total += textUnits(kids[i]);
        done = true;
        return;
      }
      if (cur.nodeType === 3) { total += cur.nodeValue.length; return; }
      if (cur.nodeName === "BR") { total += 1; return; }
      cur.childNodes.forEach((child) => { if (!done) visit(child); });
    }
    visit(root);
    return done ? total : null;
  }

  function annotationRailHtml(list) {
    if (!list.length) {
      return `<div class="ann-empty">
        <span class="ann-empty-mark" aria-hidden="true">“</span>
        <strong>还没有标注</strong>
        <span>选中正文任意一段，留下高亮或批注。</span>
      </div>`;
    }
    return list.map((a) => {
      const color = annColor(a.color);
      const label = ANNOTATION_COLORS[color].label;
      const stamp = fmtDate(a.updated_at || a.created_at);
      return `<article class="ann-card ann-${color}${a._orphan ? " orphan" : ""}"
          data-ann-card="${Number(a.id)}" tabindex="0">
        <div class="ann-card-head">
          <span class="ann-card-kind"><i aria-hidden="true"></i>${esc(label)}</span>
          <time>${esc(stamp)}</time>
        </div>
        <blockquote>${esc(a.quote || "")}</blockquote>
        ${a.note ? mdBody(a.note, "ann-card-note") : ""}
        <div class="ann-card-foot">
          <span>${a._orphan ? "原文有变，暂时无法定位" : (a.note ? "批注" : "高亮")}</span>
          <span class="ann-card-acts">
            ${a._orphan ? "" : `<button type="button" data-ann-locate>定位</button>`}
            <button type="button" data-ann-edit>编辑</button>
          </span>
        </div>
      </article>`;
    }).join("");
  }

  async function viewClip(cid) {
    // 路由守卫：只有当前确实停在这条详情页时才允许渲染，异步回来晚了的旧调用直接作废
    const hit = currentPath().match(/^\/c\/(\d+)$/);
    if (!hit || String(cid) !== hit[1]) return;
    const seq = routeSeq;
    detailAbort?.abort();
    detailAbort = new AbortController();
    const detailSignal = detailAbort.signal;
    document.body.classList.remove("reader-focus");
    const [data, boardData] = await Promise.all([api(`/api/clips/${cid}`), api("/api/boards")]);
    if (seq !== routeSeq) return;
    const c = data.clip;
    docClipTitle = c.title || "条目";
    document.title = `观澜 · ${docClipTitle}`;
    const outputs = data.outputs || [];
    const notes = data.notes || [];
    const rawAnnotations = data.annotations || [];
    const verdict = data.verdict;
    const byId = Object.fromEntries(outputs.map((o) => [String(o.id), o]));
    const boardOptions = [["", "未分类"], ...boardData.boards.map((b) => [String(b.id), b.name])]
      .map(([value, label]) => {
        const b = boardData.boards.find((x) => String(x.id) === value);
        const mark = b && b.kind === "archive" ? "▣ " : "";
        return `<option value="${esc(value)}"
        ${String(c.board_id || "") === value ? "selected" : ""}>${mark}${esc(label)}</option>`;
      }).join("");
    const archiveHere = boardData.boards.find((b) => b.kind === "archive");

    const tagChips = c.tags.map((t) => `
      <span class="chip mini tag-chip" data-tag-open="${esc(t)}" title="看同标签的条目">${esc(t)}<span
        class="tag-x" data-tag-del="${esc(t)}" title="去掉这个标签">×</span></span>`).join("");
    const quote = c.excerpt
      ? mdBody(c.excerpt, "quote") : "";
    const isFile = c.source_type === "file" && !!c.file_name;
    const fileKind = (c.file_name || "").split(".").pop().toUpperCase().slice(0, 5) || "文件";
    const clipKind = isFile ? fileKind : (c.url ? "网页" : "文字");
    // 只认 http(s)：别让存进来的 javascript:/data: 变成可点的跳板
    const safeOpenUrl = /^https?:\/\//i.test(String(c.url || "")) ? esc(c.url) : "";
    const askPlaceholder = isFile
      ? `直接问这份 ${fileKind}：提炼、找论据、换角度…`
      : "直接跟 AI 说：提炼三条 / 这条哪里站不住 / 换个立场写…";
    const directImage = isImageFileClip(c);
    // HTML 原件走 /html-reader.html：净化 + sandbox iframe 直读，不再触发下载。
    // 服务端 _clip_file 仍对 .html 返回 attachment（防同源 XSS），阅读器用
    // fetch 取文本绕开 Content-Disposition，?dl=1 仍在阅读器里保留下载入口。
    const isHtmlFile = isFile && /\.(html?|htm)$/i.test(String(c.file_name || ""));
    const fileBrief = isFile ? `
      <div class="file-brief">
        <div class="file-glyph" aria-hidden="true">${esc(fileKind)}</div>
        <div class="file-copy">
          <div class="file-name-row">
            <span class="file-name" title="${esc(c.file_name)}">${esc(c.file_name)}</span>
            <span class="file-direct">${directImage ? "可直接看" : (isHtmlFile ? "直接读" : "AI 直读")}</span>
          </div>
          <div class="file-meta">${esc(fileKind)} · ${esc(fmtSize(c.file_size || 0))} · 原件保存在本机</div>
          <div class="file-explain">${directImage
            ? "图片就在下面，点开可看原图；原件仍保存在本机"
            : (isHtmlFile
                ? "HTML 原件按原版式直接读，无需下载；脚本与外链已隔离，原件仍保存在本机"
                : (c.content_text
                    ? "正文已自动提取并整理好版式，展示在下方；原件仍保存在本机"
                    : `暂未提取到文字${c.fetch_error ? "（" + esc(c.fetch_error) + "）" : ""}，可点上方「提取正文」重试，或直接打开原件`))}</div>
        </div>
        <a class="btn small ghost file-open-btn" href="${isHtmlFile
            ? `/html-reader.html?clip=${c.id}` : `/api/clips/${c.id}/file`}"
          target="_blank" rel="noreferrer">${isHtmlFile ? "直接读" : "打开原件"} <span aria-hidden="true">↗</span></a>
      </div>` : "";
    const imagePanel = imageGallery(c, { detail: true });

    let annotations = prepareAnnotations(c.content_text, rawAnnotations);
    const readMinutes = Math.max(1, Math.round(String(c.content_text || "").length / 420));

    main.innerHTML = `
      <a class="back" href="#/">← 回收存</a>
      <div class="head clip-head">
        <div class="clip-heading">
          <div class="clip-kicker">${esc(clipKind)}${isFile ? " · 原件" : ""}</div>
          <h1 class="title-edit" id="clip-title" title="点一下改标题">${esc(c.title)}</h1>
        </div>
        <div class="meta">${esc([c.board_kind === "archive" ? `▣ ${c.board_name}`
          : (c.board_name || "未分类"), gradeName(c.grade), fmtDate(c.created_at),
          c.tier ? `${c.tier} ${TIER_NAME[c.tier] || ""}` : "未分级"]
          .filter(Boolean).join(" · "))}</div>
      </div>
      <div class="filters clip-tools">
        ${safeOpenUrl ? `<a class="btn small ghost" href="${safeOpenUrl}" target="_blank" rel="noreferrer">打开原链接</a>` : ""}
        ${(c.url || isFile) ? (() => {
          // 文件条目：后台提取中时不显示按钮，显示进度态
          if (isFile && c.fetch_status === "extracting") {
            return `<span class="btn small ghost" disabled style="cursor:wait">正在提取正文…</span>`;
          }
          // 已有正文：显示「重提正文」/「重抓正文」
          if (c.content_text) {
            return `<button class="btn small ghost" id="act-extract">${isFile ? "重提正文" : "重抓正文"}</button>`;
          }
          // 没有正文且不是提取中：显示「提取正文」/「抓正文」
          return `<button class="btn small ghost" id="act-extract">${isFile ? "提取正文" : "抓正文"}</button>`;
        })() : ""}
        <select id="act-board" class="compact-select" title="换个板块">
          ${boardOptions}</select>
        ${c.board_kind === "archive"
          ? `<button class="btn small ghost" id="act-arch-out" title="退回未分类，条目不动">移出资料库</button>`
          : (archiveHere ? `<button class="btn small ghost" id="act-arch-in"
              title="放进资料库：原件与权威原文的存放处">存进${esc(archiveHere.name)}</button>` : "")}
        <span class="f-label grade-label">材料级</span>
        ${gradeSegs(c.grade, { id: "act-grade", small: true })}
        <select id="act-tier" class="compact-select" title="信源分级">
          ${tierOptions(c.tier || "", "未分级")}</select>
        <span class="grow"></span>
        <button class="btn small ghost danger" id="act-del">删除</button>
      </div>
      ${c.grade_hint && c.grade_hint !== c.grade ? `<div class="filters hint-bar">
        <span class="f-label">AI 建议</span>
        <span class="g-badge g-${esc(c.grade_hint)}">${esc(GRADE_NAME[c.grade_hint] || c.grade_hint)}</span>
        <span class="faint">${esc(c.grade_hint_reason || "")}</span>
        <span class="grow"></span>
        <span class="cc-act" id="hint-take">采纳</span>
        <span class="cc-act" id="hint-drop">不要</span>
      </div>` : ""}

      ${fileBrief}
      ${imagePanel}

      <div class="panel ask-panel ai-desk"><div class="panel-body">
        <div class="ai-desk-head">
          <div>
            <div class="ai-desk-title">${isFile ? "问这份原件" : "问这条材料"}</div>
            <div class="ai-desk-sub">${isFile
              ? "AI 会依据已提取的正文与原件回答"
              : "回答只依据这一条，不拿别处的知识替你补结论"}</div>
          </div>
          <span class="ai-scope">仅依据本条</span>
        </div>
        <div class="instruct-row">
          <input type="text" id="ai-instruct" maxlength="200" placeholder="${esc(askPlaceholder)}">
          <button class="btn" id="ai-send">送出</button>
        </div>
        <div class="presets">
          <span class="f-label">常用</span>
          ${PRESETS.map((p) => `<span class="chip" data-preset="${esc(p)}">${esc(p)}</span>`).join("")}
          <span class="chip" data-deep="1">结构化深读</span>
        </div>
        <div class="ai-msg" id="ai-msg" role="status"><span class="ai-ready-dot"></span>${isFile
          ? (c.content_text
              ? "正文已入库，随时可问；原件仍保存在本机。"
              : (c.fetch_status === "extracting"
                  ? "正在后台提取正文，完成后自动显示；原件仍保存在本机。"
                  : "正文还没提取，可点上方「提取正文」重试；原件仍保存在本机。"))
          : "可以直接下指令，也可以点一个常用动作。"}</div>
      </div></div>

      <div id="research">${outputs.map(researchCard).join("")}</div>

      ${c.content_text ? `<div class="panel reader-panel" id="body-panel">
          <div class="reader-top">
            <div class="reader-heading">
              <span class="reader-kicker">READ</span>
              <span class="reader-title">正文</span>
              <span class="reader-sub">${c.content_chars.toLocaleString()} 字 · 约 ${readMinutes} 分钟</span>
            </div>
            <div class="reader-tools">
              <span class="reader-tip"><i aria-hidden="true"></i>选中文字即可标注</span>
              <div class="reader-zoom" aria-label="正文字号">
                <button type="button" id="reader-smaller" title="缩小字号" aria-label="缩小字号">A−</button>
                <button type="button" id="reader-reset" title="恢复默认字号">100%</button>
                <button type="button" id="reader-larger" title="放大字号" aria-label="放大字号">A＋</button>
              </div>
              <button type="button" class="reader-focus-btn" id="reader-focus">专注阅读</button>
            </div>
          </div>
          <div class="reader-layout">
            <section class="reader-pane" aria-label="正文">
              <div class="reader-scroll" id="reader-scroll" tabindex="0">
                <div class="reader" id="body-text">${readerHtml(
                  c.content_text, c.body_images, annotations)}</div>
              </div>
            </section>
            <aside class="ann-rail" id="ann-rail" aria-label="正文批注">
              <div class="ann-rail-head">
                <div>
                  <span class="ann-rail-kicker">MARGINALIA</span>
                  <strong>批注</strong>
                </div>
                <span class="ann-rail-count" id="ann-count">${annotations.length} 条</span>
              </div>
              <div class="ann-list" id="ann-list">${annotationRailHtml(annotations)}</div>
            </aside>
          </div>
          <div class="reader-progress" aria-hidden="true"><i id="reader-progress"></i></div>
        </div>
        <div class="ann-bubble" id="ann-bubble" role="toolbar" aria-label="标注选中文字" hidden>
          <span class="ann-bubble-label">标注</span>
          <span class="ann-swatches">${Object.entries(ANNOTATION_COLORS).map(([key, item]) => `
            <button type="button" class="ann-swatch ann-${key}" data-ann-color="${key}"
              title="${esc(item.label)} · ${esc(item.hint)}" aria-label="标为${esc(item.label)}"></button>`).join("")}
          </span>
          <span class="ann-bubble-sep" aria-hidden="true"></span>
          <button type="button" class="ann-comment-btn" id="ann-comment">＋ 批注</button>
        </div>
        <div class="ann-composer" id="ann-composer" role="dialog" aria-modal="true"
          aria-labelledby="ann-composer-title" hidden>
          <div class="ann-composer-head">
            <div>
              <span class="ann-composer-kicker">ANNOTATION</span>
              <strong id="ann-composer-title">写一条批注</strong>
            </div>
            <button type="button" class="ann-composer-close" id="ann-composer-close"
              aria-label="关闭">×</button>
          </div>
          <blockquote class="ann-composer-quote" id="ann-composer-quote"></blockquote>
          <div class="ann-composer-colors" id="ann-composer-colors" role="radiogroup"
            aria-label="标注类型">
            ${Object.entries(ANNOTATION_COLORS).map(([key, item]) => `
              <button type="button" class="ann-color-option ann-${key}" data-ann-color="${key}"
                role="radio" aria-checked="false"><i aria-hidden="true"></i>${esc(item.label)}</button>`).join("")}
          </div>
          <textarea id="ann-note" rows="3" maxlength="4000" data-md-preview
            placeholder="写下你的批注；留空就是一条高亮。支持 Markdown。"></textarea>
          <div class="ann-composer-foot">
            <span class="ann-note-count" id="ann-note-count">0 / 4000</span>
            <span class="grow"></span>
            <button type="button" class="btn small ghost danger" id="ann-delete" hidden>删除</button>
            <button type="button" class="btn small ghost" id="ann-cancel">取消</button>
            <button type="button" class="btn small" id="ann-save">保存</button>
          </div>
        </div>` : (c.url && !c.content_text ? `<div class="panel source-empty">
          <div class="panel-body">
            <span class="faint">这条链接只存了标题和地址。AI 目前只能读到标题；点上方「抓正文」，再问全文。</span>
          </div></div>` : (isFile && !c.content_text ? `<div class="panel source-empty">
          <div class="panel-body">
            <span class="faint">这份原件暂时没读到文字${c.fetch_error ? "（" + esc(c.fetch_error) + "）" : ""}。可点上方「提取正文」重试，或直接打开原件核对。</span>
          </div></div>` : ""))}
      ${c.summary ? `<div class="panel" id="sum-panel"><div class="panel-head">
          <div class="panel-title">AI 简介</div>
          <div class="panel-sub"><span class="cc-act" id="sum-edit">改一下</span></div></div>
        <div class="panel-body" id="sum-body">${mdBody(c.summary, "ai-box")}</div></div>` : ""}
      ${quote ? panel("我的话", "", quote) : ""}
      <div class="panel verdict-panel" id="verdict-panel">
        <div class="panel-head">
          <div class="panel-title">我的判断</div>
          <div class="panel-sub">${verdict ? `${CONF_LABEL[verdict.confidence] || "中"}置信` : "还没写"}</div>
        </div>
        <div class="panel-body">
          ${verdict && verdict.text
            ? mdBody(verdict.text, "verdict-body")
            : `<div class="verdict-prompt">这条材料说明了什么？写一句就够。</div>`}
          ${verdict && verdict.refs.length ? `<div class="verdict-refs"><span class="f-label">反方</span>
            ${verdict.refs.map((r) => `<a class="chip mini" href="#/c/${r.id}">${esc(r.title)}<span
              class="tag-x" data-ref-del="${r.id}" title="移掉">×</span></a>`).join("")}</div>` : ""}
          <div class="verdict-acts">
            <button class="btn small ghost" id="verdict-edit">${verdict ? "改判断" : "写判断"}</button>
            <button class="btn small ghost" id="verdict-ref">加反方材料</button>
            ${verdict ? `<button class="btn small ghost danger" id="verdict-drop">删掉</button>` : ""}
          </div>
          <div id="verdict-editor" hidden>
            <textarea id="verdict-text" rows="3" maxlength="500" data-md-preview
              placeholder="我认为这条材料说明……支持 Markdown。">${esc(verdict ? verdict.text : "")}</textarea>
            <div class="verdict-acts">
              <span class="f-label">置信</span>
              ${["high", "mid", "low"].map((k) => `<span class="chip conf-chip
                ${verdict && verdict.confidence === k ? "on" : ""}" data-conf="${k}">${CONF_LABEL[k]}</span>`).join("")}
              <span class="grow"></span>
              <button class="btn small ghost" id="verdict-cancel">取消</button>
              <button class="btn small" id="verdict-save">保存</button>
            </div>
          </div>
          <div id="verdict-picker" hidden>
            <input type="text" id="verdict-q" placeholder="搜一条材料，点一下当成反方">
            <div id="verdict-hits" class="verdict-hits"></div>
          </div>
        </div>
      </div>

      <div class="tag-edit" id="tag-edit">
        <span class="f-label">标签</span>
        <span class="tag-list" id="tag-list">${tagChips || `<span class="faint tag-empty">还没有标签</span>`}</span>
        <button class="tag-add" id="tag-add" type="button">＋ 贴标签</button>
        <input type="text" id="tag-input" class="tag-input" hidden maxlength="80"
          placeholder="回车确认，逗号可分隔多个" spellcheck="false">
      </div>

      <div class="notes-wrap" id="notes-wrap">
        <div class="notes-head">
          <span class="notes-title">随手记</span>
          <span class="notes-hint">${notes.length ? notes.length + " 条" : "写点什么，图也能贴"}</span>
          <span class="grow"></span>
          <span class="cc-act" id="note-write">＋ 写一条</span>
        </div>
        <div class="note-list" id="note-list">${notes.map(noteCard).join("")}</div>
        <div class="note-editor" id="note-editor" hidden>
          <textarea id="note-text" rows="3" data-md-preview spellcheck="false"
            placeholder="直接写，支持 Markdown。Ctrl+V 贴图，或者点下面「添加附件」挂文件。"></textarea>
          <div class="note-pending" id="note-pending"></div>
          <div class="note-acts">
            <label class="btn small ghost file-btn">添加附件
              <input type="file" id="note-files" multiple></label>
            <button class="btn small ghost" id="note-rec" type="button">录音</button>
            <span class="faint note-msg" id="note-msg"></span>
            <span class="grow"></span>
            <button class="btn small ghost" id="note-cancel">取消</button>
            <button class="btn small" id="note-save">保存</button>
          </div>
        </div>
      </div>
      <button class="fab" id="note-add" title="加一条随手记" aria-label="加一条随手记"></button>`;

    /* — 标题：点一下就地改 — */

    function bindTitleEdit() {
      const el = $("#clip-title");
      if (!el) return;
      el.addEventListener("click", () => {
        const original = c.title;
        const input = document.createElement("input");
        input.type = "text";
        input.className = "title-input";
        input.value = original;
        input.maxLength = 200;
        input.spellcheck = false;
        el.replaceWith(input);
        input.focus();
        input.select();
        let done = false;

        const restore = (value) => {
          const h = document.createElement("h1");
          h.className = "title-edit";
          h.id = "clip-title";
          h.title = "点一下改标题";
          h.textContent = value;
          input.replaceWith(h);
          bindTitleEdit();
        };

        const cancel = () => {
          if (done) return;
          done = true;
          restore(original);
        };

        const save = async () => {
          if (done) return;
          const value = input.value.trim();
          if (!value) { toast("标题不能空着", "warn"); input.focus(); return; }
          if (value === original) { cancel(); return; }
          done = true;
          input.disabled = true;
          try {
            await post(`/api/clips/${c.id}`, { title: value });
            c.title = value;
            toast("标题改好了");
            viewClip(c.id);
          } catch (e) {
            done = false;
            input.disabled = false;
            toast(e.message, "err");
            input.focus();
          }
        };

        // 回车存、Esc 退、点别处也算存（空着就退回原样）
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        });
        // 点别处：有内容就存，空着直接退回原样（不写库、不留空标题）
        input.addEventListener("blur", () => {
          if (!input.value.trim()) { cancel(); return; }
          save();
        });
      });
    }

    bindTitleEdit();
    bindMdPreview(main);

    /* — 正文阅读：标注、批注、字号与专注模式 — */

    (function bindReaderAnnotations() {
      const article = $("#body-text");
      if (!article) return;
      const panel = $("#body-panel");
      const scroll = $("#reader-scroll");
      const rail = $("#ann-list");
      const counter = $("#ann-count");
      const bubble = $("#ann-bubble");
      const composer = $("#ann-composer");
      const noteInput = $("#ann-note");
      const noteCount = $("#ann-note-count");
      const source = String(c.content_text || "");
      const zoomKey = "gl_reader_zoom";
      const zoomSteps = [0.9, 1, 1.1, 1.2, 1.3];
      let zoom = 1;
      try { zoom = Number(localStorage.getItem(zoomKey)) || 1; } catch (_) { /* 隐私模式 */ }
      if (!zoomSteps.includes(zoom)) zoom = 1;

      let pendingSelection = null;
      let compose = null;
      let selectionTimer = 0;
      let bubbleAbort = null;
      let focusOrigin = 0;

      const selectionOverlap = (start, end) => annotations.find(
        (a) => !a._orphan && start < a._end && end > a._start) || null;

      function applyZoom() {
        panel.style.setProperty("--reader-zoom", String(zoom));
        const label = $("#reader-reset");
        if (label) label.textContent = `${Math.round(zoom * 100)}%`;
        const smaller = $("#reader-smaller");
        const larger = $("#reader-larger");
        if (smaller) smaller.disabled = zoom <= zoomSteps[0];
        if (larger) larger.disabled = zoom >= zoomSteps[zoomSteps.length - 1];
        try { localStorage.setItem(zoomKey, String(zoom)); } catch (_) { /* 忽略 */ }
      }

      function setZoom(next) {
        const i = Math.max(0, zoomSteps.indexOf(zoom));
        zoom = zoomSteps[Math.max(0, Math.min(zoomSteps.length - 1, i + next))];
        applyZoom();
      }

      function updateProgress() {
        const bar = $("#reader-progress");
        if (!bar || !scroll) return;
        const max = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
        const pct = max <= 1 ? 100 : Math.max(0, Math.min(100, scroll.scrollTop / max * 100));
        bar.style.width = `${pct.toFixed(2)}%`;
      }

      function renderRail() {
        if (counter) counter.textContent = `${annotations.length} 条`;
        if (rail) rail.innerHTML = annotationRailHtml(annotations);
      }

      function renderArticle() {
        const top = scroll ? scroll.scrollTop : 0;
        article.innerHTML = readerHtml(source, c.body_images, annotations);
        if (scroll) scroll.scrollTop = top;
      }

      function renderReader() {
        renderArticle();
        renderRail();
        updateProgress();
      }

      function setActiveAnnotation(id) {
        $$(".ann-card", rail).forEach((el) => {
          el.classList.toggle("is-active", String(id) === el.dataset.annCard);
        });
        $$("mark.ann-mark", article).forEach((el) => {
          el.classList.toggle("ann-selected", String(id) === el.dataset.annId);
        });
      }

      function locateAnnotation(id, openEditor = false) {
        const mark = $(`mark.ann-mark[data-ann-id="${Number(id)}"]`, article);
        if (!mark) {
          if (openEditor) {
            const found = annotations.find((a) => Number(a.id) === Number(id));
            if (found) openComposer(found);
          } else {
            toast("原文有变化，这条标注暂时找不到位置", "warn");
          }
          return;
        }
        const sr = scroll.getBoundingClientRect();
        const mr = mark.getBoundingClientRect();
        scroll.scrollTo({
          top: Math.max(0, scroll.scrollTop + mr.top - sr.top - scroll.clientHeight / 3),
          behavior: "smooth",
        });
        setActiveAnnotation(id);
        mark.classList.remove("ann-pulse");
        void mark.offsetWidth;
        mark.classList.add("ann-pulse");
        setTimeout(() => mark.classList.remove("ann-pulse"), 900);
        if (openEditor) setTimeout(() => {
          const found = annotations.find((a) => Number(a.id) === Number(id));
          if (found) openComposer(found);
        }, 160);
      }

      function hideBubble() {
        if (!bubble) return;
        bubble.hidden = true;
        pendingSelection = null;
        bubbleAbort?.abort();
        bubbleAbort = null;
      }

      function placeBubble(rect) {
        if (!bubble) return;
        const br = bubble.getBoundingClientRect();
        const left = Math.max(8, Math.min(
          window.innerWidth - br.width - 8, rect.left + rect.width / 2 - br.width / 2));
        let top = rect.top - br.height - 12;
        if (top < 8) top = rect.bottom + 12;
        bubble.style.left = `${left}px`;
        bubble.style.top = `${Math.max(8, top)}px`;
      }

      function showBubble(sel) {
        if (!bubble || !sel) return hideBubble();
        pendingSelection = sel;
        bubble.hidden = false;
        requestAnimationFrame(() => placeBubble(sel.rect));
        bubbleAbort?.abort();
        bubbleAbort = new AbortController();
        const signal = bubbleAbort.signal;
        document.addEventListener("pointerdown", (ev) => {
          if (!bubble.contains(ev.target)) hideBubble();
        }, { capture: true, signal });
        scroll.addEventListener("scroll", hideBubble, { passive: true, signal });
        window.addEventListener("resize", hideBubble, { signal });
      }

      function readSelection() {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
        const range = sel.getRangeAt(0);
        if (!article.contains(range.commonAncestorContainer)) return null;
        const startBlock = blockOfNode(range.startContainer);
        const endBlock = blockOfNode(range.endContainer);
        if (!startBlock || !endBlock) return null;
        const localStart = offsetInBlock(startBlock, range.startContainer, range.startOffset);
        const localEnd = offsetInBlock(endBlock, range.endContainer, range.endOffset);
        if (localStart === null || localEnd === null) return null;
        let start = Number(startBlock.dataset.aStart) + localStart;
        let end = Number(endBlock.dataset.aStart) + localEnd;
        if (end < start) [start, end] = [end, start];
        let raw = source.slice(start, end);
        const lead = (raw.match(/^\s+/) || [""])[0].length;
        const tail = (raw.match(/\s+$/) || [""])[0].length;
        start += lead;
        end = Math.max(start, end - tail);
        const quote = source.slice(start, end);
        if (quote.length < 2) return null;
        const rect = range.getBoundingClientRect();
        if (!rect || (!rect.width && !rect.height)) return null;
        return {
          start, end, quote,
          prefix: source.slice(Math.max(0, start - 48), start),
          suffix: source.slice(end, Math.min(source.length, end + 48)),
          rect,
        };
      }

      function setComposerColor(color) {
        if (!composer) return;
        const safe = annColor(color);
        if (compose) compose.color = safe;
        $$("[data-ann-color]", composer).forEach((el) => {
          const on = el.dataset.annColor === safe;
          el.classList.toggle("on", on);
          if (el.getAttribute("role") === "radio") el.setAttribute("aria-checked", on ? "true" : "false");
        });
        composer.dataset.color = safe;
      }

      function closeComposer() {
        if (!composer) return;
        composer.hidden = true;
        compose = null;
        noteInput.value = "";
        noteCount.textContent = "0 / 4000";
        setActiveAnnotation(null);
      }

      function openComposer(annotation = null, selected = null) {
        if (!composer) return;
        hideBubble();
        compose = annotation
          ? { mode: "edit", annotation, color: annColor(annotation.color), quote: annotation.quote }
          : { mode: "create", selection: selected, color: "amber", quote: selected && selected.quote };
        if (!compose.quote) return;
        $("#ann-composer-title").textContent = annotation ? "编辑批注" : "写一条批注";
        $("#ann-composer-quote").textContent = compose.quote;
        noteInput.value = annotation ? (annotation.note || "") : "";
        noteCount.textContent = `${noteInput.value.length} / 4000`;
        $("#ann-delete").hidden = !annotation;
        setComposerColor(compose.color);
        composer.hidden = false;
        setActiveAnnotation(annotation ? annotation.id : null);
        requestAnimationFrame(() => noteInput.focus());
      }

      function sortAnnotations() {
        annotations.sort((a, b) => (a._start - b._start) || (Number(a.id) - Number(b.id)));
      }

      async function createAnnotation(selection, color, button) {
        if (!selection) return;
        const overlap = selectionOverlap(selection.start, selection.end);
        if (overlap) {
          hideBubble();
          openComposer(overlap);
          toast("这段已经有标注，已打开那一条", "warn");
          return;
        }
        if (selection.quote.length > 8000) {
          toast("一次最多标 8000 字，长段落可以拆成几处", "warn");
          return;
        }
        await withBusy(button, async () => {
          try {
            const r = await post(`/api/annotations/${c.id}`, {
              start: selection.start,
              end: selection.end,
              quote: selection.quote,
              color,
              note: "",
              prefix: selection.prefix,
              suffix: selection.suffix,
            });
            const added = prepareAnnotations(source, [r.annotation])[0];
            annotations.push(added);
            sortAnnotations();
            c.annotations = annotations;
            hideBubble();
            renderReader();
            requestAnimationFrame(() => locateAnnotation(added.id));
            toast(`标为「${ANNOTATION_COLORS[annColor(color)].label}」`);
          } catch (e) {
            toast(e.message, "err");
          }
        });
      }

      async function saveComposer(button) {
        if (!compose) return;
        const note = noteInput.value.trim();
        const color = annColor(compose.color);
        await withBusy(button, async () => {
          try {
            if (compose.mode === "edit") {
              const id = compose.annotation.id;
              const r = await post(`/api/annotations/${id}/update`, { note, color });
              const at = annotations.findIndex((a) => Number(a.id) === Number(id));
              if (at >= 0) {
                annotations[at] = {
                  ...annotations[at], ...r.annotation, color,
                  _start: annotations[at]._start, _end: annotations[at]._end,
                  _orphan: annotations[at]._orphan,
                };
              }
              closeComposer();
              c.annotations = annotations;
              renderReader();
              requestAnimationFrame(() => locateAnnotation(id));
              toast(note ? "批注改好了" : "留下高亮，批注已清空");
            } else {
              const selection = compose.selection;
              if (!selection) return;
              if (selection.quote.length > 8000) {
                toast("一次最多批注 8000 字", "warn");
                return;
              }
              const r = await post(`/api/annotations/${c.id}`, {
                start: selection.start,
                end: selection.end,
                quote: selection.quote,
                color,
                note,
                prefix: selection.prefix,
                suffix: selection.suffix,
              });
              const added = prepareAnnotations(source, [r.annotation])[0];
              annotations.push(added);
              sortAnnotations();
              c.annotations = annotations;
              closeComposer();
              renderReader();
              requestAnimationFrame(() => locateAnnotation(added.id));
              toast(note ? "批注已记下" : "已标亮");
            }
          } catch (e) {
            toast(e.message, "err");
          }
        });
      }

      async function deleteComposer(button) {
        if (!compose || compose.mode !== "edit") return;
        const id = Number(compose.annotation.id);
        if (!(await glConfirm("删掉这条批注？原文不会改。", { danger: true, confirmText: "删除" }))) return;
        await withBusy(button, async () => {
          try {
            await post(`/api/annotations/${id}/delete`, {});
            annotations = annotations.filter((a) => Number(a.id) !== id);
            c.annotations = annotations;
            closeComposer();
            renderReader();
            toast("删掉了");
          } catch (e) {
            toast(e.message, "err");
          }
        });
      }

      applyZoom();
      updateProgress();

      $("#reader-smaller")?.addEventListener("click", () => setZoom(-1));
      $("#reader-larger")?.addEventListener("click", () => setZoom(1));
      $("#reader-reset")?.addEventListener("click", () => { zoom = 1; applyZoom(); });
      scroll?.addEventListener("scroll", updateProgress, { passive: true });

      const focusBtn = $("#reader-focus");
      function setFocus(on) {
        panel.classList.toggle("is-focus", on);
        document.body.classList.toggle("reader-focus", on);
        focusBtn.textContent = on ? "退出专注" : "专注阅读";
        if (on) {
          focusOrigin = window.scrollY;
          requestAnimationFrame(() => { updateProgress(); scroll?.focus?.({ preventScroll: true }); });
        } else {
          requestAnimationFrame(() => window.scrollTo(0, focusOrigin));
        }
      }
      focusBtn?.addEventListener("click", () => setFocus(!panel.classList.contains("is-focus")));

      article.addEventListener("click", (ev) => {
        const mark = ev.target.closest("mark.ann-mark");
        if (!mark) return;
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed) return;
        ev.preventDefault();
        const found = annotations.find((a) => Number(a.id) === Number(mark.dataset.annId));
        if (found) openComposer(found);
      }, { signal: detailSignal });

      article.addEventListener("keydown", (ev) => {
        const mark = ev.target.closest("mark.ann-mark");
        if (!mark || !["Enter", " "].includes(ev.key)) return;
        ev.preventDefault();
        const found = annotations.find((a) => Number(a.id) === Number(mark.dataset.annId));
        if (found) openComposer(found);
      }, { signal: detailSignal });

      rail?.addEventListener("click", (ev) => {
        const card = ev.target.closest("[data-ann-card]");
        if (!card) return;
        const id = Number(card.dataset.annCard);
        if (ev.target.closest("[data-ann-edit]")) {
          const found = annotations.find((a) => Number(a.id) === id);
          if (found) openComposer(found);
          return;
        }
        locateAnnotation(id);
      }, { signal: detailSignal });

      rail?.addEventListener("keydown", (ev) => {
        const card = ev.target.closest("[data-ann-card]");
        if (!card || !["Enter", " "].includes(ev.key)) return;
        ev.preventDefault();
        locateAnnotation(Number(card.dataset.annCard));
      }, { signal: detailSignal });

      bubble?.addEventListener("mousedown", (ev) => {
        if (ev.target.closest("button")) ev.preventDefault();
      }, { signal: detailSignal });

      $$("[data-ann-color]", bubble).forEach((btn) => btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        const chosen = pendingSelection;
        hideBubble();
        if (chosen) createAnnotation(chosen, btn.dataset.annColor, btn);
      }, { signal: detailSignal }));

      $("#ann-comment")?.addEventListener("click", () => {
        const chosen = pendingSelection;
        hideBubble();
        if (chosen) openComposer(null, chosen);
      }, { signal: detailSignal });

      $$("[data-ann-color]", composer).forEach((btn) => btn.addEventListener(
        "click", () => setComposerColor(btn.dataset.annColor), { signal: detailSignal }));
      $("#ann-composer-close")?.addEventListener("click", closeComposer, { signal: detailSignal });
      $("#ann-cancel")?.addEventListener("click", closeComposer, { signal: detailSignal });
      $("#ann-save")?.addEventListener("click", (ev) => saveComposer(ev.currentTarget));
      $("#ann-delete")?.addEventListener("click", (ev) => deleteComposer(ev.currentTarget));
      noteInput?.addEventListener("input", () => {
        noteCount.textContent = `${noteInput.value.length} / 4000`;
      }, { signal: detailSignal });

      document.addEventListener("selectionchange", () => {
        clearTimeout(selectionTimer);
        selectionTimer = setTimeout(() => {
          if (!composer || !composer.hidden) return;
          const sel = readSelection();
          if (sel) showBubble(sel);
          else if (bubble && !bubble.hidden && !bubble.matches(":hover")) hideBubble();
        }, 160);
      }, { signal: detailSignal });

      document.addEventListener("keydown", (ev) => {
        if (ev.key !== "Escape") return;
        if (composer && !composer.hidden) closeComposer();
        else if (bubble && !bubble.hidden) hideBubble();
        else if (panel.classList.contains("is-focus")) setFocus(false);
      }, { signal: detailSignal });
    })();

    /* — 抓正文：修编码、去样板，必要时让模型只做清洗 — */

    $("#act-extract")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const btn = ev.currentTarget;
      btn.textContent = "抓取中…";
      const msg = $("#ai-msg");
      if (msg) msg.textContent = isFile ? "正在读取原件并整理版式…" : "正在读取网页…";
      // 被反爬拦住、或者正文要等脚本拉回来的页面，等半分钟很正常，得让人知道还在动
      const slow = setTimeout(() => {
        if (msg) msg.textContent = "这页要浏览器渲染，慢一点，别关窗口…";
      }, 3500);
      const slower = setTimeout(() => {
        if (msg) msg.textContent = "正文是脚本后加载的，还在等它长出来…";
      }, 18000);
      try {
        const r = await post(`/api/clips/${c.id}/extract`, {});
        const pics = r.images ? ` · 配图 ${r.images} 张` : "";
        // 后端会把「为什么 0 字」「只抽了前半本」写在 parse_message 里，
        // 以前这里整个丢掉，只弹一个「规则提取 0 字」，用户无从下手。
        const note = String(r.parse_message || "").trim();
        const head = `${r.ai ? "AI 清洗后" : "规则提取"} ${r.chars} 字${pics}`;
        toast(note ? `${head} · ${note}` : head,
          r.chars < 200 ? (note ? "err" : "warn") : "ok");
        if (note && r.chars === 0 && msg) {
          msg.innerHTML = `<span class="err-text">${esc(note)}</span>`;
        }
        viewClip(c.id);
      } catch (e) {
        if (msg) msg.innerHTML = `<span class="err-text">${esc(e.message)}</span>`;
        toast(e.message, "err");
      } finally {
        clearTimeout(slow);
        clearTimeout(slower);
      }
    }));

    /* — 标签：自己贴、自己撕 — */

    const tagInput = $("#tag-input");
    const tagAdd = $("#tag-add");

    async function saveTags(next) {
      try {
        await post(`/api/clips/${c.id}`, { tags: next });
        toast("标签改好了");
        viewClip(c.id);
      } catch (e) {
        toast(e.message, "err");
      }
    }

    function openTagInput() {
      if (!tagInput || !tagAdd) return;
      tagInput.hidden = false;
      tagAdd.hidden = true;
      tagInput.focus();
    }

    function closeTagInput() {
      if (!tagInput || !tagAdd) return;
      tagInput.hidden = true;
      tagInput.value = "";
      tagAdd.hidden = false;
    }

    async function commitTags(raw) {
      const parts = String(raw || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);
      if (!parts.length) { closeTagInput(); return; }
      const next = [...c.tags];
      let added = 0;
      for (const part of parts) {
        const t = part.slice(0, 40);
        if (!next.some((x) => x.toLowerCase() === t.toLowerCase())) { next.push(t); added++; }
      }
      if (!added) { closeTagInput(); toast("这个标签已经有了", "warn"); return; }
      await saveTags(next);
    }

    tagAdd?.addEventListener("click", openTagInput);
    tagInput?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitTags(tagInput.value); }
      else if (e.key === "Escape") { e.preventDefault(); closeTagInput(); }
    });
    tagInput?.addEventListener("blur", () => { if (!tagInput.value.trim()) closeTagInput(); });

    $$("[data-tag-del]").forEach((el) => el.addEventListener("click", async (e) => {
      e.stopPropagation();
      const label = el.dataset.tagDel;
      await saveTags(c.tags.filter((x) => x !== label));
    }));

    // 点标签本身 = 去列表按这个标签看
    $$("[data-tag-open]").forEach((el) => el.addEventListener("click", () => {
      filter.tag = el.dataset.tagOpen;
      filter.q = "";
      filter.offset = 0;
      location.hash = "#/";
    }));

    /* — 我的判断：一句话 + 置信 + 反方 — */

    let vConf = verdict ? verdict.confidence : "mid";
    let vRefs = verdict ? verdict.refs.map((r) => ({ id: r.id, title: r.title })) : [];

    async function saveVerdict(text) {
      const payload = {
        text: String(text || "").trim(),
        confidence: vConf,
        refs: vRefs.map((r) => ({ id: r.id })),
      };
      const r = await post(`/api/clips/${c.id}/verdict`, payload);
      toast(r.verdict ? "判断存下了" : "判断清空了");
      viewClip(c.id);
    }

    $("#verdict-edit")?.addEventListener("click", () => {
      const box = $("#verdict-editor");
      box.hidden = false;
      const ta = $("#verdict-text");
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    });
    $("#verdict-cancel")?.addEventListener("click", () => { $("#verdict-editor").hidden = true; });
    $$(".conf-chip").forEach((el) => el.addEventListener("click", () => {
      vConf = el.dataset.conf;
      $$(".conf-chip").forEach((x) => x.classList.toggle("on", x === el));
    }));
    $("#verdict-save")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try { await saveVerdict($("#verdict-text").value); }
      catch (e) { toast(e.message, "err"); }
    }));
    $("#verdict-drop")?.addEventListener("click", async () => {
      if (!(await glConfirm("删掉这条判断？", { danger: true, confirmText: "删除" }))) return;
      try { await post(`/api/clips/${c.id}/verdict/delete`, {}); toast("删掉了"); viewClip(c.id); }
      catch (e) { toast(e.message, "err"); }
    });
    $("#verdict-ref")?.addEventListener("click", () => {
      const picker = $("#verdict-picker");
      picker.hidden = !picker.hidden;
      if (!picker.hidden) $("#verdict-q").focus();
    });

    let pickTimer = null;
    let pickSeq = 0;
    $("#verdict-q")?.addEventListener("input", (ev) => {
      clearTimeout(pickTimer);
      const q = ev.currentTarget.value.trim();
      const host = $("#verdict-hits");
      if (!q) { host.innerHTML = ""; return; }
      pickTimer = setTimeout(async () => {
        const token = ++pickSeq;
        try {
          const r = await api(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
          // 后发的请求已经出去了：这次的结果作废，别把新的盖掉
          if (token !== pickSeq || !host.isConnected) return;
          const usable = r.results.filter((x) => x.id !== c.id && !vRefs.some((v) => v.id === x.id));
          host.innerHTML = usable.length
            ? usable.map((x) => `<div class="pick-hit" data-pick="${x.id}">${esc(x.title)}</div>`).join("")
            : `<div class="faint">没有别的材料了</div>`;
          $$("[data-pick]", host).forEach((el) => el.addEventListener("click", async () => {
            vRefs.push({ id: Number(el.dataset.pick), title: el.textContent });
            try { await saveVerdict($("#verdict-text").value); }
            catch (err) { toast(err.message, "err"); }
          }));
        } catch (err) { toast(err.message, "err"); }
      }, 250);
    });
    $$("[data-ref-del]").forEach((el) => el.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      vRefs = vRefs.filter((r) => String(r.id) !== el.dataset.refDel);
      try { await saveVerdict($("#verdict-text")?.value || ""); }
      catch (err) { toast(err.message, "err"); }
    }));

    /* — 随手记：右下角那个 + — */

    let pending = [];        // 已经传到服务端的图片
    let editing = null;      // 正在改的那条（null = 新建）

    function renderPending() {
      const host = $("#note-pending");
      if (!host) return;
      host.innerHTML = pending.map((p) => {
        if (p.kind === "audio") {
          return `<span class="note-audio">
            <audio controls preload="metadata" src="${p.url}"></audio>
            <span class="note-thumb-x" data-drop-img="${esc(p.file)}" title="移掉这段">×</span>
          </span>`;
        }
        if (p.kind === "file") {
          return `<span class="note-file pending">
            <span class="note-file-ext">${esc((p.name.split(".").pop() || "文件").slice(0, 4).toUpperCase())}</span>
            <span class="note-file-name">${esc(p.name)}</span>
            <span class="note-file-size">${esc(fmtSize(p.size || 0))}</span>
            <span class="note-thumb-x inline" data-drop-img="${esc(p.file)}" title="移掉">×</span>
          </span>`;
        }
        return `<span class="note-thumb">
          <img src="${p.url}" alt="">
          <span class="note-thumb-x" data-drop-img="${esc(p.file)}" title="移掉这张">×</span>
        </span>`;
      }).join("");
      $$("[data-drop-img]").forEach((el) => el.addEventListener("click", () => {
        pending = pending.filter((x) => x.file !== el.dataset.dropImg);
        renderPending();
      }));
    }

    function openComposer(note) {
      editing = note || null;
      const restore = (list, kind) => (list || []).map((it) => ({
        file: it.file, name: it.name || it.file, size: it.size || 0, kind,
        url: "/api/notes/img/" + encodeURIComponent(it.file),
      }));
      pending = [
        ...restore(note && note.images, "image"),
        ...restore(note && note.audios, "audio"),
        ...restore(note && note.files, "file"),
      ];
      const box = $("#note-editor");
      if (!box) return;
      box.hidden = false;
      $("#note-text").value = (note && note.text) || "";
      $("#note-save").textContent = note ? "保存修改" : "保存";
      $("#note-msg").textContent = "";
      renderPending();
      box.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimeout(() => $("#note-text").focus(), 80);
    }

    function closeComposer() {
      editing = null;
      pending = [];
      const box = $("#note-editor");
      if (!box) return;
      box.hidden = true;
      $("#note-text").value = "";
      renderPending();
    }

    async function uploadNoteMedia(file, fallbackName) {
      const q = new URLSearchParams({ name: file.name || fallbackName || "attachment" });
      const res = await fetch("/api/notes/media?" + q.toString(), {
        method: "POST",
        body: file,
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-GL-Client": clientId,
        },
      });
      let data = {};
      try { data = await res.json(); } catch (_) { /* 非 JSON 就是服务端炸了 */ }
      if (!res.ok || data.error) throw new Error(data.error || `没传上去（HTTP ${res.status}）`);
      return {
        file: data.file, url: data.url, kind: data.kind,
        name: data.name || file.name || "附件", size: data.size || file.size || 0,
      };
    }

    async function addNoteMedia(files, fallbackName) {
      const list = Array.from(files || []).filter((f) => f && f.size > 0);
      if (!list.length) return;
      const msg = $("#note-msg");
      for (let i = 0; i < list.length; i++) {
        if (msg) msg.textContent = `上传 ${i + 1}/${list.length}：${list[i].name}`;
        try {
          pending.push(await uploadNoteMedia(list[i], fallbackName));
          renderPending();
        } catch (e) {
          toast(e.message, "err");
        }
      }
      if (msg) msg.textContent = "";
    }

    $("#note-add")?.addEventListener("click", () => openComposer(null));
    $("#note-write")?.addEventListener("click", () => openComposer(null));

    // 随手记那块一进视野，就把浮着的 + 收起来：那会儿该点的是「写一条」，
    // 一个圆按钮压在正文和链接上只会碍事
    (function bindFabFade() {
      const fab = $("#note-add");
      const wrap = $("#notes-wrap");
      if (!fab || !wrap || !("IntersectionObserver" in window)) return;
      fabObserver?.disconnect();
      fabObserver = new IntersectionObserver(([entry]) => {
        fab.classList.toggle("away", entry.isIntersecting);
      }, { threshold: 0.12 });
      fabObserver.observe(wrap);
    })();

    $("#note-cancel")?.addEventListener("click", closeComposer);
    $("#note-files")?.addEventListener("change", async (ev) => {
      await addNoteMedia(ev.currentTarget.files);
      ev.currentTarget.value = "";
    });

    // 贴图：截图直接 Ctrl+V
    $("#note-text")?.addEventListener("paste", (e) => {
      const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
      if (!files.length) return;
      e.preventDefault();
      addNoteMedia(files);
    });
    $("#note-text")?.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        $("#note-save")?.click();
      }
    });

    // 拖进来也行：图片、音频、PDF、Word、Markdown……什么都收
    const editor = $("#note-editor");
    if (editor) {
      const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
      editor.addEventListener("dragover", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        editor.classList.add("drag-on");
      });
      editor.addEventListener("dragleave", (e) => {
        if (e.target === editor || !editor.contains(e.relatedTarget)) editor.classList.remove("drag-on");
      });
      editor.addEventListener("drop", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        editor.classList.remove("drag-on");
        addNoteMedia(e.dataTransfer.files);
      });
    }

    /* 录音：按一下开始，再按一下停，停下自动传上去挂在这条上 */

    let rec = null;        // { recorder, stream, chunks, started, timer, mime }

    function recStop(discard) {
      if (!rec) return;
      rec.discard = !!discard;      // 取消编辑器时录到的东西直接扔掉，不上传
      try { rec.recorder.stop(); } catch (_) { /* 已经停了 */ }
      rec.stream.getTracks().forEach((t) => t.stop());
      clearInterval(rec.timer);
    }

    async function recToggle() {
      const btn = $("#note-rec");
      const msg = $("#note-msg");
      if (rec) { recStop(); return; }
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        toast("这个浏览器不支持录音", "err");
        return;
      }
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        toast("麦克风没打开：" + (e.message || "被拒绝了"), "err");
        return;
      }
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", ""]
        .find((m) => !m || MediaRecorder.isTypeSupported(m));
      const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      const chunks = [];
      recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop = async () => {
        const type = (mime || "audio/webm").split(";")[0];
        const ext = type.includes("ogg") ? ".ogg" : ".webm";
        const blob = new Blob(chunks, { type });
        if (!blob.size || (rec && rec.discard) || state.discard) {
          if (msg) msg.textContent = "";
          return;
        }
        if (msg) msg.textContent = "传录音…";
        try {
          pending.push(await uploadNoteMedia(
            new File([blob], "录音" + new Date().toISOString().slice(11, 19).replace(/:/g, "") + ext,
              { type })));
          renderPending();
        } catch (e) {
          toast(e.message, "err");
        }
        if (msg) msg.textContent = "";
      };
      const started = Date.now();
      const tick = () => {
        const s = Math.floor((Date.now() - started) / 1000);
        if (msg) msg.textContent = `录音中 ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
      };
      const state = { recorder, stream, chunks, started, timer: setInterval(tick, 500), mime, discard: false };
      rec = state;
      recorder.start();
      tick();
      if (btn) { btn.textContent = "停止"; btn.classList.add("rec-on"); }
      if (msg) msg.textContent = "录音中 00:00";
      // 状态还原在 recStop 之后统一做
      recorder.addEventListener("stop", () => {
        rec = null;
        if (btn) { btn.textContent = "录音"; btn.classList.remove("rec-on"); }
      });
    }

    $("#note-rec")?.addEventListener("click", recToggle);
    // 关掉编辑器就别继续录了
    $("#note-cancel")?.addEventListener("click", () => { if (rec) recStop(true); });

    $("#note-save")?.addEventListener("click", async (ev) => {
      const text = $("#note-text").value.trim();
      if (!text && !pending.length) { toast("写点什么，或者贴张图、挂个附件", "warn"); $("#note-text").focus(); return; }
      if (rec) { toast("先停止录音", "warn"); return; }
      const payload = {
        text,
        media: pending.map((p) => ({ file: p.file, name: p.name, size: p.size })),
      };
      await withBusy(ev.currentTarget, async () => {
        try {
          if (editing) await post(`/api/notes/${editing.id}/update`, payload);
          else await post(`/api/notes/${c.id}`, payload);
          toast(editing ? "改好了" : "记下了");
          viewClip(c.id);
        } catch (e) {
          toast(e.message, "err");
        }
      });
    });

    $$("[data-note-edit]").forEach((el) => el.addEventListener("click", () => {
      const note = notes.find((n) => String(n.id) === el.dataset.noteEdit);
      if (note) openComposer(note);
    }));
    $$("[data-note-drop]").forEach((el) => el.addEventListener("click", async () => {
      if (!(await glConfirm("删掉这条随手记？里面的图也一起删。", { danger: true, confirmText: "删除" }))) return;
      try {
        await post(`/api/notes/${el.dataset.noteDrop}/delete`, {});
        toast("删掉了");
        viewClip(c.id);
      } catch (e) { toast(e.message, "err"); }
    }));
    $$("[data-note-preview]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.preventDefault();
      openNoteFilePreview({
        file: el.dataset.notePreview,
        name: el.dataset.noteName,
        size: Number(el.dataset.noteSize) || 0,
      }, el);
    }));
    /* 随手记里的图：点缩略图就在应用内开灯箱，不再新开浏览器标签。
       同一条随手记的多张图编成一组，可以左右翻。 */
    $$("[data-note-image]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.preventDefault();
      const tiles = $$("[data-note-image]", el.closest(".note-imgs") || document);
      const items = tiles.map((t) => {
        const src = t.dataset.noteSrc;
        return {
          src,
          download: src + (src.includes("?") ? "&" : "?") + "dl=1",
          name: t.dataset.noteName || "图片",
          size: Number(t.dataset.noteSize) || 0,
        };
      });
      openImageLightbox(items, tiles.indexOf(el), el);
    }));

    /* — 自己下指令 — */

    function aiError(html) {
      const msg = $("#ai-msg");
      if (msg) msg.innerHTML = `<span class="ai-error">${html}</span>`;
    }

    function revealResearch() {
      requestAnimationFrame(() => {
        $("#research")?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }

    async function instruct(text, button) {
      const input = $("#ai-instruct");
      const promptText = String(text ?? input.value).trim();
      if (promptText.length < 2) { input.focus(); return toast("想让 AI 做什么？写一句", "warn"); }
      const run = async () => {
        $("#ai-msg").innerHTML = `<span class="ai-working"><span class="ai-working-dot"></span>${
          isFile ? `正在读取 ${esc(fileKind)} 全文并回答…` : "AI 正在读这条材料…"}</span>`;
        try {
          const r = await post("/api/ai/instruct", { id: c.id, prompt: promptText });
          if (r.partial) toast(`整篇被内容审核拦了，只用了开头 ${r.coverage} 字`, "warn");
          if (r.direct_file) {
            toast(`已直接读取 ${fileKind} · ${Number(r.source_chars || 0).toLocaleString()} 字`, "ok");
          }
          await viewClip(c.id);
          revealResearch();
        } catch (e) {
          aiError(esc(e.message));
        }
      };
      return button ? withBusy(button, run) : run();
    }

    $("#ai-send").addEventListener("click", (ev) => instruct(null, ev.currentTarget));
    $("#ai-instruct").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); instruct(null); }
    });
    $$("[data-preset]").forEach((el) => el.addEventListener("click", () => instruct(el.dataset.preset)));
    $("[data-deep]").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      $("#ai-msg").innerHTML = `<span class="ai-working"><span class="ai-working-dot"></span>${
        isFile ? `正在直接读 ${esc(fileKind)}，整理结构…` : "正在深读，整理结构…"}</span>`;
      try {
        const r = await post("/api/ai/analyze", { id: c.id });
        if (r.partial) toast(`整篇被内容审核拦了，只用了开头 ${r.coverage} 字`, "warn");
        await viewClip(c.id);
        revealResearch();
      } catch (e) {
        aiError(esc(e.message));
      }
    }));

    /* — 简介可以手改 — */

    $("#sum-edit")?.addEventListener("click", () => {
      const host = $("#sum-body");
      host.innerHTML = `<textarea class="r-edit" id="sum-text" data-md-preview>${esc(c.summary)}</textarea>
        <div class="r-edit-acts"><button class="btn small" id="sum-save">保存</button>
        <button class="btn small ghost" id="sum-cancel">取消</button></div>`;
      bindMdPreview(host);
      $("#sum-cancel").addEventListener("click", () => viewClip(c.id));
      $("#sum-save").addEventListener("click", async (ev) => {
        const value = $("#sum-text").value.trim();
        await withBusy(ev.currentTarget, async () => {
          try {
            await post(`/api/clips/${c.id}`, { summary: value });
            toast("简介改好了");
            viewClip(c.id);
          } catch (e) { toast(e.message, "err"); }
        });
      });
    });

    /* — AI 产出的东西：能改，能删 — */

    function startEdit(oid) {
      const o = byId[oid];
      const host = $(`[data-body="${oid}"]`);
      if (!o || !host) return;
      host.innerHTML = `<textarea class="r-edit" id="edit-${oid}" data-md-preview>${esc(plainOutput(o))}</textarea>
        <div class="r-edit-acts">
          <button class="btn small" data-save="${oid}">保存</button>
          <button class="btn small ghost" data-cancel="${oid}">取消</button>
        </div>`;
      bindMdPreview(host);
      $(`[data-cancel="${oid}"]`).addEventListener("click", () => viewClip(c.id));
      $(`[data-save="${oid}"]`).addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
        const value = $(`#edit-${oid}`).value.trim();
        try {
          await post(`/api/ai/outputs/${oid}`, { content: { ...(o.content || {}), edited: value } });
          toast(value ? "改好了" : "清空了，等于删掉正文");
          viewClip(c.id);
        } catch (e) { toast(e.message, "err"); }
      }));
    }

    async function dropOutput(oid) {
      const o = byId[oid];
      if (!o || !(await glConfirm(`删掉「${o.title}」？删了就没了。`, { danger: true, confirmText: "删除" }))) return;
      try {
        await post(`/api/ai/outputs/${oid}/delete`, {});
        toast("已删除");
        viewClip(c.id);
      } catch (e) { toast(e.message, "err"); }
    }

    $$("[data-edit]").forEach((el) => el.addEventListener("click", () => startEdit(el.dataset.edit)));
    $$("[data-drop]").forEach((el) => el.addEventListener("click", () => dropOutput(el.dataset.drop)));

    $("#act-tier").addEventListener("change", async (e) => {
      try {
        await post(`/api/clips/${c.id}`, { tier: e.target.value });
        toast(e.target.value ? `信源标为 ${e.target.value}` : "信源改回未分级");
        viewClip(c.id);
      } catch (err) { toast(err.message, "err"); }
    });

    /* — 材料级：一个控件，一个问题「这条算哪一层」 — */

    async function setGrade(grade, done) {
      try {
        await post(`/api/clips/${c.id}`, { grade });
        toast(grade ? `定为「${GRADE_NAME[grade]}」` : "改回未定", "ok", grade || "unset");
        viewClip(c.id);
      } catch (err) { toast(err.message, "err"); }
    }
    $$("#act-grade .g-seg").forEach((el) => el.addEventListener("click", () => {
      if ((c.grade || "") === el.dataset.gradePick) return;
      setGrade(el.dataset.gradePick);
    }));
    $("#hint-take")?.addEventListener("click", () => setGrade(c.grade_hint));
    $("#hint-drop")?.addEventListener("click", async () => {
      try {
        await post(`/api/clips/${c.id}`, { grade_hint: "" });
        toast("建议收起来了");
        viewClip(c.id);
      } catch (err) { toast(err.message, "err"); }
    });
    $("#act-board").addEventListener("change", async (e) => {
      try {
        await post(`/api/clips/${c.id}`, { board_id: e.target.value || null });
        toast(e.target.value ? "换板块了" : "退回未分类");
        refreshSidebar();
        viewClip(c.id);   // 头部板块名、资料库进出按钮要跟着换
      } catch (err) {
        toast(err.message, "err");
        viewClip(c.id);
      }
    });
    // 资料库进出一键走：不用去翻那个下拉
    $("#act-arch-in")?.addEventListener("click", async (ev) => withBusy(ev.currentTarget, async () => {
      try {
        await post(`/api/clips/${c.id}`, { board_id: archiveHere.id });
        toast(`收进「${archiveHere.name}」了`, "ok");
        refreshSidebar();
        viewClip(c.id);
      } catch (err) { toast(err.message, "err"); }
    }));
    $("#act-arch-out")?.addEventListener("click", async (ev) => withBusy(ev.currentTarget, async () => {
      try {
        await post(`/api/clips/${c.id}`, { board_id: null });
        toast("移出资料库，退回未分类");
        refreshSidebar();
        viewClip(c.id);
      } catch (err) { toast(err.message, "err"); }
    }));
    $("#act-del").addEventListener("click", async () => {
      if (!(await glConfirm("删掉就没了，研究记录也一起删。确定？", { danger: true, confirmText: "删除" }))) return;
      try {
        await post("/api/clips/delete", { id: c.id });
        toast("已删除");
        location.hash = "#/";
      } catch (e) { toast(e.message, "err"); }
    });
    bindImagePreviews(main);
  }

  /* ── AI 产出卡片 ─────────────────────────────────── */

  const PRESETS = ["提炼三条要点", "这条哪里站不住", "换个立场重写", "对我有什么用"];
  const KIND_LABEL = { analyze: "深读", compare: "对比", instruct: "指令", ask: "问答", review: "回顾" };

  function plainOutput(o) {
    const c = o.content || {};
    if (c.edited) return c.edited;
    if (c.text) return c.text;
    if (c.raw) return c.raw;
    const parts = [];
    if (c.summary) parts.push(c.summary);
    if ((c.key_points || []).length) {
      parts.push("要点：\n" + c.key_points.map((p, i) => `${i + 1}. ${p}`).join("\n"));
    }
    if (c.conclusion) parts.push("结论：" + c.conclusion);
    if ((c.todos || []).length) {
      parts.push("待办：\n" + c.todos.map((p, i) => `${i + 1}. ${p}`).join("\n"));
    }
    return parts.join("\n\n");
  }

  /* 随手记附件：能直读的就在应用里展开，不把“看一眼”变成“先下载”。
     Markdown 先走一个很小的安全渲染器；HTML 标签一律转义，只开放 http(s) 链接。 */
  const NOTE_TEXT_EXTS = new Set([
    "md", "markdown", "txt", "log", "csv", "tsv", "json", "xml", "url",
    "yaml", "yml", "ini", "conf", "toml", "sql", "srt", "vtt",
    "py", "js", "ts", "css", "sh", "bat", "ps1", "java", "c", "h",
    "cpp", "go", "rs", "rb", "php", "tex",
    "docx", "pptx", "xlsx", "epub", "html", "htm",
  ]);
  const NOTE_OFFICE_EXTS = new Set(["docx", "pptx", "xlsx", "epub", "html", "htm"]);
  const NOTE_IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "avif"]);
  const NOTE_PDF_EXTS = new Set(["pdf"]);
  const NOTE_AUDIO_EXTS = new Set(["mp3", "m4a", "wav", "ogg", "oga", "opus", "aac", "flac", "webm"]);
  const extOf = (name) => String(name || "").split(".").pop().toLowerCase();

  function mdInline(raw) {
    const tokens = [];
    const stash = (html) => {
      tokens.push(html);
      return `\uE000${tokens.length - 1}\uE001`;
    };
    let s = esc(raw);
    s = s.replace(/`([^`\n]+)`/g, (_m, code) => stash(`<code>${code}</code>`));
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_m, label, url) => stash(`<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`));
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/&lt;br\s*\/?&gt;/gi, "<br>");
    s = s.replace(/\uE000(\d+)\uE001/g, (_m, i) => tokens[Number(i)] || "");
    return s;
  }

  function mdTableCells(line) {
    let s = String(line || "").trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    const cells = [];
    let cell = "";
    let escaped = false;
    for (const ch of s) {
      if (escaped) {
        cell += ch === "|" ? "|" : `\\${ch}`;
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === "|") {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += ch;
      }
    }
    cells.push(cell.trim());
    return cells;
  }

  const mdTableSep = (line) => {
    const s = String(line || "").trim();
    return s.includes("|") && /^[\s|:-]+$/.test(s) && s.includes("-");
  };

  function mdBlockStart(lines, i) {
    const line = lines[i] || "";
    if (!line.trim()) return true;
    if (/^\s*(```|~~~)/.test(line)) return true;
    if (/^\s{0,3}#{1,6}(?:\s+|(?=[^\x00-\x7f]))/.test(line)) return true;
    if (/^\s{0,3}>\s?/.test(line)) return true;
    if (/^\s*(?:[-*+]|\d+\.)\s+/.test(line)) return true;
    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) return true;
    return line.includes("|") && mdTableSep(lines[i + 1] || "");
  }

  function markdownHtml(raw) {
    const lines = String(raw || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      const fence = line.match(/^\s*(```|~~~)\s*([^\s]*)\s*$/);
      if (fence) {
        const mark = fence[1];
        const lang = fence[2] || "";
        const code = [];
        i++;
        while (i < lines.length && !new RegExp(`^\\s*${mark}\\s*$`).test(lines[i])) {
          code.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++;
        out.push(`<div class="md-code">${lang ? `<span>${esc(lang)}</span>` : ""}<pre><code>${
          esc(code.join("\n"))}</code></pre></div>`);
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})(?:\s+|(?=[^\x00-\x7f]))(.+)$/);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${mdInline(heading[2])}</h${level}>`);
        i++;
        continue;
      }

      if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        out.push("<hr>");
        i++;
        continue;
      }

      if (line.includes("|") && mdTableSep(lines[i + 1] || "")) {
        const header = mdTableCells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          rows.push(mdTableCells(lines[i]));
          i++;
        }
        out.push(`<div class="md-table-wrap"><table><thead><tr>${
          header.map((cell) => `<th>${mdInline(cell)}</th>`).join("")}</tr></thead><tbody>${
          rows.map((row) => `<tr>${header.map((_h, idx) => `<td>${mdInline(row[idx] || "")}</td>`)
            .join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }

      if (/^\s{0,3}>\s?/.test(line)) {
        const quoted = [];
        while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
          i++;
        }
        out.push(`<blockquote>${quoted.map(mdInline).join("<br>")}</blockquote>`);
        continue;
      }

      const listHit = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
      if (listHit) {
        const ordered = !!listHit[2];
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
          if (!m || !!m[2] !== ordered) break;
          items.push(m[3]);
          i++;
        }
        out.push(ordered
          ? `<ol>${items.map((item) => `<li>${mdInline(item)}</li>`).join("")}</ol>`
          : `<ul>${items.map((item) => `<li>${mdInline(item)}</li>`).join("")}</ul>`);
        continue;
      }

      const para = [line];
      i++;
      while (i < lines.length && !mdBlockStart(lines, i)) {
        para.push(lines[i]);
        i++;
      }
      out.push(`<p>${para.map(mdInline).join("<br>")}</p>`);
    }
    return out.join("");
  }

  /* ── 手写内容按 Markdown 排版 ─────────────────────────
     收存框、判断、随手记、批注、简介都是「自己写的话」，里面出现 # / ** /
     - / > / --- 这些记号时不该原样显示。统一走 markdownHtml：
       · mdBody()   显示用：返回带 .md-body 的块级排版；
       · mdPlain()  卡片摘要用：把记号去掉，只留干净的句子；
       · hasMarkdown() 判断有没有记号，没有就不额外渲染（纯文本照旧）；
       · bindMdPreview() 编辑时边写边看：有记号才在下面长出一块预览。
     纯文本不会有任何变化——markdownHtml 把它包成段落，换行仍然是 <br>。 */
  const MD_HINTS = [
    /(^|\n)[ \t]{0,3}#{1,6}(?:[ \t]|(?=[^\x00-\x7f]))/,  // 标题（#{空格} 与 #标题 都认）
    /(^|\n)[ \t]{0,3}>[ \t]?/,                           // 引用
    /(^|\n)[ \t]*(?:```|~~~)/,                           // 代码块
    /(^|\n)[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S/,             // 列表
    /(^|\n)[ \t]{0,3}(?:-\s*-\s*-|_\s*_\s*_|\*\s*\*\s*\*)[ \t]*(\n|$)/,  // 分隔线
    /(^|\n)[ \t]*\|.+\|[ \t]*(\n|$)/,                    // 表格
    /\*\*[^*\n]+\*\*/,                                   // 粗体
    /`[^`\n]+`/,                                         // 行内代码
    /\[[^\]]+\]\(https?:\/\//,                           // 链接
  ];

  function hasMarkdown(text) {
    const s = String(text || "");
    return MD_HINTS.some((re) => re.test(s));
  }

  function mdBody(text, extraClass = "") {
    const raw = String(text || "");
    if (!raw.trim()) return "";
    return `<div class="md-body${extraClass ? " " + extraClass : ""}">${
      markdownHtml(raw)}</div>`;
  }

  function mdPlain(text, limit = 0) {
    let s = String(text || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/^[ \t]{0,3}#{1,6}(?:[ \t]+|(?=[^\x00-\x7f]))/gm, "")
      .replace(/^[ \t]{0,3}>[ \t]?/gm, "")
      .replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+/gm, "")
      .replace(/^[ \t]{0,3}([-*_])[ \t]*(\1[ \t]*){2,}[ \t]*$/gm, " ")
      .replace(/\*\*([^*\n]+)\*\*/g, "$1")
      .replace(/__([^_\n]+)__/g, "$1")
      .replace(/(^|[^\w])\*([^*\n]+)\*(?!\*)/g, "$1$2")
      .replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, "$1$2")
      .replace(/`([^`\n]+)`/g, "$1")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, "$1")
      .replace(/\s+/g, " ")
      .trim();
    return limit > 0 ? s.slice(0, limit) : s;
  }

  /* 编辑区下方的实时预览：只在写出 Markdown 记号后出现，
     免得纯文字输入时平白多出一块和上面一模一样的字。
     长文（收存框里贴整篇文章）按 220ms 防抖，打一个字不重排整篇。 */
  function bindMdPreview(root) {
    const scope = root || document;
    scope.querySelectorAll("textarea[data-md-preview]").forEach((ta) => {
      if (ta.dataset.mdBound) return;
      ta.dataset.mdBound = "1";
      const box = document.createElement("div");
      box.className = "md-preview";
      box.hidden = true;
      ta.insertAdjacentElement("afterend", box);
      let timer = 0;
      const render = () => {
        const value = ta.value;
        if (!hasMarkdown(value)) {
          box.hidden = true;
          box.innerHTML = "";
          return;
        }
        box.hidden = false;
        box.innerHTML = `<span class="md-preview-tag">预览</span>${markdownHtml(value)}`;
      };
      const paint = () => {
        clearTimeout(timer);
        timer = setTimeout(render, ta.value.length > 8000 ? 220 : 90);
      };
      ta.addEventListener("input", paint);
      render();
    });
  }

  let pdfBundlePromise = null;
  function loadPdfBundle() {
    if (pdfBundlePromise) return pdfBundlePromise;
    pdfBundlePromise = import("/pdf.min.mjs").then((lib) => {
      if (!lib || !lib.getDocument || !lib.GlobalWorkerOptions) {
        throw new Error("PDF 渲染组件没有正确载入");
      }
      lib.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
      return lib;
    }).catch((err) => {
      pdfBundlePromise = null;
      throw err;
    });
    return pdfBundlePromise;
  }

  async function renderPdfPreview(host, url, statusEl) {
    host.innerHTML = `<div class="note-preview-loading"><span class="ai-working-dot"></span>正在准备 PDF 页面…</div>`;
    let pdf;
    try {
      const lib = await loadPdfBundle();
      const response = await fetch(url, { headers: { "X-GL-Client": clientId } });
      if (!response.ok) throw new Error(`读取 PDF 失败（HTTP ${response.status}）`);
      const length = Number(response.headers.get("Content-Length")) || 0;
      if (length > 60 * 1024 * 1024) {
        throw new Error("这份 PDF 超过 60MB，应用内逐页渲染会占用过多内存，请打开原件查看。");
      }
      const data = await response.arrayBuffer();
      pdf = await lib.getDocument({ data, useSystemFonts: true }).promise;
    } catch (e) {
      host.innerHTML = `<div class="note-preview-unavailable">
        <strong>PDF 页面渲染失败</strong>
        <span>${esc(e.message || "浏览器无法解析这份 PDF。")}</span>
        <a class="btn small" href="${url}" target="_blank" rel="noreferrer">在浏览器中打开</a>
      </div>`;
      if (statusEl) statusEl.textContent = "PDF 预览失败，可下载或在浏览器中打开原件。";
      return;
    }
    if (!host.isConnected) return;
    if (statusEl) statusEl.textContent = `PDF · ${pdf.numPages} 页 · 只读预览`;
    host.innerHTML = `<div class="note-pdf-pages" aria-label="PDF 页面"></div>
      <div class="note-pdf-more-wrap" hidden><button type="button" class="btn small ghost note-pdf-more"></button></div>`;
    const pages = $(".note-pdf-pages", host);
    const moreWrap = $(".note-pdf-more-wrap", host);
    const more = $(".note-pdf-more", host);
    let rendered = 0;
    const dpr = Math.min(2, window.devicePixelRatio || 1);

    async function renderBatch(size = 4) {
      if (!host.isConnected || rendered >= pdf.numPages) return;
      more.disabled = true;
      more.textContent = "渲染中…";
      const end = Math.min(pdf.numPages, rendered + size);
      for (let number = rendered + 1; number <= end; number++) {
        const page = await pdf.getPage(number);
        if (!host.isConnected) return;
        const card = document.createElement("section");
        card.className = "note-pdf-page";
        const label = document.createElement("span");
        label.className = "note-pdf-page-no";
        label.textContent = `第 ${number} / ${pdf.numPages} 页`;
        const canvas = document.createElement("canvas");
        card.append(label, canvas);
        pages.append(card);
        const base = page.getViewport({ scale: 1 });
        const available = Math.max(260, pages.clientWidth - 4);
        const scale = Math.min(1.55, available / base.width);
        const viewport = page.getViewport({ scale: scale * dpr });
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.style.width = `${Math.floor(viewport.width / dpr)}px`;
        canvas.style.height = `${Math.floor(viewport.height / dpr)}px`;
        await page.render({
          canvasContext: canvas.getContext("2d", { alpha: false }),
          viewport,
        }).promise;
        if (!host.isConnected) return;
      }
      rendered = end;
      more.disabled = false;
      if (rendered >= pdf.numPages) moreWrap.hidden = true;
      else {
        moreWrap.hidden = false;
        more.textContent = `继续载入后 ${Math.min(4, pdf.numPages - rendered)} 页`;
      }
    }

    more.addEventListener("click", () => {
      renderBatch(4).catch((e) => toast(e.message || "PDF 页面渲染失败", "err"));
    });
    renderBatch(4).catch((e) => toast(e.message || "PDF 页面渲染失败", "err"));
  }

  // 当前附件的预览弹层怎么收掉：重开时要先释放旧的 keydown 监听
  let notePreviewCleanup = null;

  async function openNoteFilePreview(meta, trigger) {
    const ext = extOf(meta.name);
    const stored = String(meta.file || "");
    // 主收存图 / 待收预览会直接给出 src；随手记附件仍按存储文件名拼地址。
    const url = String(meta.src || ("/api/notes/img/" + encodeURIComponent(stored)));
    const download = String(meta.download || (url + (url.includes("?") ? "&" : "?") + "dl=1"));
    const safeUrl = esc(url);
    const safeDownload = esc(download);
    const isImage = !!meta.forceImage || NOTE_IMAGE_EXTS.has(ext);
    const isPdf = NOTE_PDF_EXTS.has(ext);
    const isAudio = NOTE_AUDIO_EXTS.has(ext) && !["webm"].includes(ext);
    const canRead = NOTE_TEXT_EXTS.has(ext);
    const previewTag = ext === "docx" ? " · Word 排版预览"
      : NOTE_OFFICE_EXTS.has(ext) ? " · 提取文字预览" : "";

    try { notePreviewCleanup?.(); } catch (_) { }
    $(".note-preview-backdrop")?.remove();
    document.body.classList.add("note-preview-open");
    const backdrop = document.createElement("div");
    backdrop.className = "note-preview-backdrop";
    backdrop.innerHTML = `<section class="note-preview-modal" role="dialog" aria-modal="true"
        aria-labelledby="note-preview-title">
      <header class="note-preview-head">
        <div class="note-preview-file">
          <span class="note-preview-ext">${esc((ext || "FILE").toUpperCase().slice(0, 5))}</span>
          <div>
            <strong id="note-preview-title">${esc(meta.name || "附件预览")}</strong>
            <span>${esc(fmtSize(meta.size || 0))}${esc(previewTag)}</span>
          </div>
        </div>
        <div class="note-preview-actions">
          <a class="btn small ghost" href="${safeDownload}" download="${esc(meta.name || "附件")}">下载原件</a>
          <button type="button" class="note-preview-close" aria-label="关闭预览">×</button>
        </div>
      </header>
      <div class="note-preview-body">
        ${isPdf ? `<div class="note-pdf-host"></div>`
          : isImage ? `<div class="note-preview-image"><img src="${safeUrl}" alt="${esc(meta.name)}"></div>`
          : isAudio ? `<div class="note-preview-media"><span>音频附件</span>
              <audio controls preload="metadata" src="${safeUrl}"></audio></div>`
          : canRead ? `<div class="note-preview-loading"><span class="ai-working-dot"></span>正在读取…</div>`
          : `<div class="note-preview-unavailable">
              <strong>这种格式暂不支持应用内预览</strong>
              <span>可以下载原件，用本机程序打开。</span>
              <a class="btn small" href="${safeDownload}" download="${esc(meta.name || "附件")}">下载原件</a>
            </div>`}
      </div>
      <footer class="note-preview-foot">
        <span class="note-preview-status">${canRead
          ? "原件只读展示，不会修改或另存副本。"
          : "文件仍保存在本机附件目录。"}</span>
        <span class="grow"></span>
        <span class="note-preview-shortcut">Esc 关闭</span>
      </footer>
    </section>`;
    document.body.appendChild(backdrop);
    const close = () => {
      document.removeEventListener("keydown", onKey);
      backdrop.remove();
      document.body.classList.remove("note-preview-open");
      notePreviewCleanup = null;
      trigger?.focus?.();
    };
    const onKey = (ev) => { if (ev.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    // 下一次打开靠它把监听和 DOM 一起收干净，而不是只 remove 节点
    notePreviewCleanup = close;
    backdrop.addEventListener("click", (ev) => { if (ev.target === backdrop) close(); });
    $(".note-preview-close", backdrop)?.addEventListener("click", close);
    requestAnimationFrame(() => $(".note-preview-close", backdrop)?.focus());

    const body = $(".note-preview-body", backdrop);
    if (isPdf) {
      await renderPdfPreview($(".note-pdf-host", backdrop), url,
        $(".note-preview-status", backdrop));
      return;
    }
    if (!canRead) return;
    try {
      const r = await api(`/api/notes/preview?file=${encodeURIComponent(stored)}`, { timeout: 30000 });
      const text = String(r.text || "");
      if (!r.ok) {
        body.innerHTML = `<div class="note-preview-unavailable">
          <strong>暂时读不了这份附件</strong>
          <span>${esc(r.status || "文件没有可提取的文字。")}</span>
          <a class="btn small" href="${safeDownload}" download="${esc(meta.name || "附件")}">下载原件</a>
        </div>`;
      } else if (r.kind === "markdown") {
        body.innerHTML = `<article class="note-preview-markdown">${
          markdownHtml(text) || "<p class='faint'>文件是空的。</p>"}</article>`;
      } else if (r.kind === "document") {
        body.innerHTML = `<div class="note-preview-note">Word 排版预览：标题、段落、链接和表格已保留；图片与浮动版式请下载原件查看。</div>
          <article class="note-preview-document">${r.html || ""}</article>`;
      } else {
        body.innerHTML = `${NOTE_OFFICE_EXTS.has(ext) ? `<div class="note-preview-note">这是从原件中抽取的可读文字；表格和版式已按内容展开，复杂排版请下载原件核对。</div>` : ""}
          <pre class="note-preview-text">${esc(text || "文件是空的。")}</pre>`;
      }
      if (r.truncated) {
        $(".note-preview-status", backdrop).textContent =
          "内容较长，这里展示的是可读预览；完整内容请下载原件。";
      }
    } catch (e) {
      body.innerHTML = `<div class="note-preview-unavailable">
        <strong>读取失败</strong>
        <span>${esc(e.message)}</span>
        <a class="btn small" href="${safeDownload}" download="${esc(meta.name || "附件")}">下载原件</a>
      </div>`;
    }
  }

  /* ── 随手记图片灯箱 ──────────────────────────────────────────
     缩略图不再用 <a target="_blank">：原生窗口会把新标签交给系统浏览器，
     看一眼自己刚贴的图不该离开应用。这里在同一页面里直接放大看。 */
  let imageLightboxCleanup = null;

  function openImageLightbox(items, index = 0, trigger = null) {
    const list = (items || []).filter((it) => it && it.src);
    if (!list.length) return;
    if (imageLightboxCleanup) { imageLightboxCleanup(); imageLightboxCleanup = null; }
    // 附件预览弹层若还开着，先收干净，别让两层遮罩叠在一起
    try { notePreviewCleanup?.(); } catch (_) { }

    let at = Math.max(0, Math.min(list.length - 1, index));
    let scale = 1;                    // 1 = 适应窗口，>1 = 相对适应尺寸放大
    let panX = 0, panY = 0;
    const MAX_SCALE = 6;

    document.body.classList.add("gl-lightbox-open");
    const root = document.createElement("div");
    root.className = "gl-lightbox";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "随手记图片");
    root.innerHTML = `
      <div class="gl-lb-head">
        <div class="gl-lb-file">
          <strong class="gl-lb-name"></strong>
          <span class="gl-lb-meta"></span>
        </div>
        <div class="gl-lb-tools">
          <button type="button" class="gl-lb-btn" data-lb="out" title="缩小（−）" aria-label="缩小">−</button>
          <span class="gl-lb-scale" aria-live="polite">适应</span>
          <button type="button" class="gl-lb-btn" data-lb="in" title="放大（＋）" aria-label="放大">＋</button>
          <button type="button" class="gl-lb-btn wide" data-lb="fit" title="回到适应窗口（0）">适应</button>
          <a class="gl-lb-btn wide gl-lb-download" href="#" title="把原图存到本机">下载原件</a>
          <button type="button" class="gl-lb-btn gl-lb-close" data-lb="close" title="关闭（Esc）" aria-label="关闭">×</button>
        </div>
      </div>
      <div class="gl-lb-stage">
        <button type="button" class="gl-lb-nav prev" data-lb="prev" aria-label="上一张">‹</button>
        <div class="gl-lb-canvas">
          <img class="gl-lb-img" alt="" draggable="false">
          <span class="gl-lb-broken">这张图暂时读不出来，可以下载原件查看</span>
        </div>
        <button type="button" class="gl-lb-nav next" data-lb="next" aria-label="下一张">›</button>
      </div>
      <div class="gl-lb-foot">
        <span class="gl-lb-count"></span>
        <span class="grow"></span>
        <span class="gl-lb-tip">滚轮缩放 · 双击看原尺寸 · 放大后拖动 · ← → 翻图 · Esc 关闭</span>
      </div>`;
    document.body.appendChild(root);

    const stage = $(".gl-lb-stage", root);
    const canvas = $(".gl-lb-canvas", root);
    const img = $(".gl-lb-img", root);
    const nameEl = $(".gl-lb-name", root);
    const metaEl = $(".gl-lb-meta", root);
    const countEl = $(".gl-lb-count", root);
    const scaleEl = $(".gl-lb-scale", root);
    const dl = $(".gl-lb-download", root);
    const prev = $('[data-lb="prev"]', root);
    const next = $('[data-lb="next"]', root);

    const applyTransform = () => {
      img.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
      canvas.classList.toggle("is-zoomed", scale > 1.001);
      scaleEl.textContent = scale <= 1.001 ? "适应" : `${Math.round(scale * 100)}%`;
    };
    const zoomTo = (want) => {
      scale = Math.max(1, Math.min(MAX_SCALE, Number(want) || 1));
      if (scale === 1) panX = panY = 0;
      applyTransform();
    };
    const show = (i) => {
      at = (i + list.length) % list.length;
      const it = list[at];
      scale = 1; panX = panY = 0;
      root.classList.remove("gl-lb-error");
      img.src = it.src;
      img.alt = it.name || "随手记图片";
      nameEl.textContent = it.name || "图片";
      metaEl.textContent = `${fmtSize(it.size || 0)} · 原图`;
      countEl.textContent = list.length > 1 ? `${at + 1} / ${list.length}` : "单张图片";
      dl.href = it.download || it.src;
      dl.setAttribute("download", it.name || "图片");
      prev.hidden = next.hidden = list.length < 2;
      applyTransform();
      // 预取相邻两张，翻图时不留白
      if (list.length > 1) {
        [1, -1].forEach((d) => {
          const p = new Image();
          p.src = list[(at + d + list.length) % list.length].src;
        });
      }
    };

    const close = () => {
      document.removeEventListener("keydown", onKey);
      stage.removeEventListener("wheel", onWheel);
      root.remove();
      document.body.classList.remove("gl-lightbox-open");
      imageLightboxCleanup = null;
      trigger?.focus?.();
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); close(); }
      else if (ev.key === "ArrowLeft" && list.length > 1) { ev.preventDefault(); show(at - 1); }
      else if (ev.key === "ArrowRight" && list.length > 1) { ev.preventDefault(); show(at + 1); }
      else if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); zoomTo(scale * 1.25); }
      else if (ev.key === "-" || ev.key === "_") { ev.preventDefault(); zoomTo(scale / 1.25); }
      else if (ev.key === "0") { ev.preventDefault(); zoomTo(1); }
    };
    const onWheel = (ev) => {
      ev.preventDefault();
      zoomTo(scale * (ev.deltaY < 0 ? 1.12 : 1 / 1.12));
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    document.addEventListener("keydown", onKey);
    imageLightboxCleanup = close;

    root.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-lb]");
      if (btn) {
        const act = btn.dataset.lb;
        if (act === "close") close();
        else if (act === "in") zoomTo(scale * 1.25);
        else if (act === "out") zoomTo(scale / 1.25);
        else if (act === "fit") zoomTo(1);
        else if (act === "prev") show(at - 1);
        else if (act === "next") show(at + 1);
        return;
      }
      // 只点背景收起；点图片本身、下载链接都不收
      if (ev.target === stage || ev.target === canvas) close();
    });
    img.addEventListener("error", () => root.classList.add("gl-lb-error"));
    img.addEventListener("dblclick", (ev) => {
      ev.preventDefault();
      zoomTo(scale > 1.001 ? 1 : 2);
    });

    let dragging = null;
    canvas.addEventListener("pointerdown", (ev) => {
      if (scale <= 1.001 || ev.button !== 0) return;
      dragging = { x: ev.clientX, y: ev.clientY, panX, panY };
      canvas.setPointerCapture(ev.pointerId);
      canvas.classList.add("is-dragging");
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      panX = dragging.panX + (ev.clientX - dragging.x);
      panY = dragging.panY + (ev.clientY - dragging.y);
      applyTransform();
    });
    const endDrag = () => { dragging = null; canvas.classList.remove("is-dragging"); };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    show(at);
    requestAnimationFrame(() => $(".gl-lb-close", root)?.focus());
  }

  function noteCard(n) {
    const src = (file) => "/api/notes/img/" + encodeURIComponent(file);
    const imgs = (n.images || []).map((it) => {
      const name = it.name || it.file || "图片";
      return `
      <button type="button" class="note-img" data-note-image="1"
        data-note-src="${esc(src(it.file))}" data-note-name="${esc(name)}"
        data-note-size="${Number(it.size) || 0}"
        title="点开看大图" aria-label="查看 ${esc(name)}">
        <img src="${esc(src(it.file))}" alt="" loading="lazy" draggable="false"></button>`;
    }).join("");
    const audios = (n.audios || []).map((it) => `
      <div class="note-audio-bar">
        <audio controls preload="none" src="${src(it.file)}"></audio>
        <a class="note-audio-dl" href="${src(it.file)}?dl=1">下载</a></div>`).join("");
    const files = (n.files || []).map((it) => `
      <a class="note-file" href="${src(it.file)}" target="_blank" rel="noreferrer"
        data-note-preview="${esc(it.file)}" data-note-name="${esc(it.name)}"
        data-note-size="${Number(it.size) || 0}" title="点开预览">
        <span class="note-file-ext">${esc((it.name.split(".").pop() || "文件").slice(0, 4).toUpperCase())}</span>
        <span class="note-file-name">${esc(it.name)}</span>
        <span class="note-file-size">${esc(fmtSize(it.size || 0))}</span>
        <span class="note-file-action">查看</span>
      </a>`).join("");
    return `<div class="note" data-nid="${n.id}">
      ${n.text ? mdBody(n.text, "note-text") : ""}
      ${imgs ? `<div class="note-imgs">${imgs}</div>` : ""}
      ${audios ? `<div class="note-audios">${audios}</div>` : ""}
      ${files ? `<div class="note-files">${files}</div>` : ""}
      <div class="note-foot">
        <span class="note-time">${esc(fmtDate(n.created_at))}${n.updated_at ? " · 改过" : ""}</span>
        <span class="r-acts">
          <span class="cc-act" data-note-edit="${n.id}">编辑</span>
          <span class="cc-act danger" data-note-drop="${n.id}">删除</span>
        </span>
      </div>
    </div>`;
  }

  function researchCard(o) {
    const c = o.content || {};
    let body;
    if (c.edited) {
      body = `${mdBody(c.edited, "ai-box")}<div class="r-edited">手改过的版本</div>`;
    } else if (c.text) {
      body = mdBody(c.text, "ai-box");
    } else if (c.raw) {
      body = mdBody(c.raw, "ai-box");
    } else {
      const points = (c.key_points || []).map((p) => `<li>${esc(p)}</li>`).join("");
      const todos = (c.todos || []).map((p) => `<li>${esc(p)}</li>`).join("");
      const tgs = (c.tags || []).map((t) => `<span class="chip mini">${esc(t)}</span>`).join("");
      body = `${c.summary ? mdBody(c.summary, "ai-box") : ""}
        ${points ? `<div class="r-block"><div class="r-label">要点</div><ul>${points}</ul></div>` : ""}
        ${c.conclusion ? `<div class="r-block"><div class="r-label">结论</div>
          ${mdBody(c.conclusion, "ai-box")}</div>` : ""}
        ${todos ? `<div class="r-block"><div class="r-label">待办</div><ul>${todos}</ul></div>` : ""}
        ${tgs ? `<div class="cc-tags">${tgs}</div>` : ""}`;
    }
    const partial = Number(c.coverage) > 0 && Number(c.source_chars) > Number(c.coverage);
    return `<div class="r-card" data-oid="${o.id}">
      <div class="r-head">
        <span class="r-kind">${KIND_LABEL[o.kind] || "AI"}</span>
        <span class="r-title">${esc(o.title)}</span>
        <span class="r-time">${esc(fmtDate(o.created_at))}</span>
        <span class="r-acts">
          <span class="cc-act" data-edit="${o.id}">编辑</span>
          <span class="cc-act danger" data-drop="${o.id}">删除</span>
        </span>
      </div>
      <div class="r-body" data-body="${o.id}">${body}</div>
      ${partial ? `<div class="r-note">只用了开头 ${Number(c.coverage)} 字（全文
        ${Number(c.source_chars)} 字）——整篇被上游内容审核拦下了，没冒充读完全文</div>` : ""}
    </div>`;
  }

  /* 历史记录也能删——AI 写过的东西不是圣旨 */
  function bindHistoryDelete(onDone) {
    $$(".hist-x").forEach((el) => el.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!(await glConfirm("删掉这条记录？", { danger: true, confirmText: "删除" }))) return;
      try {
        await post(`/api/ai/outputs/${el.dataset.drop}/delete`, {});
        toast("已删除");
        onDone();
      } catch (err) { toast(err.message, "err"); }
    }));
  }

  /* ══ 问 ═════════════════════════════════════════════ */

  async function viewAsk() {
    const seq = routeSeq;
    const [st, hist] = await Promise.all([
      api("/api/ai/status"), api("/api/ai/outputs?kind=ask"),
    ]);
    if (seq !== routeSeq) return;

    main.innerHTML = head("问", "只在你收存的东西里找答案；每句都带出处")
      + panel("", "", `
        <div class="cap">
          <textarea id="ask-q" placeholder="问一个你收存过的主题，比如：这几篇对 X 的判断一致吗"></textarea>
          <div class="cap-foot">
            <span class="faint">${st.ready ? `AI · ${esc(st.model)}` : "AI 未配置，先去做设置"}</span>
            <span class="grow"></span>
            <button class="btn" id="ask-go" ${st.ready ? "" : "disabled"}>问</button>
          </div>
        </div>`)
      + `<div id="ask-out"></div>`
      + (hist.outputs.length ? panel("问过的问题", "", `
          <div class="hist">${hist.outputs.slice(0, 12).map((o) => `
            <div class="hist-row" data-oid="${o.id}">
              <span class="hist-q">${esc(o.title)}</span>
              <span class="hist-t">${esc(fmtDate(o.created_at))}</span>
              <span class="hist-x" data-drop="${o.id}" title="删除这条">×</span>
            </div>`).join("")}</div>`) : "");

    const box = $("#ask-q");
    box.focus();

    function render(data, question) {
      // 答案可能回来得比人切走得晚：节点重新取一次，路已经换了就丢掉
      const out = $("#ask-out");
      if (!out || !out.isConnected || seq !== routeSeq) return;
      if (!data.answer) {
        out.innerHTML = `<div class="panel"><div class="panel-body">
          ${empty("没有答案", data.empty || "换个问法试试")}</div></div>`;
        return;
      }
      const used = data.used || [];
      const weakNote = data.weak ? ` · 弱信源句已略 ${data.weak}` : "";
      out.innerHTML = `<div class="panel"><div class="panel-head">
          <div class="panel-title">${esc(question)}</div>
          <div class="panel-sub">${used.length} 条材料 · 丢弃无出处句 ${data.dropped}${weakNote}</div></div>
        <div class="panel-body">
          <div class="answer">${withCites(data.answer)}</div>
          ${used.length ? `<div class="used"><span class="f-label">依据</span>${used.map((u) =>
            `<a class="chip used-chip" href="#/c/${u.id}">${u.grade_label
              ? `<span class="g-badge g-${esc(u.grade || "unset")}">${esc(u.grade_label)}</span>` : ""}${u.tier
              ? `<span class="chip-tier ${esc(u.tier.toLowerCase())}">${esc(u.tier)}</span>` : ""}${esc(u.title)}</a>`).join("")}</div>` : ""}
        </div></div>`;
    }

    const ask = async () => {
      const q = box.value.trim();
      if (!q) return;
      const host = $("#ask-out");
      if (!host || seq !== routeSeq) return;
      host.innerHTML = `<div class="loading">在收存里找…</div>`;
      try {
        const data = await post("/api/ask", { q });
        render(data, q);
      } catch (e) {
        const out = $("#ask-out");
        if (!out || !out.isConnected || seq !== routeSeq) return;
        out.innerHTML = `<div class="err">${esc(e.message)}</div>`;
      }
    };
    $("#ask-go").addEventListener("click", (ev) => withBusy(ev.currentTarget, ask));
    box.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); ask(); }
    });

    $$(".hist-row").forEach((row) => row.addEventListener("click", async () => {
      const o = hist.outputs.find((x) => String(x.id) === row.dataset.oid);
      if (!o) return;
      box.value = o.title;
      render({ answer: o.content.answer, used: o.content.used || [], dropped: 0 }, o.title);
    }));
    bindHistoryDelete(() => viewAsk());
  }

  const withCites = (text) => esc(text).replace(/\[(S-(\d+))\]/g,
    (_m, full, id) => `<a class="cite" href="#/c/${id}">${full}</a>`);

  /* ══ 回顾 ═══════════════════════════════════════════ */

  async function viewReview() {
    const seq = routeSeq;
    const [st, hist] = await Promise.all([
      api("/api/ai/status"), api("/api/ai/outputs?kind=review"),
    ]);
    if (seq !== routeSeq) return;
    let days = 7;

    const usedChip = (u) => `<a class="chip used-chip" href="#/c/${u.id}">${u.grade_label
      ? `<span class="g-badge g-${esc(u.grade || "unset")}">${esc(u.grade_label)}</span>` : ""}${u.tier
      ? `<span class="chip-tier ${esc(u.tier.toLowerCase())}">${esc(u.tier)}</span>` : ""}${esc(u.title)}</a>`;

    main.innerHTML = head("回顾", "把最近收的东西串成一段：在讲什么、几条主线、哪里还缺证据")
      + panel("", "", `
        <div class="cap">
          <div class="cap-foot">
            <span class="f-label">范围</span>
            ${[7, 30, 90].map((d) => `<span class="chip ${d === days ? "on" : ""}" data-days="${d}">${d} 天</span>`).join("")}
            <span class="grow"></span>
            <button class="btn" id="rv-go" ${st.ready ? "" : "disabled"}>生成回顾</button>
          </div>
        </div>`)
      + `<div id="rv-out"></div>`
      + (hist.outputs.length ? panel("往次回顾", "", `<div class="hist">${hist.outputs.slice(0, 10).map((o) => `
          <div class="hist-row" data-oid="${o.id}">
            <span class="hist-q">${esc(o.title)}</span>
            <span class="hist-t">${esc(fmtDate(o.created_at))}</span>
            <span class="hist-x" data-drop="${o.id}" title="删除这条">×</span>
          </div>`).join("")}</div>`) : "");

    function render(data) {
      // 同「问」：异步回来时重新取节点，路由走了就丢掉这次结果
      const out = $("#rv-out");
      if (!out || !out.isConnected || seq !== routeSeq) return;
      if (!data.answer) {
        out.innerHTML = `<div class="panel"><div class="panel-body">
          ${empty("没写成", data.empty || "再试一次")}</div></div>`;
        return;
      }
      const used = data.used || [];
      out.innerHTML = `<div class="panel">
        <div class="panel-head"><div class="panel-title">最近 ${data.count} 条</div>
          <div class="panel-sub">带编号的句子点一下回到原件</div></div>
        <div class="panel-body">
          <div class="answer">${withCites(data.answer)}</div>
          ${used.length ? `<div class="used"><span class="f-label">依据</span>${used.slice(0, 12).map(usedChip).join("")}</div>` : ""}
        </div></div>`;
    }

    const go = async () => {
      const host = $("#rv-out");
      if (!host || seq !== routeSeq) return;
      host.innerHTML = `<div class="loading">在翻你的收存…</div>`;
      try {
        render(await post("/api/ai/review", { days }));
      } catch (e) {
        const out = $("#rv-out");
        if (!out || !out.isConnected || seq !== routeSeq) return;
        out.innerHTML = `<div class="err">${esc(e.message)}</div>`;
      }
    };
    $("#rv-go").addEventListener("click", (ev) => withBusy(ev.currentTarget, go));
    $$("[data-days]").forEach((el) => el.addEventListener("click", () => {
      days = Number(el.dataset.days);
      $$("[data-days]").forEach((x) => x.classList.toggle("on", x === el));
    }));
    $$(".hist-row").forEach((row) => row.addEventListener("click", () => {
      const o = hist.outputs.find((x) => String(x.id) === row.dataset.oid);
      if (!o) return;
      const used = o.content.used || [];
      render({ answer: o.content.answer, used, count: used.length });
    }));
    bindHistoryDelete(() => viewReview());
  }

  /* ══ 设置 ═══════════════════════════════════════════ */

  /* ══ 检索 ═══════════════════════════════════════════ */

  async function viewSearch() {
    const seq = routeSeq;
    const q = searchState.q.trim();
    const [saved, data] = await Promise.all([
      api("/api/searches"),
      q ? api(`/api/search?q=${encodeURIComponent(q)}&limit=40`) : Promise.resolve(null),
    ]);
    if (seq !== routeSeq) return;

    const terms = (data && data.terms) || [];
    const mark = (text) => {
      let out = esc(text);
      for (const term of terms) {
        if (!term) continue;
        const needle = esc(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        out = out.replace(new RegExp(needle, "gi"), (m) => `<mark>${m}</mark>`);
      }
      return out;
    };

    const savedChips = saved.searches.map((item) => `
      <span class="chip saved-chip ${item.q === searchState.q ? "on" : ""}" data-sq="${esc(item.q)}">
        ${esc(item.q)}<span class="saved-x" data-sdel="${item.id}" title="删掉这个检索">×</span></span>`).join("");

    const hits = ((data && data.results) || []).map((r) => `
      <a class="hit" href="#/c/${r.id}">
        <div class="hit-title">${mark(r.title)}</div>
        <div class="hit-meta">
          ${r.board ? `<span class="cc-board">${esc(r.board)}</span>` : ""}
          ${gradeBadge(r.grade)}
          ${tierBadge(r.tier)}
          <span class="hit-where">命中 ${esc(r.where)}</span>
          <span class="hit-score">相关度 ${r.score}</span>
        </div>
        ${r.snippet ? `<div class="hit-snip">${mark(r.snippet)}</div>` : ""}
      </a>`).join("");

    main.innerHTML = head("检索", "标题、标签、简介、我的话、正文、随手记，一次搜完")
      + panel("", "", `
        <div class="search-row">
          <input type="search" id="s-q" value="${esc(searchState.q)}"
            placeholder="一个词，或几个词，空格分开">
          <button class="btn" id="s-go">检索</button>
          ${q ? `<button class="btn small ghost" id="s-save">保存这组检索</button>` : ""}
        </div>
        ${savedChips ? `<div class="filters saved-row"><span class="f-label">已保存</span>${savedChips}</div>` : ""}
        <div class="faint search-meta">${data
          ? `「${esc(data.q)}」命中 ${data.total} 条${data.total > data.results.length
              ? ` · 只显示前 ${data.results.length} 条` : ""}`
          : "回车或点「检索」。"}</div>
      `)
      + (data ? `<div class="hits">${hits || empty("没有命中",
          "换个词，或者先把材料收进来")}</div>` : "");

    const box = $("#s-q");
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);

    const run = (value) => {
      searchState.q = String(value ?? box.value).trim();
      filter.offset = 0;
      viewSearch();
    };
    $("#s-go").addEventListener("click", () => withBusy($("#s-go"), async () => run()));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); run(); }
    });
    $("#s-save")?.addEventListener("click", async () => {
      try {
        const r = await post("/api/searches", { q: searchState.q });
        toast(r.duplicate ? "这组检索已经存过了" : "存下了");
        viewSearch();
      } catch (e) { toast(e.message, "err"); }
    });
    $$(".saved-chip").forEach((el) => el.addEventListener("click", (e) => {
      if (e.target.closest("[data-sdel]")) return;
      run(el.dataset.sq);
    }));
    $$("[data-sdel]").forEach((el) => el.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await post(`/api/searches/${el.dataset.sdel}/delete`, {});
        toast("删掉了");
        viewSearch();
      } catch (err) { toast(err.message, "err"); }
    }));
  }

  /* ══ 洞察 ═══════════════════════════════════════════ */

  const LENS_ICON = {
    pattern: '<circle cx="6.2" cy="7" r="2.1"/><circle cx="17.8" cy="6.2" r="2.1"/><circle cx="12" cy="17.2" r="2.1"/><path d="M8.2 7.8l2.6 7.2M15.9 7.6l-2.7 7.4M8.3 6.8h7.4"/>',
    scale: '<path d="M12 4.4v14.4M8 19h8M5.6 8.4h12.8"/><path d="M5.6 8.4l-2 4.2h4zM18.4 8.4l-2 4.2h4z"/>',
    reverse: '<path d="M7.2 18.4V10a4 4 0 014-4h6.2"/><path d="M14.4 3l3 3-3 3"/>',
    chain: '<rect x="3.6" y="9.2" width="6.8" height="10.6" rx="1.6"/><rect x="13.6" y="4.2" width="6.8" height="10.6" rx="1.6"/>',
    eye: '<path d="M2.8 12S6.4 6.2 12 6.2 21.2 12 21.2 12 17.6 17.8 12 17.8 2.8 12 2.8 12z"/><circle cx="12" cy="12" r="2.5"/>',
    split: '<path d="M12 4.4v8.4"/><path d="M12 12.8L7 19.4M12 12.8l5 6.6"/>',
    debate: '<path d="M3.4 6.4h8.8v6.6H7.6L4.6 15.4z"/><path d="M14.6 11h6v5.8h-2.8L15.4 19z"/>',
    branch: '<circle cx="6.4" cy="6.2" r="2"/><circle cx="18" cy="8.4" r="2"/><circle cx="18" cy="17.4" r="2"/><path d="M8.4 6.2h3.2a4 4 0 014 4v.4M8.4 6.6v8.8a4 4 0 004 4h3.2"/>',
  };
  const lensIcon = (key) => `<svg viewBox="0 0 24 24" aria-hidden="true">${LENS_ICON[key] || LENS_ICON.pattern}</svg>`;

  let insightScope = { kind: "all", id: "" };
  let insightResult = null;
  let insightBusy = "";

  function insightParas(text) {
    // 模型偶尔还是会甩 **粗体** 出来；先转义再还原成 <strong>，字面星号留着难看
    const fmt = (p) => esc(p).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br>");
    return String(text || "").split(/\n{2,}/).map((p) => p.trim()).filter(Boolean)
      .map((p) => (p.startsWith("下一步")
        ? `<p class="ins-next">${fmt(p)}</p>`
        : `<p>${fmt(p)}</p>`)).join("");
  }

  function insightMeta(r) {
    return [r.scope, r.count ? `${r.count} 条` : "", fmtDate(r.created_at)]
      .filter(Boolean).join(" · ");
  }

  async function viewInsight() {
    const seq = routeSeq;
    const [lensData, boards, hist] = await Promise.all([
      api("/api/insights/lenses"), api("/api/boards"), api("/api/insights"),
    ]);
    if (seq !== routeSeq) return;

    const scopeLabel = () => {
      if (insightScope.kind === "all") return "全部收存";
      if (insightScope.kind === "recent") return `最近 ${insightScope.id} 天`;
      const b = boards.boards.find((x) => String(x.id) === insightScope.id);
      return `板块「${insightScope.id === "none" ? "未分类" : (b ? b.name : "?")}」`;
    };
    const scopeCount = () => {
      if (insightScope.kind === "all") return lensData.total;
      if (insightScope.kind === "board") {
        const b = boards.boards.find((x) => String(x.id) === insightScope.id);
        return insightScope.id === "none" ? (boards.none || 0) : (b ? b.count : 0);
      }
      return null;
    };

    const scopes = [
      ["all", "", "全部收存"],
      ["recent", "30", "最近 30 天"],
      ["recent", "90", "最近 90 天"],
      ...boards.boards.map((b) => ["board", String(b.id), b.name]),
      ["board", "none", "未分类"],
    ];
    const scopeChips = scopes.map(([kind, id, label]) => `
      <span class="chip ${insightScope.kind === kind && insightScope.id === id ? "on" : ""}"
        data-scope="${kind}" data-scope-id="${esc(id)}">${esc(label)}</span>`).join("");

    const result = insightResult;
    const out = insightBusy ? `
      <div class="panel ins-out" id="ins-out">
        <div class="panel-body"><div class="ins-loading">
          <span class="ins-dot"></span><span class="ins-dot"></span><span class="ins-dot"></span>
          <span class="faint">正在读这批材料，换 ${esc(insightBusy)} 的眼镜看…</span>
        </div></div>
      </div>` : result ? `
      <div class="panel ins-out" id="ins-out">
        <div class="panel-head">
          <div class="panel-title">${esc(result.lens_name || "洞察")}</div>
          <div class="panel-sub">${esc(insightMeta(result))}${result.saved === false
            ? ` · <span class="err-text">未存进历史</span>` : ""}</div>
        </div>
        <div class="panel-body">
          <div class="insight-text">${insightParas(result.text)}</div>
          ${(result.used || []).length ? `<div class="used ins-used"><span class="f-label">读到的材料</span>
            ${result.used.slice(0, 12).map((u) => `<a class="chip used-chip" href="#/c/${u.id}">
              ${u.grade_label ? `<span class="g-badge g-${esc(u.grade || "unset")}">${esc(u.grade_label)}</span>` : ""}
              ${esc(u.title)}</a>`).join("")}</div>` : ""}
          <div class="filters" style="margin-top:12px">
            <button class="btn small ghost" id="ins-copy">复制</button>
            <button class="btn small ghost" id="ins-close">收起</button>
          </div>
        </div>
      </div>` : "";

    const lensCards = lensData.lenses.map((l) => `
      <button class="lens" data-lens="${esc(l.key)}" ${insightBusy ? "disabled" : ""}>
        <span class="lens-ico">${lensIcon(l.icon)}</span>
        <span class="lens-main">
          <span class="lens-name">${esc(l.name)}<em>by ${esc(l.by)}</em></span>
          <span class="lens-desc">${esc(l.desc)}</span>
        </span>
        <span class="lens-go">›</span>
      </button>`).join("");

    const history = hist.insights.length ? `
      <div class="section-head"><h2>过往洞察</h2><span>${hist.insights.length} 篇</span></div>
      ${hist.insights.map((h) => `
        <details class="ins-hist">
          <summary>
            <span class="ih-name">${esc(h.lens_name || "洞察")}</span>
            <span class="ih-meta">${esc(insightMeta(h))}</span>
            <span class="ih-del" data-del-ins="${h.id}" title="删掉这篇">×</span>
          </summary>
          <div class="insight-text">${insightParas(h.text)}</div>
        </details>`).join("")}` : "";

    main.innerHTML = head("洞察", "换一副眼镜看同一个库：八个视角，各问一个不同的问题")
      + `<div class="filters ins-scope">
          <span class="f-label">范围</span>
          <span class="chip on ins-scope-btn" id="scope-toggle">${esc(scopeLabel())}
            ${scopeCount() !== null ? ` · ${scopeCount()} 条` : ""} · 修改</span>
        </div>
        <div class="filters ins-scope-panel" id="scope-panel" hidden>
          ${scopeChips}
        </div>`
      + (lensData.ready ? "" : `<div class="panel"><div class="panel-body">
          <span class="err-text">AI 没配置。</span>
          <span class="faint">洞察全靠模型读你的材料，先去「设置」把端点、模型、key 填上。</span>
        </div></div>`)
      + out
      + `<div class="lens-list">${lensCards}</div>`
      + history;

    const toggle = $("#scope-toggle");
    toggle?.addEventListener("click", () => {
      const p = $("#scope-panel");
      if (p) p.hidden = !p.hidden;
    });
    $$("[data-scope]").forEach((el) => el.addEventListener("click", () => {
      insightScope = { kind: el.dataset.scope, id: el.dataset.scopeId };
      insightResult = null;
      viewInsight();
    }));

    $$("[data-lens]").forEach((el) => el.addEventListener("click", async () => {
      if (insightBusy) return;
      const key = el.dataset.lens;
      const lens = lensData.lenses.find((x) => x.key === key);
      insightBusy = lens ? lens.name : "洞察";
      insightResult = null;
      const mySeq = routeSeq;
      viewInsight();
      try {
        const r = await post("/api/insights/run", {
          lens: key, scope_kind: insightScope.kind, scope_id: insightScope.id,
        });
        insightResult = r;
        insightBusy = "";
        // 跑的过程中你可能已经翻到别处去了；别拿结果盖掉当前页面
        if (mySeq !== routeSeq) return;
        viewInsight();
        if (r.warning) toast(r.warning, "warn");
        setTimeout(() => $("#ins-out")?.scrollIntoView({ block: "start", behavior: "smooth" }), 60);
      } catch (e) {
        insightBusy = "";
        insightResult = null;
        if (mySeq !== routeSeq) return;
        await viewInsight();
        toast(e.message, "err");
      }
    }));

    $$("[data-del-ins]").forEach((el) => el.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!(await glConfirm("删掉这篇洞察？", { danger: true, confirmText: "删除" }))) return;
      try {
        await post(`/api/ai/outputs/${el.dataset.delIns}/delete`, {});
        toast("删掉了");
        viewInsight();
      } catch (err) { toast(err.message, "err"); }
    }));

    $("#ins-close")?.addEventListener("click", () => { insightResult = null; viewInsight(); });
    $("#ins-copy")?.addEventListener("click", async () => {
      if (!result) return;
      try {
        await navigator.clipboard.writeText(result.text);
        toast("洞察复制好了");
      } catch (_) { toast("复制不了，手动选一下", "warn"); }
    });
  }

  /* ══ 判断 ═══════════════════════════════════════════ */

  const CONF_LABEL = { high: "高", mid: "中", low: "低" };

  async function viewVerdicts() {
    const seq = routeSeq;
    const data = await api("/api/verdicts");
    if (seq !== routeSeq) return;

    const card = (v) => `
      <div class="verdict-card">
        <div class="verdict-top">
          <span class="conf conf-${esc(v.confidence)}">${CONF_LABEL[v.confidence] || "中"}</span>
          <a class="verdict-src" href="#/c/${v.clip_id}">${esc(v.title)}</a>
        </div>
        <div class="verdict-body">${esc(v.text)}</div>
        ${v.refs.length ? `<div class="verdict-refs"><span class="f-label">反方</span>
          ${v.refs.map((r) => `<a class="chip mini" href="#/c/${r.id}">${esc(r.title)}</a>`).join("")}
        </div>` : ""}
      </div>`;

    const groups = [["high", "高置信"], ["mid", "中等"], ["low", "低 / 存疑"]];
    const sections = groups.map(([key, label]) => {
      const list = data.verdicts.filter((v) => v.confidence === key);
      if (!list.length) return "";
      return `<div class="section-head"><h2>${label}</h2><span>${list.length} 条</span></div>
        ${list.map(card).join("")}`;
    }).join("");

    main.innerHTML = head("判断", `${data.verdicts.length} 条：你给这些材料下的结论`)
      + (data.verdicts.length ? sections
        : empty("还没有判断", "打开任意一条，在「我的判断」里写一句你认为它说明了什么"));
  }

  async function viewMore() {
    const seq = routeSeq;
    const [st, list, outputs, lan, peerState, cloudState] = await Promise.all([
      api("/api/ai/status"), api("/api/clips?limit=1"), api("/api/ai/outputs"),
      api("/api/lan").catch(() => null),
      api("/api/sync/peer/status").catch(() => null),
      api("/api/sync/cloud").catch(() => null),
    ]);
    if (seq !== routeSeq) return;

    const needsPrivacyChoice = (cloudState && !cloudState.explicit_choice)
      || (lan && lan.mode && !lan.explicit_choice);
    main.innerHTML = head("设置", "只剩这几件事")
      + (needsPrivacyChoice ? panel("安全默认", "先确认两个外部访问开关", `
        <div class="prose">
          <p>技术评审后，云同步与手机访问都<strong>默认关闭</strong>：不会自动把资料库推到云端，
          也不会默认向同一网段开放。第一次使用时请明确选择一次；之后仍可在下面各自的面板随时开关。</p>
        </div>
        <div class="filters" style="margin-top:12px">
          <button class="btn small" id="privacy-keep-off">保持关闭（推荐）</button>
          <button class="btn small ghost" id="privacy-cloud-on">打开云同步…</button>
          ${lan && lan.mode ? `<button class="btn small ghost" id="privacy-lan-on">打开手机访问…</button>` : ""}
        </div>
        <div class="faint">云同步开启后，<span class="mono">data/cloud.json</span> 里的
          <span class="mono">secret</span> 等价于这份资料库的读权限，别把它当普通配置分享出去。</div>`) : "")
      + panel("AI", st.channel, `
        <div class="kv"><span class="k">状态</span><span class="v">${st.ready
          ? `<span class="ok-dot"></span>可用` : `<span class="err-text">未配置</span>`}</span></div>
        <div class="kv"><span class="k">模型</span><span class="v mono">${esc(st.model || "—")}</span></div>
        <div class="kv"><span class="k">端点</span><span class="v mono">${esc(st.endpoint || "—")}</span></div>
        ${st.config_error ? `<div class="kv"><span class="k">问题</span>
          <span class="v err-text">${esc(st.config_error)}</span></div>` : ""}
        <div class="filters" style="margin-top:14px">
          ${st.ready ? `<button class="btn small ghost" id="ai-test">测一下通不通</button>` : ""}
        </div>
        <div class="faint">换模型、换 key 改环境变量后重启服务：GL_AI_BASE_URL / GL_AI_MODEL / GL_AI_KEY。</div>`)
      + panel("库", `共 ${list.total} 条 · 研究记录 ${outputs.outputs.length} 条`, `
        <div class="filters">
          <button class="btn small ghost" id="exp">导出 JSON</button>
        </div>
        <div class="faint">主库 <span class="mono">data/guanlan.db</span>，上传的文件在
          <span class="mono">data/uploads</span>。完整备份：先停服务，再整个复制 data 目录。
          停服务用 <span class="mono">start.cmd stop</span>，或者关掉窗口后重开图标。</div>`)
      + panel("局域网同步", !peerState ? "读不到"
          : (!peerState.available ? "这台是主机"
            : (peerState.last_error ? "等待发现电脑" : (peerState.synced_at ? "已连接" : "待首次同步"))),
        !peerState ? `<div class="faint">同步状态暂时读不到。</div>`
        : !peerState.available ? `<div class="faint">这台设备作为同步主机。手机与电脑连同一个 WiFi，
             打开手机端观澜后会自动发现本机；刷新或等待约 25 秒即可同步。</div>`
        : `<div class="kv"><span class="k">电脑</span><span class="v">${esc(peerState.host || "未发现")}</span></div>
           <div class="kv"><span class="k">上次</span><span class="v">${esc(peerState.synced_at || "尚未同步")}</span></div>
           ${peerState.last_error ? `<div class="kv"><span class="k">状态</span>
             <span class="v err-text">${esc(peerState.last_error)}</span></div>` : ""}
           <div class="filters" style="margin-top:12px">
             <button class="btn small ghost" id="peer-sync-now">立即同步</button>
           </div>
           <div class="faint">自动同步只在同一 WiFi 下工作。两端同时改过时，手机改动会另存为冲突快照。</div>`)
      + panel("云同步", !cloudState ? "读不到"
          : (!cloudState.enabled ? "已关闭"
            : (cloudState.last_error ? "有问题" : (cloudState.synced_at ? "已连接云端" : "待首次同步"))),
        !cloudState ? `<div class="faint">云同步状态暂时读不到。</div>`
        : `<div class="kv"><span class="k">云端</span><span class="v mono">${esc(String(cloudState.endpoint || "").replace(/^https?:\/\//, ""))}</span></div>
           <div class="kv"><span class="k">上次</span><span class="v">${esc(cloudState.synced_at || "尚未同步")}</span></div>
           <div class="kv"><span class="k">云端版本</span><span class="v mono">${Number(cloudState.revision) || 0}</span></div>
           ${cloudState.last_error ? `<div class="kv"><span class="k">状态</span>
             <span class="v err-text">${esc(cloudState.last_error)}</span></div>` : ""}
           ${cloudState.last_action === "conflict_pulled" ? `<div class="kv"><span class="k">注意</span>
             <span class="v">两端同时改过，本机改动已另存冲突快照（cloud-sync/conflicts）</span></div>` : ""}
           <div class="filters" style="margin-top:12px">
             <button class="btn small ghost" id="cloud-sync-now">立即同步</button>
             <button class="btn small ghost" id="cloud-sync-toggle">${cloudState.enabled ? "关闭云同步" : "打开云同步"}</button>
             <button class="btn small ghost danger" id="cloud-sync-reset">重置同步进度</button>
           </div>
           <div class="faint">走 WorkBuddy 云服务，出门没有同一 WiFi 也能同步。两端同时改过时，本机改动另存冲突快照，云端优先。</div>`)
      + panel("手机访问", !lan ? "未知" : (lan.mode ? (lan.enabled ? "已开启" : "关着") : "只本机"),
        !lan ? `<div class="faint">读不到状态，服务可能没起来。</div>`
        : !lan.mode ? `<div class="faint">服务是以「只本机」方式启动的，别的设备连不进来。
             要开就用 <span class="mono">--lan</span> 启动，或者设
             <span class="mono">GL_LAN=1</span> 再重启一次。之后这里的开关就不用再重启了。</div>`
        : `<div class="lan-box">
             <div class="lan-row"><span class="k">地址</span>
               <span class="v mono" id="lan-url">${esc(lan.url)}</span></div>
             ${lan.enabled && lan.key ? `<div class="lan-row"><span class="k">访问码</span>
               <span class="v mono lan-key" id="lan-key">${esc(lan.key)}</span></div>` : ""}
             ${lan.enabled && !lan.key ? `<div class="lan-row"><span class="k">访问码</span>
               <span class="v">只在电脑端显示</span></div>` : ""}
           </div>
           <div class="filters" style="margin-top:12px">
             ${lan.enabled ? `<button class="btn small ghost" id="lan-copy">复制地址</button>
               ${lan.key ? `<button class="btn small ghost" id="lan-copy-key">复制访问码</button>` : ""}
               <button class="btn small ghost danger" id="lan-toggle">关掉</button>`
             : `<button class="btn small" id="lan-toggle">打开手机访问</button>`}
           </div>
           <div class="faint">${lan.enabled
             ? `手机连<strong>同一个 WiFi</strong>，浏览器打开上面地址，通过 POST 表单输一次码就行（之后记住）。
                登录后会拿到 HttpOnly 随机会话令牌，访问码不再长期放在 cookie 或 URL 里。
                打不开多半是 Windows 防火墙拦了 python——允许一次「专用网络」即可。
                在手机上「添加到主屏幕」，就是一个独立图标。`
             : `打开之后，同一个 WiFi 下的手机就能连；每次连要输六位访问码。
                只在家里/办公室这种可信网络开，公共 WiFi 别开。`}</div>`)
      + panel("外观", "深浅由你", `
        <div class="filters">
          <button class="btn small ghost" id="th-dark">用深色</button>
          <button class="btn small ghost" id="th-light">用浅色</button>
          <button class="btn small ghost" id="th-auto">跟系统</button>
        </div>
        <div class="faint">选择记在这台设备上；「跟系统」= 每次打开时跟随系统深浅设置。
          侧栏底部也有一个开关。Ctrl/⌘+K 唤起快速跳转，/ 聚焦搜索框。</div>`)
      + panel("说明", "", `
        <div class="prose">
          <p>这个界面只做三件事：<strong>收存</strong>你判断重要的东西，<strong>问</strong>它们，
          <strong>回顾</strong>这段时间收了什么。正典、预测、决策、时序那些表还完整地躺在库里，
          没有删，只是不再出现在界面上。</p>
          <p>网页链接<strong>只存标题和地址</strong>，不抓正文，点「打开原链接」去读原文。
          导入的文件不一样：正文会从原件里抽出来存着，详情页直接能看，也能给 AI 用。</p>
          <p>条目在库里叫 clips，<span class="mono">/api/clips/export</span> 能整包导出成 JSON。</p>
        </div>`);

    $("#privacy-keep-off")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        await post("/api/sync/cloud", { enabled: false, explicit_choice: true });
        if (lan && lan.mode) await post("/api/lan", { enabled: false, explicit_choice: true });
        toast("已记录选择：云同步和手机访问保持关闭");
        viewMore();
      } catch (e) { toast(e.message, "err"); }
    }));

    $("#privacy-cloud-on")?.addEventListener("click", async (ev) => {
      const yes = await glDialog({
        message: "确认打开云同步？开启后整库快照会上传到配置的云端，data/cloud.json 里的 secret 等价于库的读权限。",
        confirmText: "打开云同步", danger: true,
      });
      if (!yes) return;
      try {
        await post("/api/sync/cloud", { enabled: true, explicit_choice: true });
        toast("云同步已打开");
      } catch (e) { toast(e.message, "err"); }
      viewMore();
    });

    $("#privacy-lan-on")?.addEventListener("click", async (ev) => {
      if (!lan || !lan.mode) {
        toast("当前服务只绑定本机，先带 --lan 重启才能打开手机访问", "warn");
        return;
      }
      const yes = await glDialog({
        message: "确认打开手机访问？同一 WiFi 下的设备需要六位访问码；默认只建议在可信网络开启。",
        confirmText: "打开手机访问", danger: true,
      });
      if (!yes) return;
      try {
        await post("/api/lan", { enabled: true, explicit_choice: true });
        toast("手机访问已打开，访问码只在电脑端显示");
      } catch (e) { toast(e.message, "err"); }
      viewMore();
    });

    async function copyText(text, okMsg) {
      try {
        await navigator.clipboard.writeText(text);
        toast(okMsg);
      } catch (_) {
        // 剪贴板要安全上下文，局域网 http 下会失败：退回到选中
        const box = document.createElement("textarea");
        box.value = text;
        box.style.position = "fixed";
        box.style.opacity = "0";
        document.body.appendChild(box);
        box.select();
        try { document.execCommand("copy"); toast(okMsg); }
        catch (e) { toast("复制不了，手动抄一下", "warn"); }
        box.remove();
      }
    }
    $("#lan-copy")?.addEventListener("click", () => copyText($("#lan-url").textContent, "地址复制好了"));
    $("#lan-copy-key")?.addEventListener("click", () =>
      copyText($("#lan-key").textContent, "访问码复制好了"));
    $("#lan-toggle")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        const r = await post("/api/lan", { enabled: !(lan && lan.enabled) });
        toast(r.enabled ? "手机访问打开了" : "手机访问关掉了");
        viewMore();
      } catch (e) { toast(e.message, "err"); }
    }));

    $("#peer-sync-now")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      await peerSyncTick(true);
      viewMore();
    }));

    $("#cloud-sync-now")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        const r = await post("/api/sync/cloud/run", {});
        if (r && r.available === false) { toast(r.reason || "云同步没开", "warn"); return; }
        toast("正在同步云端，稍后看状态");
      } catch (e) { toast(e.message, "err"); }
      setTimeout(viewMore, 4000);
    }));
    $("#cloud-sync-toggle")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        const cur = cloudState || {};
        await post("/api/sync/cloud", { enabled: !cur.enabled });
        toast(cur.enabled ? "云同步关掉了" : "云同步打开了");
      } catch (e) { toast(e.message, "err"); }
      viewMore();
    }));
    $("#cloud-sync-reset")?.addEventListener("click", async (ev) => {
      const yes = await glDialog({
        message: "重置云同步进度？只清掉本机记录的「已同步到哪个版本」，不动资料库，也不动云端。下次同步会重新和云端对齐。",
        danger: true,
        confirmText: "重置",
      });
      if (!yes) return;
      try {
        await post("/api/sync/cloud/reset", {});
        toast("云同步进度已重置，下次同步重新对齐");
      } catch (e) { toast(e.message, "err"); }
      viewMore();
    });

    $("#th-dark")?.addEventListener("click", () => setTheme("dark"));
    $("#th-light")?.addEventListener("click", () => setTheme("light"));
    $("#th-auto")?.addEventListener("click", themeFollowSystem);

    $("#ai-test")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        const r = await post("/api/ai/test", {});
        toast(`通了：${r.model}`);
      } catch (e) { toast(e.message, "err"); }
    }));
    $("#exp").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const data = await api("/api/clips/export");
      downloadJSON(data, `guanlan-${today()}.json`);
      toast(`导出了 ${data.clips.length} 条`);
    }));
  }

  /* ── 应用内对话框：替代原生 confirm / prompt ──────────
     同一瞬间只活一个，后来的排队；Esc/点背景=取消，Enter=确认；
     关掉时把焦点还给打开前的地方。返回 Promise：
     confirm 版 resolve true/false，prompt 版 resolve 字符串或 null。 */
  let dialogChain = Promise.resolve();

  function glDialog(opts) {
    const { message = "", confirmText = "确定", cancelText = "取消",
            danger = false, input = null, placeholder = "" } = opts || {};
    const run = () => new Promise((resolve) => {
      const prev = document.activeElement;
      const backdrop = document.createElement("div");
      backdrop.className = "gl-modal-backdrop";
      const box = document.createElement("div");
      box.className = "gl-modal";
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "true");
      const msg = document.createElement("div");
      msg.className = "gl-modal-msg";
      msg.textContent = message;          // 标题里常带用户起的名字，走 textContent 防注入
      box.appendChild(msg);
      let field = null;
      if (input !== null) {
        field = document.createElement("input");
        field.type = "text";
        field.className = "gl-modal-input";
        field.value = input;
        field.placeholder = placeholder;
        field.maxLength = 40;
        box.appendChild(field);
      }
      const acts = document.createElement("div");
      acts.className = "gl-modal-acts";
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn small ghost";
      cancelBtn.textContent = cancelText;
      const okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = `btn small${danger ? " danger-solid" : ""}`;
      okBtn.textContent = confirmText;
      acts.append(cancelBtn, okBtn);
      box.appendChild(acts);
      backdrop.appendChild(box);
      document.body.appendChild(backdrop);

      const cancelValue = input !== null ? null : false;
      let done = false;
      const close = (value) => {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey, true);
        backdrop.classList.add("out");
        setTimeout(() => backdrop.remove(), 150);
        if (prev && prev.isConnected) prev.focus();
        resolve(value);
      };
      const ok = () => close(field ? field.value : true);
      const onKey = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(cancelValue); }
        else if (e.key === "Enter" && e.target !== cancelBtn) {
          e.preventDefault(); e.stopPropagation(); ok();
        } else if (e.key === "Tab") {
          // 焦点圈在框里：field → 取消 → 确定 → 回到 field
          const order = [field, cancelBtn, okBtn].filter(Boolean);
          const i = order.indexOf(document.activeElement);
          e.preventDefault();
          order[e.shiftKey
            ? (i <= 0 ? order.length - 1 : i - 1)
            : (i === order.length - 1 ? 0 : i + 1)].focus();
        }
      };
      document.addEventListener("keydown", onKey, true);
      backdrop.addEventListener("mousedown", (e) => {
        if (e.target === backdrop) close(cancelValue);
      });
      cancelBtn.addEventListener("click", () => close(cancelValue));
      okBtn.addEventListener("click", ok);
      (field || okBtn).focus();
      if (field) field.select();
    });
    dialogChain = dialogChain.then(run, run);
    return dialogChain;
  }

  const glConfirm = (message, opts = {}) => glDialog({
    message,
    danger: !!opts.danger,
    confirmText: opts.confirmText || "确定",
  });

  const glPrompt = (message, value = "") => glDialog({
    message, input: value, confirmText: "保存",
  });

  /* ── 深色 / 浅色 ────────────────────────────────────── */

  function applyThemeClass(t) {
    if (t === "dark") document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
    const btn = $("#theme-toggle");
    if (btn) btn.textContent = t === "dark" ? "浅色" : "深色";
  }

  function setTheme(t) {
    try { localStorage.setItem("gl_theme", t); } catch (_) { /* 存不下就只对本页生效 */ }
    applyThemeClass(t);
    toast(t === "dark" ? "换成深色了" : "换成浅色了");
  }

  function themeFollowSystem() {
    try { localStorage.removeItem("gl_theme"); } catch (_) { /* 同上 */ }
    applyThemeClass(currentThemePref());
    toast("会跟着系统走");
  }

  /* ── 命令面板：Ctrl/⌘+K 唤起 ─────────────────────────
     跳页面、跳板块、搜条目、切主题。纯前端增量，
     只读 /api/search 和 boardsCache，不碰任何数据流。 */
  const paletteState = { el: null, seq: 0, sel: 0, items: [], prevFocus: null };

  function paletteCommands() {
    const cmds = [
      ["收存 · 全部", "clips home 全部", "#/"],
      ["未分类", "未分类 none", "#/b/none"],
      ["问", "ask 问答 提问", "#/ask"],
      ["检索", "search 搜索 查找", "#/search"],
      ["判断", "verdicts 判断 立场", "#/verdicts"],
      ["洞察", "insight 洞察 视角", "#/insight"],
      ["回顾", "review 回顾 总结", "#/review"],
      ["设置", "settings 设置", "#/more"],
    ].map(([label, kw, hash]) => ({ label, kw, kind: "",
      go: () => { location.hash = hash; } }));
    cmds.push({
      label: document.documentElement.dataset.theme === "dark" ? "切换到浅色" : "切换到深色",
      kw: "theme dark light 深色 浅色 主题", kind: "主题",
      go: () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"),
    });
    for (const b of boardsCache) {
      cmds.push({ label: b.name, kw: b.kind === "archive" ? "资料库" : "板块",
        kind: b.kind === "archive" ? "资料库" : "板块",
        go: () => { location.hash = `#/b/${b.id}`; } });
    }
    return cmds;
  }

  function palettePaint() {
    const host = paletteState.el?.querySelector(".pal-list");
    if (!host) return;
    const items = paletteState.items;
    if (paletteState.sel >= items.length) paletteState.sel = Math.max(0, items.length - 1);
    if (!items.length) {
      host.innerHTML = `<div class="pal-empty">没有匹配的。去「检索」页能搜到正文里去。</div>`;
      return;
    }
    let lastType = "";
    host.innerHTML = items.map((it, i) => {
      const group = it.type === "clip" ? "条目" : "跳转";
      const gh = group !== lastType ? `<div class="pal-group">${group}</div>` : "";
      lastType = group;
      return `${gh}<div class="pal-item${i === paletteState.sel ? " on" : ""}" data-pi="${i}">
        <span class="pal-label">${esc(it.label)}</span>
        ${it.kind ? `<span class="pal-kind">${esc(it.kind)}</span>` : ""}</div>`;
    }).join("");
    $$(".pal-item", host).forEach((el) => {
      el.addEventListener("click", () => paletteRun(Number(el.dataset.pi)));
      el.addEventListener("mousemove", () => {
        const i = Number(el.dataset.pi);
        if (paletteState.sel !== i) { paletteState.sel = i; palettePaint(); }
      });
    });
    $(".pal-item.on", host)?.scrollIntoView({ block: "nearest" });
  }

  function paletteRender(q) {
    const mySeq = ++paletteState.seq;   // 任何一次调用（含清空搜索词）都作废上一次请求
    const query = String(q || "").trim().toLowerCase();
    const cmds = paletteCommands().filter((c) => !query
      || c.label.toLowerCase().includes(query)
      || c.kw.toLowerCase().includes(query));
    paletteState.items = cmds.map((c) => ({ type: "cmd", label: c.label, kind: c.kind, go: c.go }));
    palettePaint();
    if (!query) return;
    api(`/api/search?q=${encodeURIComponent(q.trim())}&limit=8`).then((r) => {
      if (mySeq !== paletteState.seq || !paletteState.el) return;
      const clips = (r.results || []).map((c) => ({
        type: "clip", label: c.title, kind: "条目", id: c.id,
        go: () => { location.hash = `#/c/${c.id}`; },
      }));
      paletteState.items = paletteState.items.concat(clips);
      palettePaint();
    }).catch(() => { /* 搜不出来就只留命令，不报错 */ });
  }

  function paletteRun(i) {
    const it = paletteState.items[i];
    paletteClose();
    if (it) it.go();
  }

  function paletteOpen() {
    if (paletteState.el && paletteState.el.isConnected) return;
    if ($(".gl-modal-backdrop")) return;      // 对话框开着就别抢层
    paletteState.prevFocus = document.activeElement;
    paletteState.sel = 0;
    const backdrop = document.createElement("div");
    backdrop.className = "pal-backdrop";
    backdrop.innerHTML = `<div class="pal" role="dialog" aria-modal="true" aria-label="快速跳转">
        <input class="pal-input" type="text" maxlength="120" spellcheck="false"
          placeholder="跳到某个页面、板块，或搜一条收存…">
        <div class="pal-list"></div>
        <div class="pal-foot"><span class="kbd">↑↓</span> 选<span class="kbd">Enter</span> 去<span class="kbd">Esc</span> 关</div>
      </div>`;
    document.body.appendChild(backdrop);
    paletteState.el = backdrop;
    const input = $(".pal-input", backdrop);
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { paletteState.sel = 0; paletteRender(input.value); }, 160);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); paletteClose(); }
      else if (e.key === "ArrowDown") {
        e.preventDefault();
        paletteState.sel = Math.min(paletteState.sel + 1, paletteState.items.length - 1);
        palettePaint();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        paletteState.sel = Math.max(paletteState.sel - 1, 0);
        palettePaint();
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (!paletteState.items.length) { paletteClose(); return; }
        paletteRun(paletteState.sel);
      }
    });
    backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) paletteClose(); });
    paletteRender("");
    input.focus();
  }

  function paletteClose() {
    const el = paletteState.el;
    paletteState.el = null;
    paletteState.seq++;                       // 作废还在路上的搜索回调
    if (!el) return;
    el.classList.add("out");
    setTimeout(() => el.remove(), 150);
    if (paletteState.prevFocus && paletteState.prevFocus.isConnected) {
      paletteState.prevFocus.focus();
    }
  }

  /* ── 全局键盘：Ctrl/⌘+K 面板，/ 聚焦搜索 ───────────── */

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "k") {
      e.preventDefault();
      if ($(".gl-modal-backdrop")) return;    // 对话框优先
      if (paletteState.el && paletteState.el.isConnected) paletteClose();
      else paletteOpen();
      return;
    }
    if (paletteState.el && paletteState.el.isConnected) return;
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && typeof t.closest === "function"
      && t.closest("input, textarea, select, [contenteditable]")) return;
    const box = $("#v-search") || $("#s-q");
    if (box && box.isConnected) { e.preventDefault(); box.focus(); box.select?.(); }
    else { e.preventDefault(); paletteOpen(); }
  });

  /* ── 给阅读中心暴露的共用能力 ────────────────────────
     reading.js 是独立文件，但它要复用同一套 api / 正文渲染 / 标注锚定 /
     对话框，不能复制一份。这里只导出，不改变原有行为。 */
  window.GL = {
    $, $$, esc, api, post, toast, glDialog, glConfirm, glPrompt, withBusy,
    fmtDate, fmtSize, empty, markdownHtml, mdBody, mdPlain, hasMarkdown, bindMdPreview,
    readerHtml, bodyBlocks, paragraphHtml, prepareAnnotations,
    offsetInBlock, blockOfNode, annotationRailHtml,
    ANNOTATION_COLORS, annColor,
    addRoute: (re, fn) => ROUTES.push([re, fn]),
    routeNow: () => route(),
    refreshSidebar,
    currentPath,
  };

  /* ── 启动 ──────────────────────────────────────────── */

  applyThemeClass(currentThemePref());
  $("#theme-toggle")?.addEventListener("click", () =>
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#side-k")?.addEventListener("click", () => paletteOpen());

  $$(".nav-item").forEach((el) => el.addEventListener("click", () => {
    // 二级导航用 <a href> 直达子路由，别被这里改写成一级路由
    if (el.dataset.route === undefined || el.tagName === "A") return;
    location.hash = "#/" + el.dataset.route;
  }));
  bindBoardAdd();
  window.addEventListener("hashchange", route);
  if (!location.hash) location.hash = "#/";
  // 让 reading.js 先注册完路由再跑首屏；否则刷新在 #/reading 上会被弹回收存
  setTimeout(route, 0);
  startLiveSync();
  try {
    if (sessionStorage.getItem("gl_peer_conflict") === "1") {
      sessionStorage.removeItem("gl_peer_conflict");
      setTimeout(() => toast("两端都改过，已采用云端最新版；本机改动留了冲突备份", "warn"), 700);
    }
  } catch (_) { }
  setTimeout(peerSyncTick, 1600);
  setInterval(() => {
    if (!document.hidden && !liveSync.pending && !peerSync.busy) peerSyncTick();
  }, 25000);
  setTimeout(() => cloudSyncTick(true), 4000);
  setInterval(() => {
    if (!document.hidden && !liveSync.pending && !cloudSync.running) cloudSyncTick();
  }, 60000);
})();
