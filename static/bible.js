/* 观澜 GUANLAN · 圣经阅读地（独立大板块）
   ------------------------------------------------------------------
   分卷分章读经 → 五色划重点 / 批注 / 书签 → 灵修笔记 → 全文检索 →
   AI 伴读 → 经文导入与导出。
   本文件是独立 IIFE：只复用 app.js 导出的 window.GL，不改动任何现有文件。
   路由经 G.addRoute 注册，板块外壳（body.bl-active / 侧栏 / sect-switch）
   由 GLBible.onRoute(path) 负责；主 agent 在 app.js 的 route() 里调用它。
   所有新增 class 一律 bl- 前缀，样式见 bible.css。 */
(() => {
  "use strict";

  const G = window.GL;
  if (!G) return;
  const {
    $, $$, esc, api, post, toast, glConfirm, glPrompt, markdownHtml, bindMdPreview,
  } = G;
  if (typeof $ !== "function" || typeof $$ !== "function" || typeof api !== "function"
    || typeof post !== "function" || typeof esc !== "function") return;

  const main = $("#main");
  if (!main) return;

  /* 只用确实导出的成员；对可选能力做兜底，避免 undefined 调用。 */
  const addRoute = typeof G.addRoute === "function" ? G.addRoute : () => { };
  const routeNow = typeof G.routeNow === "function" ? G.routeNow : () => {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  };
  const toastIt = typeof toast === "function" ? toast : () => { };
  const mdHtml = typeof markdownHtml === "function" ? markdownHtml
    : (s) => esc(s || "").replace(/\n/g, "<br>");
  const bindMd = typeof bindMdPreview === "function" ? bindMdPreview : () => { };
  const confirmIt = typeof glConfirm === "function" ? glConfirm
    : (m) => Promise.resolve(!!window.confirm(String(m)));
  const promptIt = typeof glPrompt === "function" ? glPrompt
    : (m, v) => Promise.resolve(window.prompt(String(m), v == null ? "" : String(v)));

  /* G.api() 已完成「非 2xx / 2xx+error / 2xx+ok」分层：内部错误（含 ref）
     与业务校验错误都会被抛成 Error。bible.js 不按状态码分支，也不假设
     某个接口的错误一定是 200 + 文本；这里统一 toast 一次再上抛。 */
  const fail = (e) => {
    /* 打标避免「req/send 已弹过 + 事件分发外层再弹一次」的双 toast */
    try { if (e && typeof e === "object") e.__blToasted = true; } catch (_) { }
    toastIt((e && e.message) || String(e), "err");
    throw e;
  };
  const req = (p) => api(p).catch(fail);
  const send = (p, b) => post(p, b).catch(fail);

  /* ── 常量 ───────────────────────────────────────────── */

  const COLORS = {
    amber: { label: "重点", hint: "值得留下" },
    sage: { label: "论据", hint: "可作支撑" },
    blue: { label: "疑问", hint: "待核实" },
    rose: { label: "反驳", hint: "有异议" },
    violet: { label: "联想", hint: "由此及彼" },
  };
  const COLOR_KEYS = Object.keys(COLORS);
  /* 后端缺省色是 yellow；把常见别名归到同一套五色，避免显示成「未知颜色」 */
  const COLOR_ALIAS = {
    yellow: "amber", gold: "amber", orange: "amber",
    green: "sage", teal: "sage",
    purple: "violet", indigo: "violet",
    pink: "rose", red: "rose",
    cyan: "blue",
  };
  const safeColor = (c) => (COLORS[c] ? c : (COLOR_ALIAS[c] || "amber"));

  const PAPERS = [
    ["paper", "白纸"], ["sepia", "牛皮纸"], ["night", "夜间"],
  ];
  const PREF_KEY = "gl_bible_prefs";
  const loadPrefs = () => {
    try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch (_) { return {}; }
  };
  const P = Object.assign({
    fontSize: 18, paper: "paper", leftOpen: true,
    rightOpen: window.innerWidth > 1100,
  }, loadPrefs());
  const savePrefs = () => {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(P)); } catch (_) { }
  };
  const narrow = () => window.matchMedia("(max-width: 900px)").matches;

  /* 66 卷基准表：id / 简体名 / 繁体名 / 简称 / 额外别名 / 章数 / 旧约。
     只用于导入时的卷名归一、接口暂不可用时的兜底目录与显示。
     运行期以 /api/bible/state 返回的 books 为准。 */
  const CANON = [
    ["gen", "创世记", "創世記", "创", "genesis|gen|创世|1moses", 50, 1],
    ["exo", "出埃及记", "出埃及記", "出", "exodus|exo|出埃及", 40, 1],
    ["lev", "利未记", "利未記", "利", "leviticus|lev|利未", 27, 1],
    ["num", "民数记", "民數記", "民", "numbers|num|民数", 36, 1],
    ["deu", "申命记", "申命記", "申", "deuteronomy|deu|申命", 34, 1],
    ["jos", "约书亚记", "約書亞記", "书", "joshua|jos|约书亚", 24, 1],
    ["jdg", "士师记", "士師記", "士", "judges|jdg|士师", 21, 1],
    ["rut", "路得记", "路得記", "得", "ruth|rut|路得", 4, 1],
    ["1sa", "撒母耳记上", "撒母耳記上", "撒上", "1samuel|1sam|1sa|撒上|撒母耳上", 31, 1],
    ["2sa", "撒母耳记下", "撒母耳記下", "撒下", "2samuel|2sam|2sa|撒下|撒母耳下", 24, 1],
    ["1ki", "列王纪上", "列王紀上", "王上", "1kings|1kgs|1ki|王上|列王上", 22, 1],
    ["2ki", "列王纪下", "列王紀下", "王下", "2kings|2kgs|2ki|王下|列王下", 25, 1],
    ["1ch", "历代志上", "歷代志上", "代上", "1chronicles|1chr|1ch|代上|历代上", 29, 1],
    ["2ch", "历代志下", "歷代志下", "代下", "2chronicles|2chr|2ch|代下|历代下", 36, 1],
    ["ezr", "以斯拉记", "以斯拉記", "拉", "ezra|ezr|以斯拉", 10, 1],
    ["neh", "尼希米记", "尼希米記", "尼", "nehemiah|neh|尼希米", 13, 1],
    ["est", "以斯帖记", "以斯帖記", "斯", "esther|est|以斯帖", 10, 1],
    ["job", "约伯记", "約伯記", "伯", "job|约伯", 42, 1],
    ["psa", "诗篇", "詩篇", "诗", "psalms|psalm|psa|ps|诗", 150, 1],
    ["pro", "箴言", "箴言", "箴", "proverbs|pro|prov", 31, 1],
    ["ecc", "传道书", "傳道書", "传", "ecclesiastes|ecc|qoh|传道", 12, 1],
    ["sng", "雅歌", "雅歌", "歌", "songofsongs|songs|song|sng|sos|canticles", 8, 1],
    ["isa", "以赛亚书", "以賽亞書", "赛", "isaiah|isa|以赛亚", 66, 1],
    ["jer", "耶利米书", "耶利米書", "耶", "jeremiah|jer|耶利米", 52, 1],
    ["lam", "耶利米哀歌", "耶利米哀歌", "哀", "lamentations|lam|哀歌", 5, 1],
    ["ezk", "以西结书", "以西結書", "结", "ezekiel|ezk|eze|以西结", 48, 1],
    ["dan", "但以理书", "但以理書", "但", "daniel|dan|但以理", 12, 1],
    ["hos", "何西阿书", "何西阿書", "何", "hosea|hos|何西阿", 14, 1],
    ["jol", "约珥书", "約珥書", "珥", "joel|jol|joe|约珥", 3, 1],
    ["amo", "阿摩司书", "阿摩司書", "摩", "amos|amo|阿摩司", 9, 1],
    ["oba", "俄巴底亚书", "俄巴底亞書", "俄", "obadiah|oba|oba|俄巴底亚", 1, 1],
    ["jon", "约拿书", "約拿書", "拿", "jonah|jon|约拿", 4, 1],
    ["mic", "弥迦书", "彌迦書", "弥", "micah|mic|弥迦", 7, 1],
    ["nam", "那鸿书", "那鴻書", "鸿", "nahum|nam|nah|那鸿", 3, 1],
    ["hab", "哈巴谷书", "哈巴谷書", "哈", "habakkuk|hab|哈巴谷", 3, 1],
    ["zep", "西番雅书", "西番雅書", "番", "zephaniah|zep|西番雅", 3, 1],
    ["hag", "哈该书", "哈該書", "该", "haggai|hag|哈该", 2, 1],
    ["zec", "撒迦利亚书", "撒迦利亞書", "亚", "zechariah|zec|撒迦利亚", 14, 1],
    ["mal", "玛拉基书", "瑪拉基書", "玛", "malachi|mal|玛拉基", 4, 1],
    ["mat", "马太福音", "馬太福音", "太", "matthew|mat|matt|马太", 28, 0],
    ["mrk", "马可福音", "馬可福音", "可", "mark|mrk|mar|马可", 16, 0],
    ["luk", "路加福音", "路加福音", "路", "luke|luk|路加", 24, 0],
    ["jhn", "约翰福音", "約翰福音", "约", "john|jhn|joh|约翰", 21, 0],
    ["act", "使徒行传", "使徒行傳", "徒", "acts|act|使徒", 28, 0],
    ["rom", "罗马书", "羅馬書", "罗", "romans|rom|罗马", 16, 0],
    ["1co", "哥林多前书", "哥林多前書", "林前", "1corinthians|1cor|1co|哥前|林前", 16, 0],
    ["2co", "哥林多后书", "哥林多後書", "林后", "2corinthians|2cor|2co|哥后|林后|林後", 13, 0],
    ["gal", "加拉太书", "加拉太書", "加", "galatians|gal|加拉太", 6, 0],
    ["eph", "以弗所书", "以弗所書", "弗", "ephesians|eph|以弗所", 6, 0],
    ["php", "腓立比书", "腓立比書", "腓", "philippians|php|phi|腓立比", 4, 0],
    ["col", "歌罗西书", "歌羅西書", "西", "colossians|col|歌罗西", 4, 0],
    ["1th", "帖撒罗尼迦前书", "帖撒羅尼迦前書", "帖前", "1thessalonians|1thess|1th|帖前", 5, 0],
    ["2th", "帖撒罗尼迦后书", "帖撒羅尼迦後書", "帖后", "2thessalonians|2thess|2th|帖后|帖後", 3, 0],
    ["1ti", "提摩太前书", "提摩太前書", "提前", "1timothy|1tim|1ti|提前", 6, 0],
    ["2ti", "提摩太后书", "提摩太後書", "提后", "2timothy|2tim|2ti|提后|提後", 4, 0],
    ["tit", "提多书", "提多書", "多", "titus|tit|提多", 3, 0],
    ["phm", "腓利门书", "腓利門書", "门", "philemon|phm|腓利门", 1, 0],
    ["heb", "希伯来书", "希伯來書", "来", "hebrews|heb|希伯来", 13, 0],
    ["jas", "雅各书", "雅各書", "雅", "james|jas|雅各", 5, 0],
    ["1pe", "彼得前书", "彼得前書", "彼前", "1peter|1pet|1pe|彼前", 5, 0],
    ["2pe", "彼得后书", "彼得後書", "彼后", "2peter|2pet|2pe|彼后|彼後", 3, 0],
    ["1jn", "约翰一书", "約翰一書", "约一", "1john|1jn|1joh|约一|約一", 5, 0],
    ["2jn", "约翰二书", "約翰二書", "约二", "2john|2jn|2joh|约二|約二", 1, 0],
    ["3jn", "约翰三书", "約翰三書", "约三", "3john|3jn|3joh|约三|約三", 1, 0],
    ["jud", "犹大书", "猶大書", "犹", "jude|jud|犹大|猶大", 1, 0],
    ["rev", "启示录", "啟示錄", "启", "revelation|rev|apocalypse|启示|啟示", 22, 0],
  ];
  const BOOKS = CANON.map((b, i) => ({
    id: b[0], name: b[1], trad: b[2], abbr: b[3], aliases: b[4],
    chapters: b[5], ot: !!b[6], order: i + 1,
  }));

  const normKey = (s) => String(s == null ? "" : s)
    .toLowerCase()
    .replace(/[\uFF01-\uFF5E]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    .replace(/[\s\u3000]/g, "")
    .replace(/[·．.、,，:：;；'"“”‘’()（）\[\]【】]/g, "");
  const stripTail = (k) => k.replace(/(福音|行传|行傳|书|書|记|記|录|錄)$/u, "");

  const ALIAS = new Map();
  const putAlias = (k, id) => {
    const key = normKey(k);
    if (key && !ALIAS.has(key)) ALIAS.set(key, id);
  };
  BOOKS.forEach((b) => {
    putAlias(b.id, b.id);
    putAlias(b.name, b.id);
    putAlias(b.trad, b.id);
    putAlias(b.abbr, b.id);
    String(b.aliases || "").split("|").forEach((a) => putAlias(a, b.id));
    if (/^[123]/.test(b.id)) {
      const n = b.id[0];
      putAlias(n + b.name, b.id);
      putAlias(n + b.trad, b.id);
      putAlias(n + b.abbr, b.id);
      putAlias({ 1: "i", 2: "ii", 3: "iii" }[n] + b.name, b.id);
    }
  });
  /* 去尾缀的口语简称：创世 / 以赛亚 / 马太 / 使徒 …… 后注册，不抢占精确别名 */
  BOOKS.forEach((b) => {
    putAlias(stripTail(normKey(b.name)), b.id);
    putAlias(stripTail(normKey(b.trad)), b.id);
  });

  function resolveBook(raw, backendBooks) {
    const key = normKey(raw);
    if (!key) return null;
    const list = Array.isArray(backendBooks) ? backendBooks : [];
    for (const b of list) {
      if (!b) continue;
      if (normKey(b.id) === key || normKey(b.name) === key || normKey(b.abbr) === key) {
        return String(b.id);
      }
    }
    if (ALIAS.has(key)) return ALIAS.get(key);
    const cut = stripTail(key);
    if (cut && cut !== key) {
      if (ALIAS.has(cut)) return ALIAS.get(cut);
      for (const b of list) {
        if (!b) continue;
        if (stripTail(normKey(b.name)) === cut || stripTail(normKey(b.abbr)) === cut) {
          return String(b.id);
        }
      }
    }
    return null;
  }

  const isOT = (b) => {
    if (!b) return true;
    if (b.ot !== undefined && b.ot !== null) {
      return b.ot === true || b.ot === 1 || b.ot === "1"
        || b.ot === "OT" || b.ot === "ot" || b.ot === "old";
    }
    return Number(b.order || 0) <= 39;
  };
  const bookById = (id) => {
    const list = (S.state && Array.isArray(S.state.books) && S.state.books.length)
      ? S.state.books : BOOKS;
    return list.find((b) => String(b.id) === String(id))
      || BOOKS.find((b) => String(b.id) === String(id)) || null;
  };
  const bookName = (id, fallback) => {
    const b = bookById(id);
    return b ? (b.name || b.abbr || String(id)) : (fallback || String(id || ""));
  };
  const bookOrder = (id) => { const b = bookById(id); return b ? Number(b.order || 0) : 999; };
  const chaptersOf = (id) => { const b = bookById(id); return Number(b && b.chapters) || 1; };

  /* ── 状态 ───────────────────────────────────────────── */

  const S = {
    state: null,
    readSet: new Set(),
    chapter: null,
    book: "", chapterNo: 1,
    leftQ: "", leftGroup: { ot: true, nt: true }, leftPicked: "",
    rightTab: "highlights",
    selection: null,
    activeVerse: 0,
    aiDraft: "", aiOutput: null, aiBusy: false,
    drawerLeft: false, drawerRight: false,
    allNotes: [], allHighlights: [],
    importText: "", importPreview: null,
    importVersion: "自定义译本", importMode: "merge",
    searchQ: "",
    showEn: false,
  };

  let stateCache = { at: 0, data: null };
  const invalidateState = () => { stateCache.at = 0; };

  async function getState(force = false, quiet = false) {
    if (!force && stateCache.data && Date.now() - stateCache.at < 15000) {
      S.state = stateCache.data;
      return stateCache.data;
    }
    try {
      const d = await api("/api/bible/state");
      stateCache = { at: Date.now(), data: d };
      S.state = d;
      S.readSet = readSetFrom(d && d.progress && d.progress.read);
      paintNav(d);
      return d;
    } catch (e) {
      if (!quiet) toastIt(e.message, "err");
      throw e;
    }
  }

  function readSetFrom(raw) {
    const set = new Set();
    const add = (b, c) => {
      if (b == null || c == null || !Number.isFinite(Number(c))) return;
      set.add(String(b).trim().toLowerCase() + ":" + Number(c));
    };
    if (Array.isArray(raw)) {
      raw.forEach((x) => {
        if (typeof x === "string") {
          const m = x.match(/^([a-z0-9\u4e00-\u9fff]+)[:：\/\s.\-]+(\d+)$/i);
          if (m) add(m[1], m[2]);
          else if (/^\d+$/.test(x)) set.add(String(x));
        } else if (Array.isArray(x)) add(x[0], x[1]);
        else if (x && typeof x === "object") {
          add(x.book || x.b || x.book_id, x.chapter ?? x.ch ?? x.c);
        } else if (x != null) set.add(String(x));
      });
    } else if (raw && typeof raw === "object") {
      Object.entries(raw).forEach(([b, v]) => {
        if (Array.isArray(v)) v.forEach((c) => add(b, c));
        else add(b, v);
      });
    }
    return set;
  }
  const isReadChapter = (book, chapter) =>
    S.readSet.has(String(book).toLowerCase() + ":" + Number(chapter));
  const countRead = (book, chapters) => {
    let n = 0;
    for (let c = 1; c <= Number(chapters || 0); c += 1) if (isReadChapter(book, c)) n += 1;
    return n;
  };

  /* ── 板块外壳 / onRoute ─────────────────────────────── */

  function applyChrome(path) {
    const bl = String(path || "").startsWith("/bible");
    const reading = String(path || "").startsWith("/reading");
    const events = String(path || "").startsWith("/events");
    document.body.classList.toggle("bl-active", bl);
    document.body.classList.toggle("bl-reader-active", /^\/bible\/read\//.test(String(path || "")));
    const vault = $("#nav-vault"), rn = $("#nav-reading"), bb = $("#nav-bible");
    if (bl) {
      if (vault) vault.hidden = true;
      if (rn) rn.hidden = true;
      if (bb) bb.hidden = false;
    } else {
      if (bb) bb.hidden = true;
      /* /reading 由 reading.js 的 onRoute 负责；这里只做「离开本板块」的复原 */
      if (!reading && !events) {
        if (vault) vault.hidden = false;
        if (rn) rn.hidden = true;
      }
    }
    const sect = bl ? "bible" : (reading ? "reading" : "vault");
    $$("#sect-switch .sect-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.sect === sect));
    syncBibleNav(bl ? String(path || "") : "");
  }

  function syncBibleNav(path) {
    const clean = path.split("?")[0];
    $$("#nav-bible a.nav-item").forEach((a) => {
      const href = (a.getAttribute("href") || "").replace(/^#/, "").split("?")[0];
      a.classList.toggle("bl-nav-on", !!clean && (href === clean
        || (href.startsWith("/bible/read/") && clean.startsWith("/bible/read/") && href === clean)));
    });
    $$("#bl-nav-books a.bl-nav-book").forEach((a) => {
      const href = (a.getAttribute("href") || "").replace(/^#/, "");
      a.classList.toggle("bl-nav-on", !!clean && clean.startsWith(href.replace(/\/1$/, "")));
    });
  }

  function paintNav(d) {
    if (!d) return;
    const books = (Array.isArray(d.books) && d.books.length) ? d.books : BOOKS;
    const stats = d.stats || {};
    const setText = (id, txt) => {
      const el = document.getElementById(id);
      if (el) el.textContent = txt;
    };
    setText("c-bible", compactNum(stats.verses || 0));
    setText("c-bible-read", String(stats.read || 0));
    const host = document.getElementById("bl-nav-books");
    if (host) {
      const ot = books.filter(isOT), nt = books.filter((b) => !isOT(b));
      const link = (b) => `<a class="nav-item bl-nav-book" href="#/bible/read/${esc(b.id)}/1"
          title="${esc(b.name)} · ${Number(b.chapters) || 0} 章"><span>${esc(b.name)}</span>
          <span class="count">${Number(b.chapters) || 0}</span></a>`;
      host.innerHTML = `<div class="bl-nav-sub">旧约 · ${ot.length} 卷</div>`
        + ot.map(link).join("")
        + `<div class="bl-nav-sub">新约 · ${nt.length} 卷</div>`
        + nt.map(link).join("");
    }
  }

  const compactNum = (n) => {
    const v = Number(n) || 0;
    return v >= 10000 ? (v / 1000).toFixed(1) + "k" : String(v);
  };

  function onRoute(path) {
    applyChrome(path);
    /* 只在圣经板块加载其状态，离开后的收存 / 阅读 / 事件页不多发请求。 */
    if (String(path || "").startsWith("/bible")) getState(false, true).catch(() => { });
  }

  /* 视图函数开头也调一次：即便宿主尚未接上 GLBible.onRoute，板块外壳仍生效。 */
  const ensureChrome = () => applyChrome(hashPath());

  function hashPath() {
    const raw = (location.hash || "").replace(/^#/, "");
    return raw.startsWith("/") ? raw : "/" + raw;
  }
  function hashParams() {
    const h = location.hash || "";
    const i = h.indexOf("?");
    return new URLSearchParams(i >= 0 ? h.slice(i + 1) : "");
  }
  function goHash(h) {
    if (location.hash === h) routeNow();
    else location.hash = h;
  }
  function goRead(book, chapter, verse) {
    try {
      if (verse) sessionStorage.setItem("gl_bible_jump", JSON.stringify({ book, chapter, verse }));
      else sessionStorage.removeItem("gl_bible_jump");
    } catch (_) { }
    goHash(`#/bible/read/${encodeURIComponent(book)}/${Number(chapter) || 1}`);
  }
  function readJump() {
    try {
      const raw = sessionStorage.getItem("gl_bible_jump");
      sessionStorage.removeItem("gl_bible_jump");
      return raw ? JSON.parse(raw) : null;
    } catch (_) { return null; }
  }

  /* ── 通用 UI ───────────────────────────────────────── */

  function mount(html, cls = "bl-view") {
    main.innerHTML = `<div class="${cls} view-enter">${html}</div>`;
  }

  function head(title, sub, acts = "") {
    return `<header class="bl-head">
      <div class="bl-head-main"><div class="bl-eyebrow">圣经阅读地 · BIBLE READING ROOM</div>
        <h1>${esc(title)}</h1><p class="bl-sub">${esc(sub)}</p></div>
      <div class="bl-head-acts">${acts}</div></header>`;
  }

  function crumbs(items) {
    return `<nav class="bl-crumbs" aria-label="面包屑">${items.map((it, i) =>
      (i ? `<span class="bl-crumb-sep">/</span>` : "")
      + (it.href ? `<a href="${esc(it.href)}">${esc(it.label)}</a>`
        : `<span class="bl-crumb-now">${esc(it.label)}</span>`)).join("")}</nav>`;
  }

  function navActs() {
    return `<a class="btn small ghost" href="#/bible">总览</a>
      <a class="btn small ghost" href="#/bible/search">检索</a>
      <a class="btn small ghost" href="#/bible/notes">笔记</a>
      <a class="btn small ghost" href="#/bible/highlights">标注</a>
      <a class="btn small ghost" href="#/bible/import">导入</a>`;
  }

  function emptyBox(big, hint, act = "") {
    return `<div class="bl-empty"><div class="bl-empty-mark">“</div>
      <strong>${esc(big)}</strong><p>${esc(hint)}</p>${act}</div>`;
  }

  function errorBox(e) {
    return head("出了点问题", "请求没能完成，可以重试或先看看其它入口", navActs())
      + `<div class="bl-error"><strong>${esc((e && e.message) || "未知错误")}</strong>
        <p>请确认服务已启动、经文接口可用；已保存的数据不会丢。</p>
        <button type="button" class="btn small" data-bl-act="retry">重试一次</button></div>`;
  }

  function icon(name) {
    const paths = {
      search: '<circle cx="11" cy="11" r="6.4"/><path d="M15.8 15.8L20 20"/>',
      back: '<path d="M15 5l-7 7 7 7"/>',
      left: '<path d="M14 5l-7 7 7 7"/>',
      right: '<path d="M10 5l7 7-7 7"/>',
      download: '<path d="M12 4v11m0 0l-4-4m4 4l4-4M5 19h14"/>',
      upload: '<path d="M12 16V5m0 0L8 9m4-4l4 4M5 19h14"/>',
      book: '<path d="M12 6.2C10.2 4.9 7.7 4.4 4.5 4.6v13.1c3.2-.2 5.7.3 7.5 1.6 1.8-1.3 4.3-1.8 7.5-1.6V4.6c-3.2-.2-5.7.3-7.5 1.6z"/><path d="M12 6.2v13.1"/>',
      note: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 12h7M9 16h5"/>',
      mark: '<path d="M4 5h16M7 5v9l5 5 5-5V5"/>',
      check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
      plus: '<path d="M12 5v14M5 12h14"/>',
      ai: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 16l.7 1.9L21.5 18.5l-1.8.6L19 21l-.7-1.9-1.8-.6 1.8-.6z"/>',
      copy: '<rect x="8" y="8" width="11" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h2"/>',
      file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
    };
    return `<svg class="bl-ico" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.file}</svg>`;
  }

  function bar(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<span class="bl-bar"><i style="width:${p.toFixed(1)}%"></i></span>`;
  }

  function ring(pct, label) {
    const r = 42, c = 2 * Math.PI * r;
    const dash = c * Math.max(0, Math.min(1, (Number(pct) || 0) / 100));
    return `<div class="bl-ring-wrap"><svg class="bl-ring" viewBox="0 0 100 100" aria-hidden="true">
        <circle class="bl-ring-bg" cx="50" cy="50" r="${r}"></circle>
        <circle class="bl-ring-fg" cx="50" cy="50" r="${r}" stroke-dasharray="${c.toFixed(1)}"
          stroke-dashoffset="${(c - dash).toFixed(1)}"></circle></svg>
      <div class="bl-ring-center"><strong>${Math.round(Number(pct) || 0)}<small>%</small></strong>
        <span>${esc(label)}</span></div></div>`;
  }

  function refLabel(book, chapter, verse) {
    const n = bookName(book);
    return verse ? `${n} ${Number(chapter)}:${Number(verse)}` : `${n} ${Number(chapter)} 章`;
  }

  function byId(arr, id) {
    return (arr || []).find((x) => String(x && x.id) === String(id)) || null;
  }

  /* 自建对话框：不依赖 reading.js 的 rcModal，class 全部 bl- 前缀。 */
  function blModal({ title, body, okText = "保存", cancelText = "取消", wide = false, onOpen }) {
    return new Promise((resolve) => {
      const bd = document.createElement("div");
      bd.className = "bl-modal-backdrop";
      const box = document.createElement("div");
      box.className = "bl-modal" + (wide ? " wide" : "");
      box.innerHTML = `<div class="bl-modal-head"><strong>${esc(title || "")}</strong>
          <button type="button" class="bl-x" data-bl-modal="close" aria-label="关闭">×</button></div>
        <div class="bl-modal-body">${body || ""}</div>
        <div class="bl-modal-acts">
          <button type="button" class="btn small ghost" data-bl-modal="cancel">${esc(cancelText)}</button>
          ${okText ? `<button type="button" class="btn small" data-bl-modal="ok">${esc(okText)}</button>` : ""}
        </div>`;
      bd.appendChild(box);
      document.body.appendChild(bd);
      let done = false;
      const close = (v) => {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey, true);
        bd.classList.add("out");
        setTimeout(() => bd.remove(), 160);
        resolve(v);
      };
      const onKey = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(null); }
      };
      document.addEventListener("keydown", onKey, true);
      bd.addEventListener("mousedown", (e) => { if (e.target === bd) close(null); });
      box.addEventListener("click", (e) => {
        const b = e.target.closest("[data-bl-modal]");
        if (!b) return;
        if (b.dataset.blModal === "close" || b.dataset.blModal === "cancel") { close(null); return; }
        if (b.dataset.blModal === "ok") {
          const out = {};
          box.querySelectorAll("[name]").forEach((el) => {
            out[el.name] = el.type === "checkbox" ? el.checked : el.value;
          });
          close(out);
        }
      });
      if (onOpen) onOpen(box, close);
      const first = box.querySelector("input,textarea,select");
      if (first) setTimeout(() => { first.focus(); if (first.select) first.select(); }, 30);
    });
  }

  function downloadText(name, text) {
    try {
      const blob = new Blob(["\ufeff" + String(text)], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    } catch (e) { toastIt(e.message, "err"); }
  }

  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).then(() => toastIt("已复制"));
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = t; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove(); toastIt("已复制");
    } catch (_) { toastIt("复制失败", "err"); }
    return Promise.resolve();
  }

  function bind(sel, type, fn, root) {
    const el = $(sel, root || document);
    if (!el) return null;
    /* 右栏重绘会反复调 bindReaderInputs，左栏输入框并未被替换；
       用标记避免同一节点上叠同一个监听器。 */
    const key = "blBound" + type;
    if (el.dataset && el.dataset[key]) return el;
    if (el.dataset) el.dataset[key] = "1";
    el.addEventListener(type, fn);
    return el;
  }

  /* ── 总览 ───────────────────────────────────────────── */

  async function viewHome() {
    ensureChrome();
    let d;
    try { d = await getState(true); }
    catch (e) { mount(errorBox(e)); return; }

    const books = (Array.isArray(d.books) && d.books.length) ? d.books : BOOKS;
    const stats = d.stats || {};
    const prog = d.progress || {};
    const pBook = prog.book || (books[0] && books[0].id) || "gen";
    const pChapter = Number(prog.chapter) || 1;
    const total = Number(stats.total_chapters) || 1189;
    const read = Number(stats.read) || 0;
    const pct = total ? (read / total * 100) : 0;
    const version = d.version || "未指定译本";
    const ot = books.filter(isOT), nt = books.filter((b) => !isOT(b));

    const hero = `<section class="bl-hero">
      <div class="bl-hero-deco" aria-hidden="true">✦</div>
      <div class="bl-hero-text">
        <div class="bl-hero-eyebrow">LECTIO · 读经 · 默想 · 记录</div>
        <h2>在字句之间，留一处安静</h2>
        <p>分卷分章读完一卷书，五色划下重点，把感动写成笔记。进度、标注与灵修记录都留在这里。</p>
        <div class="bl-hero-acts">
          <button type="button" class="btn small" data-bl-act="continue"
            data-book="${esc(pBook)}" data-chapter="${pChapter}">${icon("book")} 继续阅读</button>
          <a class="btn small ghost" href="#/bible/search">${icon("search")} 检索经文</a>
          <a class="btn small ghost" href="#/bible/import">${icon("upload")} 导入经文</a>
        </div>
      </div>
      <div class="bl-hero-side"><span class="bl-hero-k">当前经文</span>
        <strong>${esc(version)}</strong><span class="bl-hero-v">${esc(compactNum(stats.verses || 0))} 节 · ${books.length} 卷</span></div>
    </section>`;

    const demo = d.demo ? `<div class="bl-demo">${icon("file")}
      <span>当前为演示选段，导入完整经文后即可全本阅读。</span>
      <a class="btn small ghost" href="#/bible/import">去导入 →</a></div>` : "";

    const continueCard = `<section class="bl-card bl-continue">
      <div class="bl-card-head"><h3>继续阅读</h3><span class="bl-card-tag">上次进度</span></div>
      <button type="button" class="bl-continue-main" data-bl-act="continue"
        data-book="${esc(pBook)}" data-chapter="${pChapter}">
        <span class="bl-continue-abbr">${esc((bookById(pBook) || {}).abbr || "卷")}</span>
        <span class="bl-continue-title"><strong>${esc(bookName(pBook))} · 第 ${pChapter} 章</strong>
          <em>${prog.updated_at ? "上次读到 " + esc(String(prog.updated_at).slice(0, 16).replace("T", " ")) : "从创世记 1 章开始"}</em></span>
        <span class="bl-continue-go">${icon("right")}</span></button>
      <div class="bl-continue-foot">${bar(pct)}<span>全本已读 ${read} / ${total} 章</span></div>
    </section>`;

    const progressCard = `<section class="bl-card bl-progress-card">
      <div class="bl-card-head"><h3>整体进度</h3><span class="bl-card-tag">按章统计</span></div>
      <div class="bl-progress-body">${ring(pct, "已读章数")}
        <div class="bl-progress-figs">
          <div><strong>${read}</strong><span>已读章</span></div>
          <div><strong>${total}</strong><span>全本章</span></div>
          <div><strong>${Number(stats.bookmarks) || 0}</strong><span>书签</span></div>
        </div></div>
      <div class="bl-progress-note">${esc(version)} · ${Number(stats.verses) || 0} 节经文已入库</div>
    </section>`;

    const statCards = `<section class="bl-stats">
      ${[["经文节数", stats.verses || 0], ["划重点", stats.highlights || 0],
      ["灵修笔记", stats.notes || 0], ["书签", stats.bookmarks || 0]].map(([k, v]) =>
      `<div class="bl-stat"><strong>${esc(compactNum(v))}</strong><span>${esc(k)}</span></div>`).join("")}
    </section>`;

    const bookGrid = (list, title, sub) => `<section class="bl-book-section">
      <div class="bl-sec-title"><h3>${esc(title)}</h3><span>${esc(sub)}</span></div>
      <div class="bl-book-grid">${list.map((b) => {
      const done = countRead(b.id, b.chapters);
      const bp = Number(b.chapters) ? done / Number(b.chapters) * 100 : 0;
      return `<button type="button" class="bl-book-card" data-bl-act="open-book" data-book="${esc(b.id)}"
          title="${esc(b.name)} · ${Number(b.chapters) || 0} 章">
          <span class="bl-book-abbr">${esc(b.abbr || String(b.name || "").slice(0, 1))}</span>
          <span class="bl-book-name">${esc(b.name)}</span>
          <span class="bl-book-meta">${Number(b.chapters) || 0} 章 · 已读 <em>${done}</em></span>
          ${bar(bp)}</button>`;
    }).join("")}</div></section>`;

    const quick = `<section class="bl-card bl-quick">
      <div class="bl-card-head"><h3>快速跳转</h3><span class="bl-card-tag">卷 · 章</span></div>
      <label class="bl-field"><span>书卷</span><select id="bl-q-book">
        <optgroup label="旧约">${ot.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("")}</optgroup>
        <optgroup label="新约">${nt.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("")}</optgroup>
      </select></label>
      <label class="bl-field"><span>章</span><input id="bl-q-ch" type="number" min="1" value="1"></label>
      <button type="button" class="btn small" data-bl-act="quick-go">打开这一章 ${icon("right")}</button>
    </section>`;

    const links = `<section class="bl-card bl-links">
      <div class="bl-card-head"><h3>随处可去</h3></div>
      <a class="bl-link-row" href="#/bible/search">${icon("search")}<span>经文检索</span><em>按词查全本</em></a>
      <a class="bl-link-row" href="#/bible/highlights">${icon("mark")}<span>我的划重点</span><em>${Number(stats.highlights) || 0} 条</em></a>
      <a class="bl-link-row" href="#/bible/notes">${icon("note")}<span>灵修笔记</span><em>${Number(stats.notes) || 0} 篇</em></a>
      <a class="bl-link-row" href="#/bible/import">${icon("upload")}<span>导入经文</span><em>txt / json</em></a>
      <button type="button" class="bl-link-row bl-link-btn" data-bl-act="export">${icon("download")}
        <span>导出全部笔记</span><em>Markdown</em></button>
    </section>`;

    /* 外部研读站点：只放普通链接，不嵌 iframe、不引外部脚本，
       点开由 target=_blank 交给系统浏览器，读经页面的数据与进度一概不碰。 */
    const ext = `<section class="bl-card bl-ext">
      <div class="bl-card-head"><h3>研读资源</h3><span class="bl-card-tag">外部站点 · 新窗口</span></div>
      <div class="bl-ext-list">
        <a class="bl-ext-row" href="https://bibleproject.com/zh-hans/" target="_blank" rel="noreferrer">
          <span class="bl-ext-mark" aria-hidden="true">圣</span>
          <span class="bl-ext-body"><strong>圣经工程 · BibleProject</strong>
            <em>书卷概览与主题短片，中文配音、简繁字幕</em></span>
          <span class="bl-ext-go" aria-hidden="true">↗</span>
        </a>
        <a class="bl-ext-row" href="https://www.biblicaltraining.org/" target="_blank" rel="noreferrer">
          <span class="bl-ext-mark" aria-hidden="true">课</span>
          <span class="bl-ext-body"><strong>BiblicalTraining</strong>
            <em>免费神学课程，从基础到研究院四级</em></span>
          <span class="bl-ext-go" aria-hidden="true">↗</span>
        </a>
      </div>
    </section>`;

    mount(crumbs([{ label: "圣经阅读地" }])
      + head("圣经阅读地", "分卷分章读经 · 五色标注 · 灵修笔记 · AI 伴读", navActs())
      + demo + hero
      + `<div class="bl-home-grid"><div class="bl-home-main">
          <div class="bl-home-two">${continueCard}${progressCard}</div>
          ${ext}
          ${bookGrid(ot, "旧约", ot.length + " 卷")}
          ${bookGrid(nt, "新约", nt.length + " 卷")}
        </div><aside class="bl-home-side">${statCards}${quick}${links}</aside></div>`);

    const sel = $("#bl-q-book");
    if (sel) sel.value = pBook;
  }

  /* ── 阅读页 ─────────────────────────────────────────── */

  async function viewRead(book, chapter) {
    ensureChrome();
    const b = String(book || "gen").toLowerCase();
    const c = Number(chapter) || 1;
    S.book = b; S.chapterNo = c; S.selection = null; S.activeVerse = 0;
    S.leftPicked = b;
    /* 书卷目录 / 已读章：state 取不到就用 66 卷基准表，不挡正文 */
    try { await getState(false, true); } catch (_) { /* 已有缓存或兜底 */ }
    let d;
    try {
      d = await req(`/api/bible/chapter?book=${encodeURIComponent(b)}&chapter=${encodeURIComponent(c)}`);
    } catch (e) { mount(errorBox(e)); return; }
    S.chapter = d;
    renderReaderShell(d);
    renderLeft();
    renderRight();
    syncReaderType();
    bindReaderInputs();
    /* 进入某章就记下位置（done=false 不改已读章列表） */
    send("/api/bible/progress", { book: b, chapter: c, verse: 1, done: false }).catch(() => { });
    const jump = readJump();
    if (jump && String(jump.book) === b && Number(jump.chapter) === c && jump.verse) {
      setTimeout(() => scrollToVerse(jump.verse), 140);
    }
    const anchor = hashParams().get("v");
    if (anchor) setTimeout(() => scrollToVerse(Number(anchor) || 1), 140);
    syncBibleNav(hashPath());
  }

  function renderReaderShell(d) {
    const prev = d.prev || null, next = d.next || null;
    const bn = d.book_name || bookName(S.book);
    const read = isReadChapter(S.book, S.chapterNo);
    const marked = (d.bookmarks || []).some((x) => Number(x.verse) === 1);
    /* 窄屏用抽屉，不要打上宽屏的 no-left / no-right 折叠标记，
       否则从窄屏拉回宽屏时两侧栏会被 CSS 永久藏起来。 */
    const openL = narrow() ? true : P.leftOpen;
    const openR = narrow() ? true : P.rightOpen;
    main.innerHTML = `<div class="bl-reader view-enter${openL ? "" : " no-left"}${openR ? "" : " no-right"}${S.showEn ? " show-en" : ""}" id="bl-reader">
      <header class="bl-rhead">
        <a class="bl-back" href="#/bible" title="回到读经台">${icon("back")}</a>
        ${crumbs([{ label: "圣经阅读地", href: "#/bible" }, { label: bn }, { label: "第 " + S.chapterNo + " 章" }])}
        <div class="bl-rhead-title"><strong>${esc(bn)}</strong><span>第 ${S.chapterNo} / ${Number(d.chapter_count) || chaptersOf(S.book)} 章 · ${(d.verses || []).length} 节</span></div>
        <div class="bl-rhead-acts">
          <button type="button" class="bl-icon-btn" data-bl-act="prev" title="上一章" ${prev ? "" : "disabled"}>${icon("left")}</button>
          <button type="button" class="bl-icon-btn" data-bl-act="next" title="下一章" ${next ? "" : "disabled"}>${icon("right")}</button>
          <span class="bl-tool-sep"></span>
          <button type="button" class="bl-icon-btn" data-bl-act="font-minus" title="缩小字号">A−</button>
          <button type="button" class="bl-icon-btn" data-bl-act="font-plus" title="放大字号">A＋</button>
          <button type="button" class="bl-icon-btn" data-bl-act="paper" title="切换纸张" id="bl-paper-btn">◐</button>
          <button type="button" class="bl-icon-btn${S.showEn ? " on" : ""}" data-bl-act="toggle-en"
            title="中英对照">EN</button>
          <span class="bl-tool-sep"></span>
          <button type="button" class="bl-icon-btn bl-read-btn${read ? " on" : ""}" data-bl-act="toggle-read"
            title="${read ? "本章已读" : "标记本章已读"}">${icon("check")}<span>${read ? "已读" : "标记已读"}</span></button>
          <button type="button" class="bl-icon-btn bl-mark-btn${marked ? " on" : ""}" data-bl-act="bookmark"
            data-verse="1" title="${marked ? "移除本章书签" : "给本章加书签"}">★<span>书签</span></button>
          <span class="bl-tool-sep"></span>
          <button type="button" class="bl-icon-btn" data-bl-act="drawer" data-side="left" title="目录">☰</button>
          <button type="button" class="bl-icon-btn" data-bl-act="drawer" data-side="right" title="标注 / 笔记 / AI">▤</button>
        </div>
      </header>
      <div class="bl-rgrid" id="bl-rgrid">
        <aside class="bl-left" id="bl-left">
          <div class="bl-left-search">${icon("search")}
            <input id="bl-left-q" type="search" placeholder="找书卷（名称 / 简称）" value="${esc(S.leftQ)}"></div>
          <div class="bl-left-list" id="bl-left-list"></div>
          <div class="bl-left-foot"><a class="btn small ghost" href="#/bible">← 读经台</a></div>
        </aside>
        <section class="bl-scroll" id="bl-scroll">
          <div class="bl-paper" id="bl-paper" data-paper="${esc(P.paper)}"
            style="--bl-fs:${Number(P.fontSize) || 18}px">
            <div class="bl-chapter-head" id="bl-chapter-head"></div>
            <article class="bl-article" id="bl-article"></article>
            <div class="bl-chapter-foot" id="bl-chapter-foot"></div>
          </div>
        </section>
        <aside class="bl-right" id="bl-right"></aside>
      </div>
      <button type="button" class="bl-rail bl-rail-left" data-bl-act="drawer" data-side="left" title="展开目录">目录</button>
      <button type="button" class="bl-rail bl-rail-right" data-bl-act="drawer" data-side="right" title="展开标注">标注</button>
      <div class="bl-scrim" id="bl-scrim" hidden></div>
      <div class="bl-selbar" id="bl-selbar" hidden></div>
    </div>`;
    renderArticle();
  }

  /* 把一节经文按 highlight 的 [start,end) 字符区间切成 esc 分段 + <mark>。 */
  function verseHtml(text, marks) {
    const src = String(text || "");
    const list = (marks || []).map((h) => ({
      h,
      s: Math.max(0, Math.min(src.length, Number(h.start) || 0)),
      e: Math.max(0, Math.min(src.length, Number(h.end) || 0)),
    })).filter((x) => x.e > x.s).sort((a, b) => a.s - b.s || a.e - b.e);
    let html = "", at = 0;
    for (const x of list) {
      if (x.s < at) continue;                 // 重叠标注跳过，保证 DOM 文本与原文严格一致
      html += esc(src.slice(at, x.s));
      const col = safeColor(x.h.color);
      const tip = (COLORS[col] ? COLORS[col].label : "标注")
        + (x.h.note ? " · " + String(x.h.note).slice(0, 80) : "");
      html += `<mark class="bl-mark bl-${col}" data-bl-act="hl-edit" data-id="${esc(x.h.id)}"
        title="${esc(tip)}">${esc(src.slice(x.s, x.e))}</mark>`;
      at = x.e;
    }
    html += esc(src.slice(at));
    return html;
  }

  function renderArticle() {
    const d = S.chapter;
    if (!d) return;
    const art = $("#bl-article"), headEl = $("#bl-chapter-head"), foot = $("#bl-chapter-foot");
    if (!art || !headEl || !foot) return;
    const verses = Array.isArray(d.verses) ? d.verses : [];
    const bn = d.book_name || bookName(S.book);
    headEl.innerHTML = `<div class="bl-ch-eyebrow">${esc(d.book || S.book)} · ${esc(String(bn).toUpperCase())}</div>
      <h2>${esc(bn)} 第 ${S.chapterNo} 章</h2>
      <div class="bl-ch-meta">共 ${verses.length} 节 · ${esc(S.state && S.state.version ? S.state.version : "")}</div>`;
    if (!verses.length) {
      /* demo=true 时大多数章本来就是空的，是正常空态，不要渲染成加载失败 */
      const demo = !!(S.state && S.state.demo);
      art.innerHTML = emptyBox(
        demo ? "这一章还没有演示经文" : "这一章还没有经文",
        demo ? "当前是演示选段（仅 24 节）；导入完整经文后，全本 66 卷都能读。"
          : "可以先用「导入经文」把这一卷补进来，或换一章看看。",
        `<a class="btn small" href="#/bible/import">去导入</a>`);
    } else {
      const hl = Array.isArray(d.highlights) ? d.highlights : [];
      const bm = new Set((Array.isArray(d.bookmarks) ? d.bookmarks : [])
        .map((x) => Number(x.verse)));
      const nt = new Set((Array.isArray(d.notes) ? d.notes : [])
        .map((x) => Number(x.verse)));
      art.innerHTML = verses.map((v) => {
        const n = Number(v.verse) || 0;
        const mine = hl.filter((h) => Number(h.verse) === n);
        const tags = (bm.has(n) ? `<span class="bl-vtag" title="有书签">★</span>` : "")
          + (nt.has(n) ? `<span class="bl-vtag" title="有笔记">✎</span>` : "");
        return `<div class="bl-verse${S.activeVerse === n ? " on" : ""}" data-bl-act="verse" data-verse="${n}">
          <sup class="bl-vnum">${n}</sup><span class="bl-vtext" data-verse="${n}">${verseHtml(v.text, mine)}</span>${
            v.text_en ? `<span class="bl-vtext-en">${esc(v.text_en)}</span>` : ""}${tags}</div>`;
      }).join("");
    }
    const prev = d.prev || null, next = d.next || null;
    foot.innerHTML = `<button type="button" class="btn small ghost" data-bl-act="prev" ${prev ? "" : "disabled"}>← 上一章</button>
      <span class="bl-ch-foot-mid">${esc(bn)} ${S.chapterNo}</span>
      <button type="button" class="btn small ghost" data-bl-act="next" ${next ? "" : "disabled"}>下一章 →</button>`;
    syncReadButtons();
  }

  function syncReadButtons() {
    const read = isReadChapter(S.book, S.chapterNo);
    $$(".bl-read-btn").forEach((b) => {
      b.classList.toggle("on", read);
      b.title = read ? "本章已读" : "标记本章已读";
      const s = b.querySelector("span");
      if (s) s.textContent = read ? "已读" : "标记已读";
    });
    const marked = (S.chapter && Array.isArray(S.chapter.bookmarks)
      ? S.chapter.bookmarks : []).some((x) => Number(x.verse) === 1);
    $$(".bl-mark-btn").forEach((b) => {
      b.classList.toggle("on", marked);
      b.title = marked ? "移除本章书签" : "给本章加书签";
    });
  }

  function renderLeft() {
    const host = $("#bl-left-list");
    if (!host) return;
    const list = (S.state && Array.isArray(S.state.books) && S.state.books.length)
      ? S.state.books : BOOKS;
    const q = normKey(S.leftQ);
    const match = (b) => !q || [b.id, b.name, b.abbr, b.trad]
      .some((x) => normKey(x).includes(q));
    const group = (title, arr, key) => {
      const rows = arr.filter(match);
      if (!rows.length && q) return "";
      const open = S.leftGroup[key] !== false;
      return `<div class="bl-left-group">
        <button type="button" class="bl-left-group-head" data-bl-act="toggle-group" data-group="${key}">
          <span>${esc(title)} · ${rows.length} 卷</span><em>${open ? "▾" : "▸"}</em></button>
        ${open ? `<div class="bl-left-books">${rows.map((b) => {
        const cur = String(b.id) === String(S.book);
        const picked = String(b.id) === String(S.leftPicked || S.book);
        const chapters = Number(b.chapters) || chaptersOf(b.id);
        let grid = "";
        if (picked) {
          const cells = [];
          for (let i = 1; i <= chapters; i += 1) {
            const on = cur && i === S.chapterNo;
            cells.push(`<button type="button" class="bl-ch-cell${isReadChapter(b.id, i) ? " done" : ""}${on ? " on" : ""}"
              data-bl-act="pick-chapter" data-book="${esc(b.id)}" data-chapter="${i}" title="第 ${i} 章">${i}</button>`);
          }
          grid = `<div class="bl-ch-grid">${cells.join("")}</div>`;
        }
        return `<div class="bl-left-book">
            <button type="button" class="bl-left-book-btn${cur ? " on" : ""}" data-bl-act="pick-book"
              data-book="${esc(b.id)}" title="${esc(b.name)} · ${chapters} 章">
              <span class="bl-lb-abbr">${esc(b.abbr || String(b.name || "").slice(0, 1))}</span>
              <span class="bl-lb-name">${esc(b.name)}</span>
              <span class="bl-lb-meta">${chapters}</span></button>${grid}</div>`;
      }).join("")}</div>` : ""}</div>`;
    };
    host.innerHTML = group("旧约", list.filter(isOT), "ot")
      + group("新约", list.filter((b) => !isOT(b)), "nt")
      + ((q && !list.filter(match).length) ? `<div class="bl-left-none">没有匹配的书卷</div>` : "");
  }

  function tabs(items, cur, attr) {
    return `<div class="bl-tabs">${items.map(([v, l]) =>
      `<button type="button" class="bl-tab${String(cur) === String(v) ? " on" : ""}"
        data-bl-act="right-tab" ${attr}="${esc(v)}">${esc(l)}</button>`).join("")}</div>`;
  }

  function renderRight(keepScroll = false) {
    const host = $("#bl-right");
    if (!host) return;
    const oldBody = $("#bl-right-body");
    const top = keepScroll && oldBody ? oldBody.scrollTop : 0;
    const d = S.chapter || {};
    const hl = Array.isArray(d.highlights) ? d.highlights : [];
    const notes = Array.isArray(d.notes) ? d.notes : [];
    let body = "";
    if (S.rightTab === "highlights") {
      const sorted = [...hl].sort((a, b) => Number(a.verse) - Number(b.verse)
        || Number(a.start) - Number(b.start));
      body = `<div class="bl-right-sec-head"><span>本章划重点 · ${sorted.length}</span>
          <span class="bl-right-hint">选中正文即可新增</span></div>`
        + (sorted.length ? sorted.map((h) => {
          const col = safeColor(h.color);
          return `<article class="bl-hl-card bl-${col}">
              <button type="button" class="bl-hl-jump" data-bl-act="hl-jump" data-id="${esc(h.id)}"
                data-verse="${Number(h.verse) || 1}">
                <span class="bl-hl-ref">${Number(h.verse) || 1} 节 · ${esc(COLORS[col].label)}</span>
                <span class="bl-hl-quote">${esc(h.quote || "")}</span></button>
              ${h.note ? `<div class="bl-hl-note">${mdHtml(String(h.note).slice(0, 1200))}</div>` : ""}
              <div class="bl-card-acts">
                <button type="button" data-bl-act="hl-edit" data-id="${esc(h.id)}">编辑批注</button>
                <button type="button" class="bl-del" data-bl-act="hl-del" data-id="${esc(h.id)}">取消划重点</button>
              </div></article>`;
        }).join("") : emptyBox("本章还没有划重点", "选中一段经文，浮出的工具条里挑一种颜色试试。"));
    } else if (S.rightTab === "notes") {
      const sorted = [...notes].sort((a, b) => Number(a.verse) - Number(b.verse));
      body = `<div class="bl-right-sec-head"><span>本章笔记 · ${sorted.length}</span>
          <button type="button" class="btn small" data-bl-act="note-new">＋ 新建</button></div>`
        + (sorted.length ? sorted.map((n) =>
          `<article class="bl-note-card" data-id="${esc(n.id)}">
            <div class="bl-note-top"><strong>${esc(n.title || "无标题笔记")}</strong>
              <span>${Number(n.verse) || 1} 节</span></div>
            <div class="bl-note-md">${mdHtml(String(n.content_md || "").slice(0, 4000))}</div>
            <div class="bl-card-acts">
              <button type="button" data-bl-act="note-jump" data-id="${esc(n.id)}">定位</button>
              <button type="button" data-bl-act="note-edit" data-id="${esc(n.id)}">编辑</button>
              <button type="button" class="bl-del" data-bl-act="note-del" data-id="${esc(n.id)}">删除</button>
            </div></article>`).join("")
          : emptyBox("还没有本章笔记", "把读经时的感动、疑问或查考结果写下来。",
            `<button type="button" class="btn small" data-bl-act="note-new">写第一篇</button>`));
    } else if (S.rightTab === "ai") {
      body = `<div class="bl-right-sec-head"><span>AI 伴读</span><span class="bl-right-hint">本章为上下文</span></div>
        <div class="bl-ai-quick">
          <button type="button" class="chip" data-bl-act="ai-action" data-action="explain">解释本章</button>
          <button type="button" class="chip" data-bl-act="ai-action" data-action="summary">总结本章</button>
        </div>
        <label class="bl-field"><span>自由提问（Ctrl / ⌘ + Enter 发送）</span>
          <textarea id="bl-ai-q" rows="3" placeholder="例如：这一章的结构是什么？">${esc(S.aiDraft)}</textarea></label>
        <button type="button" class="btn small" data-bl-act="ai-send" id="bl-ai-send">提问</button>
        <div class="bl-ai-out" id="bl-ai-out">${aiOutHtml()}</div>`;
    } else {
      const b = bookById(S.book) || {};
      const bms = Array.isArray(d.bookmarks) ? d.bookmarks : [];
      body = `<div class="bl-right-sec-head"><span>本章信息</span></div>
        <div class="bl-info">
          <div><span>书卷</span><strong>${esc(d.book_name || bookName(S.book))}</strong></div>
          <div><span>简称 / 代号</span><strong>${esc(b.abbr || "—")} · ${esc(S.book)}</strong></div>
          <div><span>所属</span><strong>${isOT(b) ? "旧约" : "新约"}</strong></div>
          <div><span>全卷章数</span><strong>${Number(d.chapter_count) || chaptersOf(S.book)} 章</strong></div>
          <div><span>本章节数</span><strong>${(Array.isArray(d.verses) ? d.verses : []).length} 节</strong></div>
          <div><span>已读状态</span><strong>${isReadChapter(S.book, S.chapterNo) ? "已读 ✓" : "未读"}</strong></div>
          <div><span>译本</span><strong>${esc((S.state && S.state.version) || "未指定")}</strong></div>
        </div>
        <div class="bl-sec-sm">本章书签 · ${bms.length}</div>
        ${bms.length ? bms.map((m) => `<div class="bl-bm-row">
            <button type="button" data-bl-act="bm-jump" data-id="${esc(m.id)}" data-verse="${Number(m.verse) || 1}">
              ${Number(m.verse) || 1} 节 · ${esc(m.label || "书签")}</button>
            <button type="button" class="bl-del" data-bl-act="bm-del" data-id="${esc(m.id)}">×</button></div>`).join("")
        : `<div class="bl-faint">还没有书签</div>`}
        <div class="bl-info-acts">
          <button type="button" class="btn small ghost" data-bl-act="copy-chapter">${icon("copy")} 复制本章</button>
          <button type="button" class="btn small ghost" data-bl-act="export">${icon("download")} 导出笔记</button>
        </div>`;
    }
    host.innerHTML = tabs([["highlights", "标注"], ["notes", "笔记"],
    ["ai", "AI 伴读"], ["info", "本章信息"]], S.rightTab, "data-tab")
      + `<div class="bl-right-body" id="bl-right-body">${body}</div>`;
    const nb = $("#bl-right-body");
    if (nb && top > 0) nb.scrollTop = top;
    bindReaderInputs();
  }

  function aiOutHtml() {
    const o = S.aiOutput;
    if (S.aiBusy) return `<div class="bl-ai-loading">正在请教 AI …</div>`;
    if (!o) return emptyBox("还没有提问", "点上面的快捷动作，或直接写个问题。");
    const ctx = o.context == null ? "" : (typeof o.context === "string"
      ? o.context : JSON.stringify(o.context));
    return `<div class="bl-ai-answer"><div class="bl-ai-meta">
        <span>${esc(o.actionLabel || "AI 回答")}</span>
        <em>${esc(o.model || "模型未标注")}</em></div>
      ${o.question ? `<div class="bl-ai-q">${esc(o.question)}</div>` : ""}
      <div class="bl-ai-body">${mdHtml(o.text || "")}</div>
      ${ctx ? `<div class="bl-ai-ctx">上下文：${esc(ctx)}</div>` : ""}</div>`;
  }

  function syncReaderType() {
    const paper = $("#bl-paper");
    if (paper) {
      paper.dataset.paper = P.paper;
      paper.style.setProperty("--bl-fs", (Number(P.fontSize) || 18) + "px");
    }
    const btn = $("#bl-paper-btn");
    if (btn) {
      const label = (PAPERS.find((x) => x[0] === P.paper) || PAPERS[0])[1];
      btn.title = `切换纸张（当前：${label}）`;
      btn.setAttribute("aria-label", btn.title);
    }
  }

  function bindReaderInputs() {
    bind("#bl-left-q", "input", (e) => {
      S.leftQ = e.target.value;
      renderLeft();
    });
    bind("#bl-ai-q", "input", (e) => { S.aiDraft = e.target.value; });
    bind("#bl-ai-q", "keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        dispatch("ai-send", null).catch(() => { });
      }
    });
  }

  async function refreshChapter(keepScroll = true) {
    const sc = $("#bl-scroll");
    const top = keepScroll && sc ? sc.scrollTop : 0;
    try {
      const d = await req(`/api/bible/chapter?book=${encodeURIComponent(S.book)}&chapter=${S.chapterNo}`);
      S.chapter = d;
      renderArticle();
      renderLeft();
      renderRight(true);
      if (sc && keepScroll) sc.scrollTop = top;
    } catch (_) { /* fail() 已 toast */ }
  }

  function scrollToVerse(v) {
    const sc = $("#bl-scroll"), art = $("#bl-article");
    const el = art ? art.querySelector(`.bl-verse[data-verse="${Number(v)}"]`) : null;
    if (!sc || !el) return;
    const top = el.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - 100;
    sc.scrollTop = Math.max(0, top);
    el.classList.add("bl-flash");
    setTimeout(() => el.classList.remove("bl-flash"), 1800);
  }

  /* ── 选区 / 浮动工具条 ─────────────────────────────── */

  /* Range：从 .bl-vtext 起点量到选区边界的字符数。mark 是透明容器，
     Range.toString() 天然把嵌套 <mark> 的文字算进去，比手写 walker 更稳。
     边界落在节号 <sup> 或节尾标记上时，用 setEnd 后夹到 [0, len]：
     落在 vtext 之前 → 0，之后 → len，都不会把额外 DOM 文本算进偏移。 */
  function pointOffset(root, node, offset, max) {
    if (!root || !node) return null;
    try {
      const r = document.createRange();
      r.selectNodeContents(root);
      r.setEnd(node, offset);
      return Math.max(0, Math.min(max, r.toString().length));
    } catch (_) { return null; }
  }

  function onSelection() {
    const art = $("#bl-article");
    if (!art || !S.chapter) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim() || !sel.rangeCount) { hideSelbar(); return; }
    const range = sel.getRangeAt(0);
    const elOf = (n) => (n && n.nodeType === 1 ? n : (n && n.parentElement));
    const v1 = elOf(range.startContainer)?.closest?.(".bl-verse");
    const v2 = elOf(range.endContainer)?.closest?.(".bl-verse");
    if (!v1 || v1 !== v2 || !art.contains(v1)) { hideSelbar(); return; }
    const vtext = v1.querySelector(".bl-vtext");
    if (!vtext) { hideSelbar(); return; }
    const verse = Number(vtext.dataset.verse) || 1;
    const vrow = (S.chapter.verses || []).find((x) => Number(x.verse) === verse);
    const text = String((vrow && vrow.text) || "");
    const max = text.length;
    const o1 = pointOffset(vtext, range.startContainer, range.startOffset, max);
    const o2 = pointOffset(vtext, range.endContainer, range.endOffset, max);
    if (o1 == null || o2 == null) { hideSelbar(); return; }
    const start = Math.min(o1, o2), end = Math.max(o1, o2);
    if (end <= start) { hideSelbar(); return; }
    S.selection = {
      book: S.book, chapter: S.chapterNo, verse,
      start, end, quote: text.slice(start, end),
    };
    showSelbar(range);
  }

  function overlaps() {
    const s = S.selection;
    if (!s || !S.chapter) return [];
    return (S.chapter.highlights || []).filter((h) => Number(h.verse) === s.verse
      && Number(h.start) < s.end && Number(h.end) > s.start);
  }

  function showSelbar(range) {
    const bar = $("#bl-selbar");
    if (!bar) return;
    const ov = overlaps();
    bar.innerHTML = `<div class="bl-swatches">${COLOR_KEYS.map((k) =>
      `<button type="button" class="bl-sw bl-${k}" data-bl-act="sel-color" data-color="${k}"
        title="${esc(COLORS[k].label)}：${esc(COLORS[k].hint)}"></button>`).join("")}</div>
      <span class="bl-sel-sep"></span>
      <button type="button" data-bl-act="sel-note">批注</button>
      <button type="button" data-bl-act="sel-bookmark">加书签</button>
      <button type="button" data-bl-act="sel-note2">存为笔记</button>
      <button type="button" data-bl-act="sel-ai">问 AI</button>
      <button type="button" data-bl-act="sel-copy">复制</button>
      ${ov.length ? `<span class="bl-sel-sep"></span><button type="button" class="bl-sel-unmark"
        data-bl-act="sel-unmark">取消划重点（${ov.length}）</button>` : ""}`;
    bar.hidden = false;
    const rect = range.getBoundingClientRect();
    const w = bar.offsetWidth || 380;
    bar.style.left = Math.max(8, Math.min(window.innerWidth - w - 8,
      rect.left + rect.width / 2 - w / 2)) + "px";
    bar.style.top = Math.max(8, rect.top - bar.offsetHeight - 10) + "px";
  }

  function hideSelbar() {
    const bar = $("#bl-selbar");
    if (bar) bar.hidden = true;
    S.selection = null;
  }

  /* ── 检索 ───────────────────────────────────────────── */

  function highlightEsc(text, q) {
    const src = String(text || "");
    const needle = String(q || "").trim();
    if (!needle) return esc(src);
    const low = src.toLowerCase(), nl = needle.toLowerCase();
    let out = "", at = 0, i = low.indexOf(nl, at);
    if (i < 0) return esc(src);
    while (i >= 0) {
      out += esc(src.slice(at, i));
      out += `<em class="bl-hit">${esc(src.slice(i, i + needle.length))}</em>`;
      at = i + needle.length;
      i = low.indexOf(nl, at);
    }
    out += esc(src.slice(at));
    return out;
  }

  async function viewSearch() {
    ensureChrome();
    const q = (hashParams().get("q") || "").trim();
    S.searchQ = q;
    let d = { hits: [], total: 0 };
    if (q) {
      try {
        d = await req("/api/bible/search?q=" + encodeURIComponent(q));
      } catch (e) { mount(errorBox(e)); return; }
    }
    const hits = Array.isArray(d.hits) ? d.hits : [];
    const list = hits.length ? `<div class="bl-results">${hits.map((h, i) => {
      const text = h.text || h.snippet || "";
      return `<article class="bl-result" data-bl-act="hit" data-book="${esc(h.book)}"
          data-chapter="${Number(h.chapter) || 1}" data-verse="${Number(h.verse) || 1}"
          data-hit="${i}" tabindex="0">
        <div class="bl-result-top"><span class="bl-result-ref">${esc(h.book_name || bookName(h.book))}
          ${Number(h.chapter) || 1}:${Number(h.verse) || 1}</span>
          <span class="bl-result-go">打开 ${icon("right")}</span></div>
        <div class="bl-result-text">${highlightEsc(text, q)}</div>
        ${h.snippet && h.snippet !== text ? `<div class="bl-result-snip">${highlightEsc(h.snippet, q)}</div>` : ""}
      </article>`;
    }).join("")}</div>` : (q
      ? emptyBox("没有命中", "换个关键词试试；支持单字、词组或卷名简称。")
      : emptyBox("还没开始搜", "输入一个词、一句经文，或「创 1:1」这样的出处。"));

    mount(crumbs([{ label: "圣经阅读地", href: "#/bible" }, { label: "经文检索" }])
      + head("经文检索", "在全本经文里找一句话、一个词", navActs())
      + `<div class="bl-searchbar">
          <label class="bl-bigsearch">${icon("search")}
            <input id="bl-sq" type="search" placeholder="例如：起初 / light / 神爱世人" value="${esc(q)}"></label>
          <button type="button" class="btn small" data-bl-act="search-go" id="bl-sgo">检索</button>
        </div>
        <div class="bl-search-meta">${q ? `命中 <strong>${Number(d.total) || hits.length}</strong> 条 · 关键词「${esc(q)}」`
      : "回车即可检索；结果点一下就跳到对应章节"}</div>
        ${list}`);
    bind("#bl-sq", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); dispatch("search-go", null).catch(() => { }); }
    });
  }

  /* ── 全部笔记 ───────────────────────────────────────── */

  async function viewNotes() {
    ensureChrome();
    let d;
    try { d = await req("/api/bible/notes"); }
    catch (e) { mount(errorBox(e)); return; }
    const notes = Array.isArray(d.notes) ? d.notes : [];
    S.allNotes = notes;
    const sorted = [...notes].sort((a, b) =>
      bookOrder(a.book) - bookOrder(b.book)
      || Number(a.chapter) - Number(b.chapter)
      || Number(a.verse) - Number(b.verse));
    const groups = new Map();
    sorted.forEach((n) => {
      const k = String(n.book || "");
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(n);
    });
    const body = groups.size ? `<div class="bl-groups">${[...groups.entries()].map(([k, arr]) =>
      `<section class="bl-group"><div class="bl-group-head"><h3>${esc(bookName(k))}</h3>
        <span>${arr.length} 篇</span></div>
        <div class="bl-group-cards">${arr.map((n) => `<article class="bl-note-card bl-wide">
          <div class="bl-note-top"><strong>${esc(n.title || "无标题笔记")}</strong>
            <button type="button" class="bl-ref-link" data-bl-act="note-jump" data-id="${esc(n.id)}"
              data-book="${esc(n.book)}" data-chapter="${Number(n.chapter) || 1}"
              data-verse="${Number(n.verse) || 1}">${esc(refLabel(n.book, n.chapter, n.verse))} ↗</button></div>
          <div class="bl-note-md">${mdHtml(String(n.content_md || "").slice(0, 4000))}</div>
          <div class="bl-card-acts">
            <button type="button" data-bl-act="note-edit" data-id="${esc(n.id)}">编辑</button>
            <button type="button" data-bl-act="note-jump" data-id="${esc(n.id)}"
              data-book="${esc(n.book)}" data-chapter="${Number(n.chapter) || 1}"
              data-verse="${Number(n.verse) || 1}">回到经文</button>
            <button type="button" class="bl-del" data-bl-act="note-del" data-id="${esc(n.id)}">删除</button>
          </div></article>`).join("")}</div></section>`).join("")}</div>`
      : emptyBox("还没有灵修笔记", "在阅读页右侧「笔记」标签里新建，或选中经文后存为笔记。",
        `<a class="btn small" href="#/bible">去读经</a>`);

    mount(crumbs([{ label: "圣经阅读地", href: "#/bible" }, { label: "灵修笔记" }])
      + head("灵修笔记", "按书卷归拢的全部笔记，可编辑、可跳回经文",
        `<button type="button" class="btn small" data-bl-act="note-new">＋ 新建笔记</button>` + navActs())
      + `<div class="bl-list-meta">共 ${notes.length} 篇 · ${groups.size} 卷</div>` + body);
  }

  /* ── 全部划重点 ─────────────────────────────────────── */

  async function viewHighlights() {
    ensureChrome();
    let d;
    try { d = await req("/api/bible/highlights"); }
    catch (e) { mount(errorBox(e)); return; }
    const hl = Array.isArray(d.highlights) ? d.highlights : [];
    S.allHighlights = hl;
    const body = hl.length ? `<div class="bl-groups">${COLOR_KEYS.map((k) => {
      const arr = hl.filter((h) => safeColor(h.color) === k)
        .sort((a, b) => bookOrder(a.book) - bookOrder(b.book)
          || Number(a.chapter) - Number(b.chapter)
          || Number(a.verse) - Number(b.verse));
      if (!arr.length) return "";
      return `<section class="bl-group"><div class="bl-group-head">
          <h3><span class="bl-dot bl-${k}"></span>${esc(COLORS[k].label)}</h3>
          <span>${arr.length} 条 · ${esc(COLORS[k].hint)}</span></div>
        <div class="bl-group-cards">${arr.map((h) => `<article class="bl-hl-card bl-wide bl-${k}">
          <button type="button" class="bl-hl-jump" data-bl-act="hl-jump" data-id="${esc(h.id)}"
            data-book="${esc(h.book)}" data-chapter="${Number(h.chapter) || 1}"
            data-verse="${Number(h.verse) || 1}">
            <span class="bl-hl-ref">${esc(refLabel(h.book, h.chapter, h.verse))}</span>
            <span class="bl-hl-quote">${esc(h.quote || "")}</span></button>
          ${h.note ? `<div class="bl-hl-note">${mdHtml(String(h.note).slice(0, 1200))}</div>` : ""}
          <div class="bl-card-acts">
            <button type="button" data-bl-act="hl-jump" data-id="${esc(h.id)}"
              data-book="${esc(h.book)}" data-chapter="${Number(h.chapter) || 1}"
              data-verse="${Number(h.verse) || 1}">回到经文</button>
            <button type="button" class="bl-del" data-bl-act="hl-del" data-id="${esc(h.id)}">取消划重点</button>
          </div></article>`).join("")}</div></section>`;
    }).join("")}</div>`
      : emptyBox("还没有划重点", "在阅读页选中一段经文，从浮出的工具条挑一种颜色。",
        `<a class="btn small" href="#/bible">去读经</a>`);

    mount(crumbs([{ label: "圣经阅读地", href: "#/bible" }, { label: "我的划重点" }])
      + head("我的划重点", "按颜色分组的全部标注，可取消、可跳回原处", navActs())
      + `<div class="bl-list-meta">共 ${hl.length} 条</div>` + body);
  }

  /* ── 导入 ───────────────────────────────────────────── */

  const TEMPLATE = [
    "# 观澜 · 圣经阅读地 · 经文导入模板",
    "# 每行一条：卷名 章:节 经文（支持简体/繁体卷名、简称、英文 id，也支持 创1:1 无空格写法）",
    "# 也可以直接粘贴 JSON 数组：[{\"book\":\"gen\",\"chapter\":1,\"verse\":1,\"text\":\"...\"}]",
    "创世记 1:1 起初，神创造天地。",
    "创世记 1:2 地是空虚混沌，渊面黑暗；神的灵运行在水面上。",
    "创1:3 神说：「要有光」，就有了光。",
    "創世記 1:4 神看光是好的，就把光暗分开了。",
    "詩篇 23:1 耶和华是我的牧者，我必不致缺乏。",
    "約翰福音 3:16 神爱世人，甚至将他的独生子赐给他们，叫一切信他的，不至灭亡，反得永生。",
    "gen 1:5 神称光为昼，称暗为夜。有晚上，有早晨，这是头一日。",
  ].join("\n");

  function parseImport(raw) {
    const text = String(raw || "").replace(/\r\n?/g, "\n").trim();
    const out = { verses: [], errors: [], books: new Set(), json: false };
    if (!text) return out;
    const backend = S.state && S.state.books;
    const push = (book, chapter, verse, txt, where) => {
      if (!book) { out.errors.push(`${where}：无法识别卷名`); return; }
      const c = Number(chapter), v = Number(verse), t = String(txt || "").trim();
      if (!Number.isFinite(c) || c < 1 || !Number.isFinite(v) || v < 1 || !t) {
        out.errors.push(`${where}：章 / 节 / 经文不完整`); return;
      }
      out.verses.push({ book, chapter: c, verse: v, text: t });
      out.books.add(book);
    };
    if (text[0] === "[") {
      try {
        const arr = JSON.parse(text);
        if (!Array.isArray(arr)) throw new Error("顶层不是数组");
        arr.forEach((v, i) => {
          const token = v && (v.book || v.book_id || v.b);
          push(resolveBook(token, backend), v && (v.chapter ?? v.ch ?? v.c),
            v && (v.verse ?? v.v), v && (v.text || v.content), `第 ${i + 1} 条「${String(token || "").slice(0, 20)}」`);
        });
        out.json = true;
        return out;
      } catch (e) {
        out.errors.push(`JSON 解析失败（${e.message}），已按纯文本格式重试`);
      }
    }
    text.split("\n").forEach((line, i) => {
      if (!line.trim() || /^\s*(#|\/\/|;)/.test(line)) return;
      /* 只归一全角数字 / 字母 / 冒号 / 全角空格；经文里的全角标点
         （，；「」等）必须原样保留，否则导入后正文会被改字。 */
      const toHalf = (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0);
      const s = line
        .replace(/[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/g, toHalf)
        .replace(/\uFF1A/g, ":")
        .replace(/[\u3000]/g, " ").trim();
      let m = s.match(/^(.*?)\s*(\d{1,3})\s*[:：]\s*(\d{1,3})[\s:：\-–—.]*(.*)$/);
      if (!m) m = s.match(/^(.*?)\s*(\d{1,3})\s*章\s*(\d{1,3})\s*节\s*(.*)$/);
      const where = `第 ${i + 1} 行`;
      if (!m) { out.errors.push(`${where}：格式不对 → ${line.trim().slice(0, 60)}`); return; }
      const token = m[1].trim().replace(/[第卷]\s*$/, "");
      const id = resolveBook(token, backend);
      if (!id) { out.errors.push(`${where}：无法识别卷名「${m[1].trim().slice(0, 24)}」`); return; }
      push(id, m[2], m[3], m[4], where);
    });
    return out;
  }

  function importPreviewHtml(p) {
    if (!p) return `<div class="bl-hint">在左侧粘贴经文或选择文件，这里会实时显示解析结果。</div>`;
    const rows = p.verses.slice(0, 10).map((v) =>
      `<tr><td>${esc(bookName(v.book, v.book))}</td><td>${v.chapter}:${v.verse}</td>
        <td>${esc(String(v.text).slice(0, 90))}</td></tr>`).join("");
    return `<div class="bl-parse-stats">
        <div><strong>${p.verses.length}</strong><span>共解析节数</span></div>
        <div><strong>${p.books.size}</strong><span>覆盖书卷</span></div>
        <div class="${p.errors.length ? "bad" : ""}"><strong>${p.errors.length}</strong><span>错误条目</span></div>
      </div>
      <div class="bl-sec-sm">前 ${Math.min(10, p.verses.length)} 条预览 ${p.json ? "· JSON 格式" : "· 文本格式"}</div>
      ${p.verses.length ? `<div class="bl-preview-table"><table><thead><tr><th>卷名</th><th>章:节</th><th>经文</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : `<div class="bl-faint">还没有解析出可用的经文</div>`}
      ${p.errors.length ? `<div class="bl-sec-sm bl-err-title">未导入的 ${p.errors.length} 条（请修正后重新粘贴）</div>
        <ul class="bl-err-list">${p.errors.slice(0, 30).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}`;
  }

  async function viewImport() {
    ensureChrome();
    try { await getState(false, true); } catch (_) { }
    mount(crumbs([{ label: "圣经阅读地", href: "#/bible" }, { label: "导入经文" }])
      + head("导入经文", "把完整经文放进书库；支持 txt 逐行格式与 JSON 数组", navActs())
      + `<div class="bl-import-grid">
        <section class="bl-card bl-import-editor">
          <div class="bl-card-head"><h3>经文内容</h3><span class="bl-card-tag">粘贴 / 上传</span></div>
          <textarea id="bl-import-text" rows="14" spellcheck="false"
            placeholder="创世记 1:1 起初，神创造天地。&#10;创1:2 地是空虚混沌…&#10;或粘贴 JSON 数组">${esc(S.importText)}</textarea>
          <div class="bl-import-file">
            <label class="bl-file-btn">${icon("upload")} 选择 .txt / .json 文件
              <input type="file" id="bl-import-file" accept=".txt,.json,text/plain,application/json" hidden></label>
            <button type="button" class="btn small ghost" data-bl-act="import-template">${icon("download")} 下载模板</button>
            <button type="button" class="btn small ghost" data-bl-act="import-parse">重新解析</button>
          </div>
          <div class="bl-import-opts">
            <label class="bl-field"><span>译本 / 版本名</span>
              <input id="bl-imp-version" type="text" value="${esc(S.importVersion)}" placeholder="例如：和合本 1919"></label>
            <div class="bl-field"><span>导入方式</span><div class="bl-radios">
              <label><input type="radio" name="bl-imp-mode" value="merge" ${S.importMode === "merge" ? "checked" : ""}> 合并（保留已有）</label>
              <label><input type="radio" name="bl-imp-mode" value="replace" ${S.importMode === "replace" ? "checked" : ""}> 覆盖（清空重来）</label>
            </div></div>
          </div>
          <div class="bl-import-actions">
            <button type="button" class="btn small" data-bl-act="import-submit" id="bl-imp-submit">导入到书库</button>
            <button type="button" class="btn small ghost bl-danger" data-bl-act="import-reset">恢复演示数据</button>
          </div>
          <div class="bl-hint">「覆盖」会先清空现有经文与标注，请先确认已导出笔记。</div>
        </section>
        <aside class="bl-card bl-import-side">
          <div class="bl-card-head"><h3>格式说明</h3><span class="bl-card-tag">解析规则</span></div>
          <div class="bl-format">
            <p><code>卷名 章:节 经文</code> —— 每行一节。</p>
            <ul>
              <li>卷名支持中文简体 / 繁体、简称（<code>创</code>、<code>约一</code>）、英文 id（<code>gen</code>、<code>1co</code>）与英文名。</li>
              <li>全角空格、全角冒号、<code>创1:1</code> 无空格写法、<code>1章1节</code> 写法都能识别。</li>
              <li>也支持 JSON 数组：<code>[{"book":"gen","chapter":1,"verse":1,"text":"..."}]</code>。</li>
              <li>以 <code>#</code> / <code>//</code> 开头的行会被当作注释跳过。</li>
              <li>认不出的卷名不会被静默丢掉，会列在右侧错误清单里。</li>
            </ul>
            <p>章数按 66 卷基准表校验，导入后可回到读经台逐卷阅读。</p>
          </div>
          <div class="bl-card-head bl-preview-head"><h3>解析预览</h3></div>
          <div class="bl-preview" id="bl-import-preview">${importPreviewHtml(S.importPreview)}</div>
        </aside></div>`);

    const ta = $("#bl-import-text");
    const parseNow = () => {
      S.importText = ta ? ta.value : "";
      S.importPreview = parseImport(S.importText);
      const host = $("#bl-import-preview");
      if (host) host.innerHTML = importPreviewHtml(S.importPreview);
      const btn = $("#bl-imp-submit");
      if (btn) btn.disabled = !S.importPreview || !S.importPreview.verses.length;
    };
    if (ta) {
      ta.addEventListener("input", parseNow);
      /* 首次进入给个空预览，不自动塞示例内容 */
      S.importPreview = S.importPreview || parseImport("");
      parseNow();
    }
    bind("#bl-import-file", "change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        if (ta) { ta.value = String(reader.result || ""); parseNow(); }
        toastIt(`已读取 ${f.name}`);
      };
      reader.onerror = () => toastIt(`读取 ${f.name} 失败`, "err");
      reader.readAsText(f, "utf-8");
    });
    bind("#bl-imp-version", "input", (e) => { S.importVersion = e.target.value; });
    $$('input[name="bl-imp-mode"]').forEach((el) =>
      el.addEventListener("change", () => { if (el.checked) S.importMode = el.value; }));
  }

  /* ── 动作分发 ───────────────────────────────────────── */

  async function dispatch(act, el) {
    const ds = (el && el.dataset) || {};
    const num = (k, dft) => Number(ds[k] || dft) || dft;
    switch (act) {
      case "retry": routeNow(); return;

      case "continue":
      case "open-book": {
        const book = ds.book || "gen";
        let chapter = num("chapter", 1);
        if (act === "open-book") {
          const p = S.state && S.state.progress;
          chapter = (p && String(p.book) === String(book) && Number(p.chapter)) || 1;
        }
        goRead(book, chapter);
        return;
      }
      case "quick-go": {
        const sel = $("#bl-q-book"), ch = $("#bl-q-ch");
        const book = sel ? sel.value : "gen";
        const chapter = Math.max(1, Number(ch && ch.value) || 1);
        goRead(book, chapter);
        return;
      }
      case "search-go": {
        const box = $("#bl-sq");
        const q = (box && box.value || "").trim();
        if (!q) { toastIt("先写个要搜的词", "warn"); return; }
        goHash("#/bible/search?q=" + encodeURIComponent(q));
        return;
      }
      case "hit": goRead(ds.book, num("chapter", 1), num("verse", 1)); return;

      case "pick-book": {
        S.leftPicked = ds.book;
        renderLeft();
        return;
      }
      case "toggle-group": {
        const k = ds.group === "nt" ? "nt" : "ot";
        S.leftGroup[k] = S.leftGroup[k] === false;
        renderLeft();
        return;
      }
      case "pick-chapter": goRead(ds.book, num("chapter", 1)); return;
      case "prev":
      case "next": {
        const d = S.chapter;
        const t = d && (act === "prev" ? d.prev : d.next);
        if (t && t.book) goRead(t.book, Number(t.chapter) || 1);
        else toastIt(act === "prev" ? "已经是第一卷第一章" : "已经是最后一卷最后一章", "warn");
        return;
      }
      case "font-minus":
      case "font-plus": {
        if (act === "font-minus") {
          if (P.fontSize <= 13) { toastIt("已是最小字号 13px", "ok", "", 1600); return; }
          P.fontSize -= 1;
        } else {
          if (P.fontSize >= 28) { toastIt("已是最大字号 28px", "ok", "", 1600); return; }
          P.fontSize += 1;
        }
        savePrefs(); syncReaderType();
        return;
      }
      case "paper": {
        const i = PAPERS.findIndex((x) => x[0] === P.paper);
        P.paper = PAPERS[(i + 1 + PAPERS.length) % PAPERS.length][0];
        savePrefs(); syncReaderType();
        toastIt("纸张 · " + PAPERS.find((x) => x[0] === P.paper)[1], "ok", "", 1600);
        return;
      }
      case "toggle-en": {
        S.showEn = !S.showEn;
        const reader = $("#bl-reader");
        if (reader) reader.classList.toggle("show-en", S.showEn);
        const enBtn = document.querySelector('[data-bl-act="toggle-en"]');
        if (enBtn) enBtn.classList.toggle("on", S.showEn);
        return;
      }

      case "toggle-read": {
        /* 后端 progress(done=false) 只记录位置、不删已读章，所以这里做成单向：
           已读的章只提示，不给「取消已读」这种点了没反应的假按钮。 */
        if (isReadChapter(S.book, S.chapterNo)) {
          toastIt("这一章已经在已读列表里了", "ok", "", 1600);
          return;
        }
        await send("/api/bible/progress", {
          book: S.book, chapter: S.chapterNo, verse: 1, done: true,
        });
        S.readSet.add(String(S.book).toLowerCase() + ":" + S.chapterNo);
        if (S.state && S.state.stats) {
          S.state.stats.read = Number(S.state.stats.read || 0) + 1;
        }
        invalidateState();
        syncReadButtons(); renderLeft(); renderRight();
        toastIt("已标记本章为已读");
        return;
      }
      case "bookmark": await toggleBookmark(num("verse", 1)); return;
      case "bm-jump": scrollToVerse(num("verse", 1)); return;
      case "bm-del": {
        await send("/api/bible/bookmark/delete", { id: ds.id });
        await refreshChapter();
        invalidateState(); toastIt("书签已移除");
        return;
      }
      case "right-tab": {
        S.rightTab = ds.tab || "highlights";
        renderRight();
        return;
      }
      case "drawer": setDrawer(ds.side === "right" ? "right" : "left"); return;
      case "scrim": setDrawer("", false); setDrawer("", false); return;

      case "verse": {
        const v = num("verse", 1);
        S.activeVerse = v;
        $$(".bl-verse.on").forEach((x) => x.classList.remove("on"));
        const row = $(`.bl-verse[data-verse="${v}"]`);
        if (row) row.classList.add("on");
        return;
      }
      case "copy-chapter": {
        const verses = (S.chapter && S.chapter.verses) || [];
        const text = verses.map((v) => `${Number(v.verse)} ${v.text}`).join("\n");
        await copyText(`${bookName(S.book)} 第 ${S.chapterNo} 章\n\n${text}`);
        return;
      }
      case "export": {
        const r = await req("/api/bible/export");
        downloadText("观澜-圣经阅读地-笔记.md", r.markdown || "");
        toastIt("已导出 Markdown");
        return;
      }

      /* 选区工具条 */
      case "sel-color": await addHighlight(ds.color || "amber", ""); return;
      case "sel-note": {
        const s = S.selection;
        if (!s) return;
        const v = await blModal({
          title: "给这段经文加批注", okText: "保存批注",
          body: `<blockquote class="bl-quote">${esc(s.quote.slice(0, 600))}</blockquote>
            <label class="bl-field"><span>颜色</span><select name="color">${COLOR_KEYS.map((k) =>
            `<option value="${k}">${esc(COLORS[k].label)}</option>`).join("")}</select></label>
            <label class="bl-field"><span>批注（支持 Markdown）</span>
              <textarea name="note" rows="5" data-md-preview placeholder="写下理解、疑问或查考"></textarea></label>`,
          onOpen: (box) => bindMd(box),
        });
        if (!v) return;
        await addHighlight(v.color, v.note);
        return;
      }
      case "sel-bookmark": {
        const s = S.selection;
        if (!s) return;
        const label = await promptIt("书签标签", s.quote.slice(0, 30));
        if (label == null) return;
        /* 后端对同一 (book,chapter,verse) 再调一次是「切换」：看 removed 决定提示 */
        const r = await send("/api/bible/bookmark", {
          book: s.book, chapter: s.chapter, verse: s.verse, label: label || s.quote.slice(0, 30),
        });
        hideSelbar(); window.getSelection()?.removeAllRanges();
        await refreshChapter(); invalidateState();
        toastIt(r && r.removed ? "书签已移除" : "已加书签");
        return;
      }
      case "sel-note2": {
        const s = S.selection;
        if (!s) return;
        hideSelbar();
        await noteModal(null, {
          book: s.book, chapter: s.chapter, verse: s.verse,
          title: `摘录 · ${s.quote.slice(0, 18)}`,
          content: `> ${s.quote}\n\n`,
        });
        return;
      }
      case "sel-ai": {
        const s = S.selection;
        if (!s) return;
        S.rightTab = "ai"; renderRight();
        S.aiDraft = `请解释这段经文：「${s.quote.slice(0, 160)}」`;
        const q = $("#bl-ai-q"); if (q) q.value = S.aiDraft;
        hideSelbar();
        await runAI("explain", S.aiDraft, s.quote);
        return;
      }
      case "sel-copy": {
        const s = S.selection;
        if (!s) return;
        await copyText(s.quote);
        hideSelbar();
        return;
      }
      case "sel-unmark": {
        const ids = overlaps().map((h) => h.id);
        hideSelbar();
        await deleteHighlights(ids);
        return;
      }

      /* 标注 / 笔记 */
      case "hl-jump": {
        const h = byId((S.chapter && S.chapter.highlights) || [], ds.id)
          || byId(S.allHighlights, ds.id);
        const book = ds.book || (h && h.book) || S.book;
        const chapter = num("chapter", h && h.chapter) || S.chapterNo;
        const verse = num("verse", h && h.verse) || 1;
        if (String(book) === String(S.book) && Number(chapter) === S.chapterNo && $("#bl-reader")) {
          scrollToVerse(verse);
        } else goRead(book, chapter, verse);
        return;
      }
      case "hl-edit": {
        const h = byId((S.chapter && S.chapter.highlights) || [], ds.id)
          || byId(S.allHighlights, ds.id);
        if (!h) { toastIt("这条标注已不存在", "warn"); return; }
        const col = safeColor(h.color);
        const v = await blModal({
          title: "编辑划重点 / 批注", okText: "保存",
          body: `<blockquote class="bl-quote">${esc(h.quote || "")}</blockquote>
            <label class="bl-field"><span>颜色</span><select name="color">${COLOR_KEYS.map((k) =>
            `<option value="${k}"${k === col ? " selected" : ""}>${esc(COLORS[k].label)}</option>`).join("")}</select></label>
            <label class="bl-field"><span>批注（支持 Markdown）</span>
              <textarea name="note" rows="5" data-md-preview>${esc(h.note || "")}</textarea></label>`,
          onOpen: (box) => bindMd(box),
        });
        if (!v) return;
        /* 契约里没有 update 接口：用同一字符区间重建一条。
           AUTOINCREMENT 会换新 id，所以绝不拿旧 id 做任何缓存；
           先建新的、成功后再删旧的 —— 中途失败宁可短暂多一条重叠标注，
           也不能把批注弄丢。两步都结束后统一重新拉 /chapter 自愈。 */
        await send("/api/bible/highlight", {
          book: h.book || S.book, chapter: h.chapter || S.chapterNo,
          verse: h.verse, start: h.start, end: h.end, quote: h.quote,
          color: safeColor(v.color), note: v.note || "",
        });
        let removed = true;
        try {
          await send("/api/bible/highlight/delete", { id: h.id });
        } catch (_) { removed = false; }
        if ($("#bl-reader")) await refreshChapter();
        else routeNow();
        invalidateState();
        toastIt(removed ? "已保存" : "新标注已保存，旧标注删除失败；已按服务端真实状态刷新",
          removed ? "ok" : "warn");
        return;
      }
      case "hl-del": {
        if (!(await confirmIt("取消这条划重点？经文原文不会改。",
          { danger: true, confirmText: "取消划重点" }))) return;
        await deleteHighlights([ds.id]);
        return;
      }
      case "note-new": {
        const onReader = !!$("#bl-reader");
        const last = (S.state && S.state.progress) || {};
        await noteModal(null, {
          book: onReader ? S.book : (last.book || "gen"),
          chapter: onReader ? S.chapterNo : (Number(last.chapter) || 1),
          verse: onReader ? (S.activeVerse || 1) : (Number(last.verse) || 1),
          title: "", content: "",
        });
        return;
      }
      case "note-edit": {
        const n = byId((S.chapter && S.chapter.notes) || [], ds.id) || byId(S.allNotes, ds.id);
        if (!n) { toastIt("这篇笔记已不存在", "warn"); return; }
        await noteModal(n, null);
        return;
      }
      case "note-del": {
        if (!(await confirmIt("删掉这篇笔记？删了就没了。",
          { danger: true, confirmText: "删除" }))) return;
        await send("/api/bible/note/delete", { id: ds.id });
        invalidateState();
        if ($("#bl-reader")) await refreshChapter();
        else routeNow();
        toastIt("笔记已删除");
        return;
      }
      case "note-jump": {
        const n = byId(S.allNotes, ds.id) || byId((S.chapter && S.chapter.notes) || [], ds.id);
        const book = ds.book || (n && n.book) || S.book;
        const chapter = num("chapter", n && n.chapter) || S.chapterNo;
        const verse = num("verse", n && n.verse) || 1;
        if (String(book) === String(S.book) && Number(chapter) === S.chapterNo && $("#bl-reader")) {
          scrollToVerse(verse);
        } else goRead(book, chapter, verse);
        return;
      }

      /* AI 伴读 */
      case "ai-action": {
        const action = ds.action === "summary" ? "summary" : "explain";
        await runAI(action, "", "");
        return;
      }
      case "ai-send": {
        const q = ($("#bl-ai-q") && $("#bl-ai-q").value || S.aiDraft || "").trim();
        if (!q) { toastIt("先写个问题", "warn"); return; }
        await runAI("chat", q, "");
        return;
      }

      /* 导入 */
      case "import-parse": {
        const ta = $("#bl-import-text");
        S.importText = ta ? ta.value : "";
        S.importPreview = parseImport(S.importText);
        const host = $("#bl-import-preview");
        if (host) host.innerHTML = importPreviewHtml(S.importPreview);
        const btn = $("#bl-imp-submit");
        if (btn) btn.disabled = !S.importPreview.verses.length;
        toastIt(`解析出 ${S.importPreview.verses.length} 节，错误 ${S.importPreview.errors.length} 条`);
        return;
      }
      case "import-submit": {
        const p = S.importPreview || parseImport($("#bl-import-text")?.value || "");
        S.importPreview = p;
        if (!p.verses.length) { toastIt("没有可导入的经文", "warn"); return; }
        if (p.errors.length) {
          const ok = await confirmIt(`有 ${p.errors.length} 条没能识别（不会静默丢弃），仍要导入其余 ${p.verses.length} 节吗？`,
            { confirmText: "继续导入" });
          if (!ok) return;
        }
        const ver = ($("#bl-imp-version") && $("#bl-imp-version").value || S.importVersion || "自定义译本").trim();
        const modeEl = $('input[name="bl-imp-mode"]:checked');
        const mode = modeEl ? modeEl.value : S.importMode;
        if (mode === "replace") {
          const ok = await confirmIt("「覆盖」会清空现有经文与全部标注、笔记，确定继续？",
            { danger: true, confirmText: "清空并导入" });
          if (!ok) return;
        }
        const btn = $("#bl-imp-submit");
        if (btn) { btn.disabled = true; btn.textContent = "导入中…"; }
        try {
          const r = await send("/api/bible/import", {
            verses: p.verses, version: ver || "自定义译本", mode,
          });
          invalidateState();
          S.importText = ""; S.importPreview = parseImport("");
          const ta = $("#bl-import-text"); if (ta) ta.value = "";
          const host = $("#bl-import-preview");
          if (host) host.innerHTML = importPreviewHtml(S.importPreview);
          const skipped = Number(r.skipped) || 0;
          toastIt(`已导入 ${Number(r.imported) || p.verses.length} 节经文`
            + (skipped ? ` · 跳过 ${skipped} 条无效条目` : ""));
          setTimeout(() => goHash("#/bible"), 700);
        } finally {
          if (btn) { btn.disabled = false; btn.textContent = "导入到书库"; }
        }
        return;
      }
      case "import-template": {
        downloadText("观澜-经文导入模板.txt", TEMPLATE);
        toastIt("模板已下载");
        return;
      }
      case "import-reset": {
        if (!(await confirmIt("恢复演示数据会清空当前经文、标注、笔记与进度，确定？",
          { danger: true, confirmText: "恢复演示数据" }))) return;
        await send("/api/bible/reset", { confirm: true });
        invalidateState();
        toastIt("已恢复演示数据");
        setTimeout(() => goHash("#/bible"), 500);
        return;
      }
      default: return;
    }
  }

  /* ── 具体动作 ───────────────────────────────────────── */

  async function addHighlight(color, note) {
    const s = S.selection;
    if (!s) return;
    await send("/api/bible/highlight", {
      book: s.book, chapter: s.chapter, verse: s.verse,
      start: s.start, end: s.end, quote: s.quote,
      color: safeColor(color), note: note || "",
    });
    hideSelbar();
    window.getSelection()?.removeAllRanges();
    await refreshChapter();
    invalidateState();
    toastIt(`已标为「${COLORS[safeColor(color)].label}」`);
  }

  async function deleteHighlights(ids) {
    const list = [...new Set((ids || []).filter(Boolean).map(String))];
    if (!list.length) return;
    let failed = 0;
    /* 逐条删；单条失败不中断，最后统一按服务端真实状态刷新，避免本地 splice 漂移 */
    for (const id of list) {
      try { await send("/api/bible/highlight/delete", { id }); }
      catch (_) { failed += 1; }
    }
    hideSelbar();
    window.getSelection()?.removeAllRanges();
    if ($("#bl-reader")) await refreshChapter();
    else routeNow();
    invalidateState();
    if (failed) toastIt(`${failed} 条没能取消，已按服务端状态刷新`, "warn");
    else toastIt(list.length > 1 ? `已取消 ${list.length} 条划重点` : "划重点已取消");
  }

  async function toggleBookmark(verse) {
    const list = (S.chapter && S.chapter.bookmarks) || [];
    const old = list.find((x) => Number(x.verse) === Number(verse));
    if (old) {
      await send("/api/bible/bookmark/delete", { id: old.id });
      await refreshChapter();
      invalidateState(); toastIt("书签已移除");
      return;
    }
    const label = await promptIt("书签标签", `${bookName(S.book)} ${S.chapterNo}:${verse}`);
    if (label == null) return;
    const r = await send("/api/bible/bookmark", {
      book: S.book, chapter: S.chapterNo, verse,
      label: label || `${bookName(S.book)} ${S.chapterNo}:${verse}`,
    });
    await refreshChapter();
    invalidateState();
    toastIt(r && r.removed ? "书签已移除" : "已加书签");
  }

  async function noteModal(note, preset) {
    const p = preset || {
      book: note.book || S.book, chapter: note.chapter || S.chapterNo,
      verse: note.verse || 1, title: note.title || "", content: note.content_md || "",
    };
    const v = await blModal({
      title: note ? "编辑灵修笔记" : "新建灵修笔记", okText: "保存笔记", wide: true,
      body: `<label class="bl-field"><span>标题</span>
          <input name="title" type="text" value="${esc(note ? note.title : p.title)}" placeholder="给这篇笔记起个名字"></label>
        <div class="bl-ref-static">出处：${esc(refLabel(p.book, p.chapter, p.verse))}</div>
        <label class="bl-field"><span>正文（支持 Markdown）</span>
          <textarea name="content_md" rows="9" data-md-preview
            placeholder="写下读经时的感动、查考与疑问">${esc(note ? note.content_md : p.content)}</textarea></label>`,
      onOpen: (box) => bindMd(box),
    });
    if (!v) return;
    const title = String(v.title || "").trim();
    const content = String(v.content_md || "").trim();
    if (!title && !content) { toastIt("笔记是空的", "warn"); return; }
    await send("/api/bible/note", {
      ...(note && note.id ? { id: note.id } : {}),
      book: p.book, chapter: Number(p.chapter) || 1, verse: Number(p.verse) || 1,
      title: title || "无标题笔记", content_md: content,
    });
    invalidateState();
    if ($("#bl-reader")) await refreshChapter();
    else routeNow();
    toastIt("笔记已保存");
  }

  async function runAI(action, question, selection) {
    if (S.aiBusy) return;
    S.aiBusy = true;
    S.aiOutput = null;
    const out = $("#bl-ai-out");
    if (out) out.innerHTML = aiOutHtml();
    try {
      const r = await send("/api/bible/ai", {
        action, book: S.book, chapter: S.chapterNo,
        question: question || "", selection: selection || "",
      });
      S.aiOutput = {
        text: r.text || "", model: r.model || "", context: r.context || "",
        actionLabel: action === "summary" ? "总结本章"
          : (action === "chat" ? "自由提问" : "解释经文"),
        question: question || "",
      };
    } catch (e) {
      S.aiOutput = {
        text: `**请求失败**\n\n${String((e && e.message) || "未知错误")}`,
        model: "", context: "", actionLabel: "出错了", question: "",
      };
    } finally {
      S.aiBusy = false;
      const box = $("#bl-ai-out");
      if (box) box.innerHTML = aiOutHtml();
    }
  }

  function setDrawer(side) {
    const r = $("#bl-reader");
    if (!r) return;
    if (narrow()) {
      if (side === "left") S.drawerLeft = !S.drawerLeft;
      else if (side === "right") S.drawerRight = !S.drawerRight;
      else { S.drawerLeft = false; S.drawerRight = false; }
      r.classList.toggle("show-left", S.drawerLeft);
      r.classList.toggle("show-right", S.drawerRight);
      const scrim = $("#bl-scrim");
      if (scrim) scrim.hidden = !(S.drawerLeft || S.drawerRight);
      return;
    }
    if (side === "left") P.leftOpen = !P.leftOpen;
    else if (side === "right") P.rightOpen = !P.rightOpen;
    else return;
    savePrefs();
    r.classList.toggle("no-left", !P.leftOpen);
    r.classList.toggle("no-right", !P.rightOpen);
  }

  /* ── 事件：容器委托（只注册一次，不随视图重绘泄漏）─── */

  main.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest("[data-bl-act]");
    if (!el || !main.contains(el)) return;
    const act = el.dataset.blAct;
    if (act === "scrim") { setDrawer(""); return; }
    if (el.disabled) return;
    Promise.resolve(dispatch(act, el)).catch((err) => {
      if (!(err && err.__blToasted)) toastIt((err && err.message) || String(err), "err");
    });
  });

  main.addEventListener("mousedown", (e) => {
    const bar = $("#bl-selbar");
    if (bar && !bar.hidden && !bar.contains(e.target)
      && !(e.target.closest && e.target.closest(".bl-vtext"))) hideSelbar();
  });

  const onSelectEnd = (e) => {
    if (!document.body.classList.contains("bl-active")) return;
    const art = $("#bl-article");
    if (!art) return;
    if (e && e.target && !art.contains(e.target)) return;
    setTimeout(onSelection, 0);
  };
  main.addEventListener("mouseup", onSelectEnd);
  main.addEventListener("touchend", onSelectEnd);

  let selTimer = 0;
  document.addEventListener("selectionchange", () => {
    if (!document.body.classList.contains("bl-active") || !$("#bl-article")) return;
    clearTimeout(selTimer);
    selTimer = setTimeout(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) return;
      const node = sel.anchorNode;
      const art = $("#bl-article");
      if (node && art && art.contains(node.nodeType === 3 ? node.parentNode : node)) onSelection();
    }, 200);
  });

  document.addEventListener("keydown", (e) => {
    if (!document.body.classList.contains("bl-active")) return;
    const inField = e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName);
    if (e.key === "Escape") {
      const r = $("#bl-reader");
      if (r && (r.classList.contains("show-left") || r.classList.contains("show-right"))) setDrawer("");
      hideSelbar();
      return;
    }
    /* 有对话框时不抢方向键：让对话框自己处理 */
    if ($(".bl-modal-backdrop, .gl-modal-backdrop, .pal-backdrop")) return;
    if (inField || !$("#bl-reader")) return;
    if (e.key === "[") { e.preventDefault(); setDrawer("left"); }
    else if (e.key === "]") { e.preventDefault(); setDrawer("right"); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); dispatch("prev", null).catch(() => { }); }
    else if (e.key === "ArrowRight") { e.preventDefault(); dispatch("next", null).catch(() => { }); }
  });

  main.addEventListener("click", (e) => {
    if (e.target && e.target.id === "bl-scrim") setDrawer("");
  });

  /* 跨过 900px 断点时重新落位：宽屏按偏好折叠，窄屏收起抽屉。
     只注册一次，不做逐视图监听，避免泄漏。 */
  let wasNarrow = narrow();
  window.addEventListener("resize", () => {
    const isN = narrow();
    if (isN === wasNarrow) return;
    wasNarrow = isN;
    const scrim = $("#bl-scrim");
    if (scrim) scrim.hidden = true;
    S.drawerLeft = false; S.drawerRight = false;
    const r = $("#bl-reader");
    if (!r) return;
    r.classList.remove("show-left", "show-right");
    if (isN) r.classList.remove("no-left", "no-right");
    else {
      r.classList.toggle("no-left", !P.leftOpen);
      r.classList.toggle("no-right", !P.rightOpen);
    }
  });

  /* ── 路由注册 ───────────────────────────────────────── */

  /* 三个板块并列后，切换器需要三列；用 bl- 前缀的类补，不改 reading.css */
  const sectSwitch = $("#sect-switch");
  if (sectSwitch) sectSwitch.classList.add("bl-sect-3");

  const Q = "(?:\\?.*)?$";
  addRoute(new RegExp("^\\/bible" + Q), viewHome);
  addRoute(new RegExp("^\\/bible\\/read\\/([a-z0-9]+)\\/(\\d+)" + Q),
    (m) => viewRead(m[1], Number(m[2])));
  addRoute(new RegExp("^\\/bible\\/search" + Q), viewSearch);
  addRoute(new RegExp("^\\/bible\\/notes" + Q), viewNotes);
  addRoute(new RegExp("^\\/bible\\/import" + Q), viewImport);
  addRoute(new RegExp("^\\/bible\\/highlights" + Q), viewHighlights);

  window.GLBible = { onRoute };
})();
