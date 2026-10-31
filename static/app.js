/* 观澜 GUANLAN · 前端
   无框架、无构建。hash 路由 + /api + 手写 SVG。
   视图：overview · canon(+detail) · entry · series(+detail)
         conflicts · predictions · decisions · ai · library */

(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const main = $("#main");

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const api = async (p, opts) => {
    const r = await fetch(p, opts);
    const j = await r.json();
    if (j && j.error) throw new Error(j.error);
    return j;
  };
  const post = (p, body) => api(p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

  const fmt = (v, d = 2) => {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    return isFinite(n) ? n.toFixed(d).replace(/\.?0+$/, "") || "0" : esc(v);
  };

  const DOMAINS = {
    economy: "经济", population: "人口", history: "历史",
    society: "社会", culture: "文化", world: "世界", cross: "跨域",
  };
  const TIERS = ["T0", "T1", "T2", "T3", "T4"];
  const CEIL = { T0: 0.95, T1: 0.85, T2: 0.70, T3: 0.50, T4: 0.25 };

  /* ── 片段 ──────────────────────────────────────────── */

  const tierBadge = (t) => t ? `<span class="badge ${String(t).toLowerCase()}">${esc(t)}</span>` : "";
  const statusBadge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;

  const head = (title, meta) => `
    <div class="head"><h1>${esc(title)}</h1>
    <div class="meta">${meta || ""}</div></div>`;

  const panel = (title, sub, body, flush) => `
    <div class="panel"><div class="panel-head">
      <div class="panel-title">${esc(title)}</div>
      <div class="panel-sub">${sub || ""}</div></div>
      <div class="panel-body ${flush ? "flush" : ""}">${body}</div></div>`;

  const empty = (big, hint) => `<div class="empty"><div class="big">${big}</div>
    ${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;

  const back = (href, label) =>
    `<div style="margin-bottom:14px"><a href="${href}" class="mono" style="font-size:11.5px">← ${esc(label)}</a></div>`;

  const claimRow = (c) => `
    <div class="claim-row" data-claim="${esc(c.code)}">
      <div class="claim-id">${esc(c.code)}</div>
      <div>
        <div class="claim-st">${esc(c.kind === "question" ? (c.question || c.statement) : c.statement)}</div>
        <div class="claim-meta">
          ${c.kind === "question" ? `<span class="badge q">待验证</span>` : tierBadge(c.tier)}
          ${statusBadge(c.status)}
          <span>${esc(DOMAINS[c.domain] || c.domain)}</span>
          <span>as_of ${esc(c.as_of)}</span>
          ${(c.entities || []).slice(0, 3).map((e) => `<span>${esc(e)}</span>`).join("")}
        </div>
      </div>
      <div class="claim-conf">${c.kind === "claim" && c.confidence != null ? Number(c.confidence).toFixed(2) : ""}</div>
    </div>`;

  function applyStagger(root) {
    root.querySelectorAll(".stat-grid, .panel-body.flush, .form-grid").forEach((box) => {
      if (box.classList.contains("stagger")) return;
      box.classList.add("stagger");
      Array.from(box.children).forEach((el, i) =>
        el.style.setProperty("--i", Math.min(i, 14)));
    });
  }

  /* ── 图表引擎 ──────────────────────────────────────── */

  function lineChart(points, opt = {}) {
    const W = opt.w || 900, H = opt.h || 240;
    const pad = { l: 44, r: 16, t: 20, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const pts = points.filter((p) => p.v !== null && p.v !== undefined);
    if (!pts.length) return `<svg class="chart" viewBox="0 0 ${W} ${H}">
      <text x="${W/2}" y="${H/2}" text-anchor="middle">无数据</text></svg>`;

    let lo = Math.min(...pts.map(p => p.v)), hi = Math.max(...pts.map(p => p.v));
    if (lo === hi) { lo -= 1; hi += 1; }
    const span = hi - lo; lo -= span * .12; hi += span * .12;
    const X = (i) => pad.l + (pts.length === 1 ? iw/2 : i / (pts.length - 1) * iw);
    const Y = (v) => pad.t + ih - (v - lo) / (hi - lo) * ih;

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const v = lo + (hi - lo) * i / 4, y = Y(v);
      grid += `<line class="grid-line" x1="${pad.l}" y1="${y.toFixed(1)}" x2="${W-pad.r}" y2="${y.toFixed(1)}"/>`;
      grid += `<text x="${pad.l-7}" y="${(y+3.5).toFixed(1)}" text-anchor="end">${fmt(v,1)}</text>`;
    }
    // 修订标记：同一 period 出现多次 vintage 的点
    let revMarks = "";
    (opt.revisions || []).forEach((rv) => {
      const idx = pts.findIndex((p) => p.p === rv.period);
      if (idx < 0) return;
      revMarks += `<circle class="rev-dot" cx="${X(idx).toFixed(1)}" cy="${Y(pts[idx].v).toFixed(1)}" r="4"/>`;
      revMarks += `<text class="rev-label" x="${(X(idx)+6).toFixed(1)}" y="${(pad.t+10).toFixed(1)}">修订</text>`;
    });

    const nlab = Math.max(2, Math.min(8, Math.floor(iw / 72)));
    let xlab = "";
    for (let i = 0; i < nlab; i++) {
      const idx = Math.round(i / (nlab - 1) * (pts.length - 1));
      const anchor = i === 0 ? "start" : i === nlab - 1 ? "end" : "middle";
      xlab += `<text x="${X(idx).toFixed(1)}" y="${H-8}" text-anchor="${anchor}">${esc(pts[idx].p)}</text>`;
    }

    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.v).toFixed(1)}`).join(" ");
    const last = pts[pts.length - 1];
    return `<svg class="chart animate" viewBox="0 0 ${W} ${H}" data-chart='${esc(JSON.stringify({ pts, pad, W, iw, ih, lo, hi }))}'>
      ${grid}
      <path class="series-line" d="${d}" pathLength="1"/>
      <line class="cursor-line" x1="0" y1="${pad.t}" x2="0" y2="${pad.t+ih}" style="display:none"/>
      <circle class="cursor-dot" r="3" style="display:none"/>
      <circle class="last-halo" cx="${X(pts.length-1).toFixed(1)}" cy="${Y(last.v).toFixed(1)}" r="7"/>
      <circle class="last-dot" cx="${X(pts.length-1).toFixed(1)}" cy="${Y(last.v).toFixed(1)}" r="3"/>
      ${revMarks}${xlab}</svg>`;
  }

  function mountChart(host, points, tipEl, opt) {
    if (!host) return;
    host.innerHTML = lineChart(points, opt);
    const svg = host.querySelector("svg");
    if (!svg) return;
    const meta = JSON.parse(svg.dataset.chart || "{}");
    const line = svg.querySelector(".cursor-line"), dot = svg.querySelector(".cursor-dot");
    svg.addEventListener("mousemove", (ev) => {
      const box = svg.getBoundingClientRect();
      const x = (ev.clientX - box.left) / box.width * meta.W;
      if (x < meta.pad.l || x > meta.W - meta.pad.r) {
        line.style.display = "none"; dot.style.display = "none";
        if (tipEl) tipEl.innerHTML = tipEl.dataset.base || "";
        return;
      }
      const i = Math.max(0, Math.min(meta.pts.length - 1,
        Math.round((x - meta.pad.l) / meta.iw * (meta.pts.length - 1))));
      const p = meta.pts[i];
      const cx = meta.pad.l + i / Math.max(1, meta.pts.length - 1) * meta.iw;
      const cy = meta.pad.t + meta.ih - (p.v - meta.lo) / (meta.hi - meta.lo) * meta.ih;
      line.setAttribute("x1", cx); line.setAttribute("x2", cx); line.style.display = "";
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.style.display = "";
      if (tipEl) tipEl.innerHTML = `<span class="mono">${esc(p.p)}</span> · <strong>${fmt(p.v)}</strong> · vintage ${esc(p.vintage || "")}`;
    });
    svg.addEventListener("mouseleave", () => {
      line.style.display = "none"; dot.style.display = "none";
      if (tipEl) tipEl.innerHTML = tipEl.dataset.base || "";
    });
  }

  function calibrationChart(bins) {
    const W = 380, H = 250, pad = 42;
    const iw = W - pad * 2, ih = H - pad * 2;
    const X = (v) => pad + v * iw, Y = (v) => pad + ih - v * ih;
    let g = "";
    for (let i = 0; i <= 4; i++) {
      const v = i / 4;
      g += `<line class="grid-line" x1="${pad}" y1="${Y(v)}" x2="${W-pad}" y2="${Y(v)}"/>`;
      g += `<text x="${pad-8}" y="${Y(v)+3}" text-anchor="end">${Math.round(v*100)}%</text>`;
      g += `<text x="${X(v)}" y="${H-pad+15}" text-anchor="middle">${Math.round(v*100)}%</text>`;
    }
    let dots = "";
    (bins || []).forEach((b) => {
      if (!b.n) return;
      const cx = X((b.lo + b.hi) / 2), cy = Y(b.observed);
      // 误差棒（二项标准误）
      const se = Math.sqrt(Math.max(b.observed * (1 - b.observed), 0.02) / b.n);
      dots += `<line class="cal-err" x1="${cx}" y1="${Y(Math.min(1, b.observed + se))}" x2="${cx}" y2="${Y(Math.max(0, b.observed - se))}"/>`;
      dots += `<circle class="cal-dot" cx="${cx}" cy="${cy}" r="4.5"/>`;
    });
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:440px;height:auto">
      ${g}<line class="perfect-line" x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}"/>${dots}
      <text x="${W/2}" y="${H-6}" text-anchor="middle" class="axis-title">声称概率 →</text>
      <text x="12" y="${H/2}" text-anchor="middle" class="axis-title" transform="rotate(-90 12 ${H/2})">实际频率 →</text></svg>`;
  }

  /* ── 视图：总览 ────────────────────────────────────── */

  async function viewOverview() {
    const d = await api("/api/overview");
    const s = d.stats;
    const conf = Number(s.t0) > 0
      ? (s.t0 / Math.max(1, s.active) * 100).toFixed(0) : "—";

    const recent = (d.recent || []).map(claimRow).join("");
    const due = (d.due_predictions || []).map((p) => `
      <div class="claim-row">
        <div class="claim-id">${esc(p.code)}</div>
        <div><div class="claim-st">${esc(p.proposition)}</div>
          <div class="claim-meta"><span>到期 ${esc(p.resolve_by)}</span>
          <span>概率 ${(p.probability * 100).toFixed(0)}%</span></div></div>
        <div class="claim-conf"></div></div>`).join("");

    const brier = d.calibration && d.calibration.brier_avg != null
      ? d.calibration.brier_avg : null;

    main.innerHTML = head("总览", "认知仪表盘 · 不看条目数，看判断力")
      + `<div class="stat-grid">
          <div class="stat"><div class="n">${s.active}</div><div class="l">正典 active</div></div>
          <div class="stat gold"><div class="n">${conf === "—" ? "—" : conf + "%"}</div><div class="l">T0 覆盖率</div></div>
          <div class="stat ${s.conflicts ? "warn" : ""}"><div class="n">${s.conflicts}</div><div class="l">矛盾发现（越多越活）</div></div>
          <div class="stat"><div class="n">${s.predictions_open}</div><div class="l">在册预测</div></div>
          <div class="stat ${brier != null && brier >= 0.25 ? "warn" : brier != null ? "gold" : ""}">
            <div class="n">${brier != null ? brier.toFixed(3) : "—"}</div><div class="l">Brier 均值（&lt;0.25 有价值）</div></div>
          <div class="stat"><div class="n">${s.questions}</div><div class="l">待验证命题</div></div>
          <div class="stat"><div class="n">${s.series}</div><div class="l">时序序列</div></div>
          <div class="stat"><div class="n">${s.calibers}</div><div class="l">口径变更记录</div></div>
        </div>`
      + (d.due_predictions && d.due_predictions.length
        ? panel("到期预测", "结算是对判断力的唯一投资", due, true) : "")
      + panel("最新正典", "", recent || empty("正典为空", "去「录入」页建第一条 claim——falsifier 是必填的"), true);

    $$("[data-claim]").forEach((el) => el.addEventListener("click", () => {
      location.hash = `#/canon/${el.dataset.claim}`;
    }));
  }

  /* ── 视图：正典 ────────────────────────────────────── */

  let canonFilter = { kind: "all", tier: "all", domain: "all" };

  async function viewCanon() {
    const q = new URLSearchParams();
    if (canonFilter.kind !== "all") q.set("kind", canonFilter.kind);
    if (canonFilter.tier !== "all") q.set("tier", canonFilter.tier);
    if (canonFilter.domain !== "all") q.set("domain", canonFilter.domain);
    const d = await api(`/api/claims?${q}`);

    const chips = (arr, key, labelFn) => arr.map((v) =>
      `<span class="chip ${canonFilter[key] === v ? "on" : ""}" data-k="${key}" data-v="${esc(v)}">
        ${esc(v === "all" ? "全部" : (labelFn ? labelFn(v) : v))}</span>`).join("");

    const kindLabel = (v) => v === "claim" ? "命题" : v === "question" ? "待验证" : v;

    main.innerHTML = head("正典", `${d.claims.length} 条 · Claim 是原子，不是文章`)
      + `<div class="filters">
          <span class="f-label">类型</span>${chips(["all", "claim", "question"], "kind", kindLabel)}
          <span class="f-label">层级</span>${chips(["all", ...TIERS], "tier")}
          <span class="f-label">域</span>${chips(["all", ...Object.keys(DOMAINS)], "domain", (v) => DOMAINS[v])}
        </div>`
      + `<div class="filters">
          <input type="text" id="search-box" placeholder="全文检索（FTS5 trigram，支持中文子串）…" style="flex:1">
        </div>`
      + panel("条目", "点击查看详情", d.claims.map(claimRow).join("") || empty("没有匹配条目"), true);

    $$(".chip").forEach((el) => el.addEventListener("click", () => {
      canonFilter[el.dataset.k] = el.dataset.v;
      viewCanon();
    }));
    let timer = null;
    $("#search-box").addEventListener("input", (e) => {
      clearTimeout(timer);
      const term = e.target.value.trim();
      timer = setTimeout(async () => {
        if (!term) return viewCanon();
        const r = await api(`/api/search?q=${encodeURIComponent(term)}`);
        const box = $(".panel-body.flush");
        box.innerHTML = r.results.map(claimRow).join("") || empty("检索无结果", "trigram 分词至少需要 2 个字符");
        bindClaims(box);
      }, 250);
    });
    bindClaims(main);
  }

  function bindClaims(root) {
    root.querySelectorAll("[data-claim]").forEach((el) =>
      el.addEventListener("click", () => {
        location.hash = `#/canon/${el.dataset.claim}`;
      }));
  }

  async function viewClaim(code) {
    const c = await api(`/api/claims/${encodeURIComponent(code)}`);
    const barW = c.confidence != null ? Math.round(c.confidence * 100) : 0;
    const ceilW = Math.round((c.ceiling || 1) * 100);

    const isQ = c.kind === "question";
    main.innerHTML = back("#/canon", "返回正典")
      + head(c.code, isQ ? "待验证命题 · 等 T0/T1 来裁决" : esc(c.domain_label) + " · " + c.status)
      + `<div class="detail-grid">
          <div class="detail-cell"><div class="k">TIER</div><div class="v">${tierBadge(c.tier)}</div></div>
          <div class="detail-cell"><div class="k">AS_OF</div><div class="v">${esc(c.as_of)}</div></div>
          <div class="detail-cell"><div class="k">OBSERVED_AT</div><div class="v">${esc(c.observed_at)}</div></div>
          <div class="detail-cell"><div class="k">STATUS</div><div class="v">${statusBadge(c.status)}</div></div>
          <div class="detail-cell"><div class="k">ENTITIES</div><div class="v">${esc((c.entities || []).join(" · ") || "—")}</div></div>
          <div class="detail-cell"><div class="k">SOURCE</div><div class="v">${c.source ? esc(c.source.code) + " " + tierBadge(c.source.tier) : "—"}</div></div>
        </div>`
      + (!isQ ? panel("置信度", `${c.tier} 上限 ${(c.ceiling * 100).toFixed(0)}%（I5 硬约束）`,
          `<div class="conf-bar"><div class="conf-fill" style="width:${barW}%"></div>
             <div class="conf-ceil" style="left:${ceilW}%"></div></div>
           <div class="conf-legend"><span>当前 <strong>${c.confidence?.toFixed?.(2) ?? "—"}</strong></span>
             <span>层级上限 <span class="mono">${c.ceiling?.toFixed?.(2) ?? "—"}</span></span></div>`) : "")
      + panel(isQ ? "疑问" : "命题", "",
          `<div class="prose"><p style="font-size:14.5px">${esc(isQ ? (c.question || c.statement) : c.statement)}</p></div>`)
      + (!isQ ? panel("证伪条件", "I8 · 什么情况下这条是错的",
          `<div class="prose"><p>${esc(c.falsifier)}</p></div>`) :
          panel("裁决标准", "满足即转正典", `<div class="prose"><p>${esc(c.resolution || "—")}</p></div>`))
      + (c.notes ? panel("判断依据", "", `<div class="prose">${esc(c.notes).replace(/\n/g, "<br>")}</div>`) : "")
      + panel("操作", "状态迁移是人工签署（I6）", `
          <div class="filters">
            <button class="btn small ghost" data-set="superseded">标记 superseded</button>
            <button class="btn small ghost" data-set="contradicted">标记 contradicted</button>
            <button class="btn small ghost" data-set="retired">退役 retired</button>
            ${isQ ? `<button class="btn small" id="promote">裁决为 claim</button>` : ""}
          </div>`)
      + (isQ ? panel("原始记录", "", `<div class="prose">${esc(c.resolution || c.statement)}</div>`) : "");

    $$("[data-set]").forEach((b) => b.addEventListener("click", async () => {
      await post(`/api/claims/${encodeURIComponent(code)}`, { status: b.dataset.set });
      viewClaim(code);
    }));
    const promo = $("#promote");
    if (promo) promo.addEventListener("click", async () => {
      await post(`/api/claims/${encodeURIComponent(code)}`, { status: "active" });
      viewClaim(code);
    });
  }

  /* ── 视图：录入 ────────────────────────────────────── */

  async function viewEntry() {
    const [srcs, seriesList] = await Promise.all([
      api("/api/sources"), api("/api/series")]);

    main.innerHTML = head("录入", "AI 提议，人批准 —— 手工录入同样过三关")
      + panel("新建条目", "表单即校验：模糊词 / tier 上限 / falsifier 全部硬约束", `
        <div class="form-grid">
          <div class="form-row">
            <label>类型 <span class="req">*</span></label>
            <select id="e-kind">
              <option value="claim">Claim · 可判定真伪的命题</option>
              <option value="question">待验证命题 · 观点/疑问（T3 的合法形态）</option>
            </select>
          </div>
          <div class="form-row">
            <label>域 <span class="req">*</span></label>
            <select id="e-domain">${Object.entries(DOMAINS)
              .map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
          </div>
          <div class="form-row full">
            <label>Statement（可判定真伪的一句话）<span class="req">*</span></label>
            <textarea id="e-statement" placeholder="禁用：可能 / 或许 / 据说 / 大概 —— 有这些词请改用待验证命题"></textarea>
          </div>
          <div class="form-row full hide" id="e-q-wrap">
            <label>疑问句</label>
            <input type="text" id="e-question" placeholder="把它变成一个可裁决的问题">
            <div class="hint">裁决标准：满足什么条件时这个问题算有了答案</div>
          </div>
          <div class="form-row full hide" id="e-res-wrap">
            <label>裁决标准</label>
            <input type="text" id="e-resolution" placeholder="用什么数据、对照什么指标来裁决">
          </div>
          <div class="form-row">
            <label>证据层级 Tier <span class="req">*</span></label>
            <select id="e-tier">${TIERS.map((t) =>
              `<option value="${t}">${t} · 上限 ${CEIL[t]}</option>`).join("")}</select>
            <div class="hint">T3/T4 不能作为证据 claim（I7），只能开待验证命题</div>
          </div>
          <div class="form-row">
            <label>Confidence <span class="req">*</span></label>
            <input type="number" id="e-conf" min="0" max="0.95" step="0.01" value="0.70">
            <div class="hint" id="e-conf-hint">当前层级上限 0.70（I5）</div>
          </div>
          <div class="form-row">
            <label>as_of（事实时点）<span class="req">*</span></label>
            <input type="date" id="e-asof">
            <div class="hint">事实本身发生在什么时候（I3）</div>
          </div>
          <div class="form-row">
            <label>observed_at（看到时点）<span class="req">*</span></label>
            <input type="date" id="e-obs">
          </div>
          <div class="form-row">
            <label>来源</label>
            <select id="e-source"><option value="">（无 / 后补）</option>
              ${srcs.sources.map((s) => `<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.title)}</option>`).join("")}
            </select>
          </div>
          <div class="form-row">
            <label>实体（逗号分隔）</label>
            <input type="text" id="e-entities" placeholder="GDP, 人口, 某政策">
            <div class="hint">实体用于矛盾检测的配对（同实体+同年）</div>
          </div>
          <div class="form-row full">
            <label>Falsifier · 什么情况下这条是错的 <span class="req">*</span></label>
            <textarea id="e-falsifier" placeholder="写不出来，说明这不是 claim，是态度（I8）"></textarea>
          </div>
          <div class="form-row full">
            <label>Notes（判断依据 / 数字口径）</label>
            <textarea id="e-notes" placeholder="为什么是这个置信度而不是更高？数字的算法是什么？"></textarea>
          </div>
        </div>
        <div style="margin-top:16px;display:flex;gap:10px;align-items:center">
          <button class="btn" id="e-submit">提入正典</button>
          <span class="faint" id="e-msg"></span>
        </div>`)

      + panel("批量导入（JSON）", "claims 数组或 series 对象，同一套校验", `
          <div class="form-row">
            <textarea id="e-json" style="min-height:120px;font-family:var(--mono);font-size:12px"
              placeholder='{"claims":[{"statement":"...","tier":"T1","confidence":0.8,"as_of":"2025-12-31","observed_at":"2026-10-31","falsifier":"..."}],
  "series":{"code":"CN.CPI.YOY","name":"CPI同比","unit":"%","rows":[{"period":"2026-01","value":"0.5"}]}}'></textarea>
          </div>
          <div style="margin-top:10px"><button class="btn ghost" id="e-import">导入 JSON</button>
          <span class="faint" id="e-import-msg"></span></div>`);

    const today = new Date().toISOString().slice(0, 10);
    $("#e-asof").value = today;
    $("#e-obs").value = today;

    $("#e-kind").addEventListener("change", (e) => {
      const isQ = e.target.value === "question";
      $("#e-q-wrap").classList.toggle("hide", !isQ);
      $("#e-res-wrap").classList.toggle("hide", !isQ);
    });
    $("#e-tier").addEventListener("change", (e) => {
      $("#e-conf-hint").textContent = `当前层级上限 ${CEIL[e.target.value]}（I5）`;
    });

    $("#e-submit").addEventListener("click", async () => {
      const msg = $("#e-msg");
      msg.textContent = "";
      try {
        const r = await post("/api/claims", {
          kind: $("#e-kind").value,
          statement: $("#e-statement").value,
          question: $("#e-question").value,
          resolution: $("#e-resolution").value,
          domain: $("#e-domain").value,
          tier: $("#e-tier").value,
          confidence: $("#e-conf").value,
          as_of: $("#e-asof").value,
          observed_at: $("#e-obs").value,
          source_code: $("#e-source").value,
          entities: $("#e-entities").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          falsifier: $("#e-falsifier").value,
          notes: $("#e-notes").value,
        });
        msg.innerHTML = `<span style="color:var(--green)">✓ 已录入 ${esc(r.code)}</span>`;
        refreshSidebar();
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });

    $("#e-import").addEventListener("click", async () => {
      const msg = $("#e-import-msg");
      try {
        const payload = JSON.parse($("#e-json").value);
        const r = await post("/api/import", payload);
        msg.innerHTML = `<span style="color:var(--green)">✓ claims ${r.claims} · obs ${r.observations}` +
          (r.errors.length ? ` · 错误 ${r.errors.length} 条（详见控制台）` : "") + `</span>`;
        if (r.errors.length) console.warn(r.errors);
        refreshSidebar();
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });
  }

  /* ── 视图：时序 ────────────────────────────────────── */

  async function viewSeries() {
    const d = await api("/api/series");
    const rows = d.series.map((s) => `
      <div class="claim-row" data-code="${esc(s.code)}">
        <div class="claim-id">${esc(s.freq)}</div>
        <div><div class="claim-st">${esc(s.name)}</div>
          <div class="claim-meta"><span>${esc(s.code)}</span><span>${esc(s.unit)}</span>
          <span>${s.n_obs} 观测</span><span>${s.n_vintage} vintage</span></div></div>
        <div class="claim-conf">${esc(s.latest_period || "—")}</div>
      </div>`).join("");
    main.innerHTML = head("时序", "I4 · 只追加，永不覆盖——旧值就是当时的你")
      + panel("序列注册表", "点击查看走势与修订", rows || empty("还没有序列", "录入页底部可 JSON 导入"), true);
    $$("[data-code]").forEach((el) => el.addEventListener("click", () => {
      location.hash = `#/series/${el.dataset.code}`;
    }));
  }

  async function viewSeriesDetail(code) {
    const d = await api(`/api/series/${encodeURIComponent(code)}`);
    const s = d.series;
    const tipBase = `最新 ${d.points.length ? esc(d.points[d.points.length - 1].p) : "—"}`;

    const revs = (d.revisions || []).map((r) => `
      <tr><td class="num">${esc(r.period)}</td>
      <td class="num">${r.history.map((h) => `${esc(h.value)} <span class="faint">(${esc(h.vintage)})</span>`).join(" → ")}</td></tr>`).join("");

    const cals = (d.calibers || []).map((c) => `
      <div class="br-item">
        <div class="br-head"><span class="mono faint">${esc(c.changed_at)}</span>
          <span class="br-name">${c.comparable ? "可比" : "不可比"}</span></div>
        <div class="br-q">${esc(c.description)}</div></div>`).join("");

    main.innerHTML = back("#/series", "返回时序")
      + head(s.name, `${esc(s.code)} · ${esc(s.unit)} · ${s.n_vintage} vintage`)
      + `<div class="panel"><div class="panel-head">
          <div class="panel-title">走势（各期取最新 vintage）</div>
          <div class="panel-sub" id="chart-tip" data-base="${esc(tipBase)}">${tipBase}</div></div>
        <div class="panel-body"><div id="chart-host"></div>
          <div class="faint" style="font-size:11.5px;margin-top:10px">
            红点为修订点。跨口径断点的比较必须显式声明，不能连成一条趋势线。</div></div></div>`
      + panel("修订台账", `${(d.revisions || []).length} 个期间被修订（旧值保留）`,
          revs ? `<table class="grid"><thead><tr><th>期间</th><th>历史（旧 → 新）</th></tr></thead><tbody>${revs}</tbody></table>`
            : empty("尚无修订", "修订不常有。一旦出现，旧值会一直留在这里"), true)
      + panel("口径沿革", `${(d.calibers || []).length} 条 · ${esc(s.definition || "")}`,
          cals || empty("无口径变更记录", "口径登记簿是这个平台最稀缺的资产"));
    mountChart($("#chart-host"), d.points, $("#chart-tip"), { revisions: d.revisions });
  }

  /* ── 视图：矛盾 ────────────────────────────────────── */

  async function viewConflicts() {
    const d = await api("/api/conflicts");
    const block = (list, cls) => (list || []).map((x) => `
      <div class="conflict ${cls}">
        <div class="conflict-pair">
          <a href="#/canon/${esc(x.a)}" class="mono">${esc(x.a)}</a><span class="faint">⇄</span>
          <a href="#/canon/${esc(x.b)}" class="mono">${esc(x.b)}</a></div>
        <div class="conflict-reason">${esc(x.reason)}</div>
        ${x.a_tier ? `<div class="conflict-act">
          <button class="btn small ghost" data-keep="${esc(x.a)}" data-drop="${esc(x.b)}">信 ${esc(x.a)} · 降级 ${esc(x.b)}</button>
          <button class="btn small ghost" data-keep="${esc(x.b)}" data-drop="${esc(x.a)}">信 ${esc(x.b)} · 降级 ${esc(x.a)}</button></div>` : ""}
      </div>`).join("");

    main.innerHTML = head("矛盾与关联", `候选 ${d.conflicts.length} · 倒挂 ${d.inverted.length} · 单向 ${d.asymmetric.length}`)
      + `<div class="notice small"><div class="notice-body">
          <strong>零矛盾不一定是好消息</strong>——要么库干净，要么你在灌同质观点。
          每一个矛盾都是认知升级的入口：它逼你回答"我到底信哪个，为什么"。<br>
          ${esc(d.honesty)}</div></div>`
      + panel("候选冲突", "同实体 + 同年 + 数值不同",
          block(d.conflicts, "high") || empty("无候选冲突"), true)
      + panel("置信度倒挂", "低层级反而更自信 = 结构性错误",
          block(d.inverted, "mid") || empty("无倒挂"), true)
      + panel("单向 contradicts", "声明不对称，通常是漏写",
          block(d.asymmetric, "mid") || empty("无单向声明"), true);

    $$("[data-keep]").forEach((b) => b.addEventListener("click", async () => {
      await post(`/api/claims/${b.dataset.drop}`, { status: "contradicted" });
      viewConflicts();
    }));
  }

  /* ── 视图：预测 ────────────────────────────────────── */

  async function viewPredictions() {
    const d = await api("/api/predictions");
    const open = d.predictions.filter((p) => p.status === "open");
    const resolved = d.predictions.filter((p) => p.status === "resolved");

    const row = (p) => `
      <div class="claim-row">
        <div class="claim-id">${esc(p.code)}</div>
        <div><div class="claim-st">${esc(p.proposition)}</div>
          <div class="claim-meta">
            <span>概率 <strong>${(p.probability * 100).toFixed(0)}%</strong></span>
            <span>到期 ${esc(p.resolve_by)}</span>
            ${p.base_rate ? `<span>基准率 ${esc(p.base_rate)}</span>` : ""}
            ${p.pre_mortem ? `<span title="${esc(p.pre_mortem)}">pre-mortem ✓</span>` : ""}</div></div>
        <div class="claim-conf">${p.status === "resolved"
          ? `Brier ${p.brier}` : ""}</div>
      </div>`;

    const settle = open.length ? open.map((p) => `
      <div class="claim-row">
        <div class="claim-id">${esc(p.code)}</div>
        <div><div class="claim-st">${esc(p.proposition)}</div>
          <div class="claim-meta"><span>到期 ${esc(p.resolve_by)}</span></div></div>
        <div class="conflict-act">
          <button class="btn small" data-settle="${esc(p.code)}" data-outcome="1">发生</button>
          <button class="btn small ghost" data-settle="${esc(p.code)}" data-outcome="0">未发生</button></div>
      </div>`).join("") : empty("没有待结算预测", "到期后才结算。日历驱动，不拖不放");

    const cal = d.calibration;
    main.innerHTML = head("预测台账", `${open.length} 在册 · ${cal.n_resolved} 已结算`)
      + `<div class="notice small"><div class="notice-body">
          四要件缺一不可：<strong>可证伪 · 概率化 · 预注册 pre-mortem · 到期结算</strong>。
          没有到期日的预测不是预测，是感想。</div></div>`
      + `<div class="two-col">`
      + panel("在册", "", open.map(row).join("") || empty("台账为空", "第一条预测注定会错。它的价值在于从此你有了会被结算的判断"), true)
      + panel("校准曲线", cal.note, calibrationChart(cal.bins)
          + `<div style="text-align:center;margin-top:8px" class="faint">
              Brier 均值 <strong class="mono">${cal.brier_avg ?? "—"}</strong> · 样本 ${cal.n_resolved} · 对角线=完美校准</div>`)
      + `</div>`
      + panel("到期结算", "判定后 30 天内必须结算，拖延等于放弃学习",
          settle || empty("全部结算完毕"), true)
      + panel("已结算", "每条 Brier 都是你判断力的样本",
          resolved.map(row).join("") || empty("尚无结算记录"), true)
      + panel("登记新预测", "写入即预注册，到期即结算", `
          <div class="form-grid">
            <div class="form-row full"><label>命题（可第三方复核）</label>
              <input type="text" id="p-prop" placeholder="2027年底前，70城一线二手房价较2026年中回升超5%"></div>
            <div class="form-row"><label>概率（0-1，不许『可能会』）</label>
              <input type="number" id="p-prob" min="0.01" max="0.99" step="0.05" value="0.35"></div>
            <div class="form-row"><label>到期日</label>
              <input type="date" id="p-by"></div>
            <div class="form-row full"><label>判定标准（可第三方复核）</label>
              <input type="text" id="p-criteria" placeholder="用什么公开数据、什么口径判定"></div>
            <div class="form-row"><label>基准率类（可选）</label>
              <input type="text" id="p-br" placeholder="BR-MKT-002"></div>
            <div class="form-row"><label>pre-mortem（错了最可能错在哪）</label>
              <input type="text" id="p-pm" placeholder="把政策底当成市场底…"></div>
          </div>
          <div style="margin-top:14px"><button class="btn" id="p-submit">登记</button>
            <span class="faint" id="p-msg"></span></div>`);

    const by = $("#p-by");
    if (by) {
      by.min = today();
      $("#p-submit").addEventListener("click", async () => {
        const msg = $("#p-msg");
        try {
          const r = await post("/api/predictions", {
            proposition: $("#p-prop").value,
            probability: $("#p-prob").value,
            criteria: $("#p-criteria").value,
            resolve_by: by.value,
            base_rate: $("#p-br").value,
            pre_mortem: $("#p-pm").value,
          });
          msg.innerHTML = `<span style="color:var(--green)">✓ ${esc(r.code)} 已预注册</span>`;
          refreshSidebar();
        } catch (e) {
          msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
        }
      });
      $$("[data-settle]").forEach((b) => b.addEventListener("click", async () => {
        const r = await post(`/api/predictions/${b.dataset.settle}/settle`,
          { outcome: Number(b.dataset.outcome) });
        if (r.ok) viewPredictions();
      }));
    }
  }

  function today() { return new Date().toISOString().slice(0, 10); }

  /* ── 视图：决策 ────────────────────────────────────── */

  async function viewDecisions() {
    const d = await api("/api/decisions");
    const rows = d.decisions.map((x) => `
      <div class="claim-row">
        <div class="claim-id">${esc(x.code)}</div>
        <div><div class="claim-st">${esc(x.decision)}</div>
          <div class="claim-meta"><span>${esc(x.date)}</span>
            <span>${esc(x.stakes)}</span><span>${x.reversible ? "可逆" : "不可逆"}</span>
            ${x.outcome ? `<span>结果：${esc(x.outcome)}</span>` : ""}</div></div>
        <div class="claim-conf"></div></div>`).join("");

    main.innerHTML = head("决策日志", `${d.decisions.length} 条 · 决策 → 依赖判断 → 结果 → 归因`)
      + `<div class="notice small"><div class="notice-body">
          这是库里唯一能形成<strong>闭环</strong>的东西。没有它，十年后你只是信息更多，判断力没变。</div></div>`
      + panel("记录", "", rows || empty("还没有决策记录", "决策 + 依赖的 claim + 事后结果，才叫复盘"), true)
      + panel("登记决策", "", `
          <div class="form-grid">
            <div class="form-row full"><label>决策内容</label>
              <input type="text" id="d-dec"></div>
            <div class="form-row"><label>日期</label>
              <input type="date" id="d-date" value="${today()}"></div>
            <div class="form-row"><label>赌注大小</label>
              <select id="d-stakes"><option value="low">低</option>
                <option value="medium" selected>中</option><option value="high">高</option></select></div>
            <div class="form-row full"><label>因为什么（引用 claim codes）</label>
              <input type="text" id="d-because" placeholder="C-2026-0001, C-2026-0004"></div>
          </div>
          <div style="margin-top:14px"><button class="btn" id="d-submit">登记</button>
            <span class="faint" id="d-msg"></span></div>`);

    $("#d-submit").addEventListener("click", async () => {
      try {
        await post("/api/decisions", {
          decision: $("#d-dec").value, date: $("#d-date").value,
          stakes: $("#d-stakes").value, chosen_because: $("#d-because").value,
        });
        viewDecisions();
      } catch (e) {
        $("#d-msg").innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });
  }

  /* ── 视图：AI ──────────────────────────────────────── */

  let aiTab = "ingest";

  async function viewAI() {
    const st = await api("/api/ai/status");
    main.innerHTML = head("AI 三职能", st.ready ? `已连接 · ${esc(st.model)}` : "未配置")
      + (!st.ready ? `<div class="notice"><div class="notice-body">
          <strong>AI 未配置。</strong>设置环境变量后重启服务（OpenAI 兼容端点均可）：<br>
          <code>set GL_AI_BASE_URL=https://api.deepseek.com</code><br>
          <code>set GL_AI_MODEL=deepseek-chat</code><br>
          <code>set GL_AI_KEY=sk-...</code><br>
          未配置时其余功能全部可用——这个平台不靠 AI 也站得住。</div></div>` : "")
      + `<div class="tabs">
          <div class="tab ${aiTab === "ingest" ? "on" : ""}" data-tab="ingest">A · 摄取</div>
          <div class="tab ${aiTab === "ask" ? "on" : ""}" data-tab="ask">B · 综合（引用强制）</div>
          <div class="tab ${aiTab === "devil" ? "on" : ""}" data-tab="devil">C · 反方论证</div>
        </div><div id="ai-body"></div>`;

    $$(".tab").forEach((t) => t.addEventListener("click", () => {
      aiTab = t.dataset.tab;
      viewAI();
    }));

    if (aiTab === "ingest") await aiIngest();
    if (aiTab === "ask") await aiAsk();
    if (aiTab === "devil") await aiDevil();
  }

  async function aiIngest() {
    const drafts = (await api("/api/ai/drafts")).drafts;
    $("#ai-body").innerHTML = panel("粘贴文章", "AI 只能写草稿（I6）——收编前逐条过三关", `
        <div class="form-row">
          <textarea id="ai-text" style="min-height:140px" placeholder="粘贴博主文章 / 资讯 / 视频转写…"></textarea></div>
        <div style="margin-top:10px"><button class="btn" id="ai-run">摄取</button>
          <span class="faint" id="ai-msg"></span></div>
        <div id="ai-drafts" style="margin-top:16px"></div>`);
    $("#ai-run").addEventListener("click", async () => {
      const msg = $("#ai-msg");
      msg.textContent = "摄取中…";
      try {
        const r = await post("/api/ai/ingest", { text: $("#ai-text").value });
        msg.innerHTML = `<span style="color:var(--green)">✓ ${r.count} 条草稿</span>`;
        renderDrafts(r.items);
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });

    function renderDrafts(items) {
      $("#ai-drafts").innerHTML = items.map((it, i) => `
        <div class="draft-card">
          <div>${it.kind === "question"
            ? `<span class="badge q">待验证</span> ${esc(it.question || it.statement)}`
            : `${tierBadge(it.tier)} ${esc(it.statement)}`}</div>
          <div class="claim-meta" style="margin-top:6px">
            <span>tier ${esc(it.tier)} · conf ${esc(it.confidence)}</span>
            <span>as_of ${esc(it.as_of || "?")}</span>
            <span>${esc(it.entities || "")}</span></div>
          ${it.quote ? `<div class="dc-quote">${esc(it.quote)}</div>` : ""}
          <div class="dc-actions">
            <button class="btn small" data-i="${i}">收编进正典</button></div>
        </div>`).join("") || empty("无草稿", "");
      $$("#ai-drafts [data-i]").forEach((b) => b.addEventListener("click", async () => {
        const it = items[Number(b.dataset.i)];
        it.observed_at = today();
        try {
          const r = await post("/api/claims", it);
          b.textContent = `✓ ${r.code}`;
          b.disabled = true;
          refreshSidebar();
        } catch (e) {
          b.outerHTML = `<span class="err">${esc(e.message)}</span>`;
        }
      }));
    }
    renderDrafts(drafts.length ? drafts.flatMap((d) => d.parsed) : []);
  }

  async function aiAsk() {
    $("#ai-body").innerHTML = panel("向正典提问", "每一句必须挂 [C-编号]，无出处的句子已在服务端丢弃", `
        <div class="form-row"><input type="text" id="ask-q"
          placeholder="例如：地方财政现在什么状况？"></div>
        <div style="margin-top:10px"><button class="btn" id="ask-run">提问</button>
          <span class="faint" id="ask-msg"></span></div>
        <div id="ask-out" style="margin-top:16px"></div>`);
    $("#ask-run").addEventListener("click", async () => {
      const msg = $("#ask-msg");
      msg.textContent = "检索 + 生成中…";
      const out = $("#ask-out");
      try {
        const r = await post("/api/ai/ask", { q: $("#ask-q").value });
        msg.innerHTML = `<span style="color:var(--green)">✓ 引用 ${r.used.length} 条 · 丢弃无出处句 ${r.dropped} 句</span>`;
        const html = esc(r.answer)
          .replace(/\[(C-\d{4}-\d{3,4})\]/g, `<a class="answer-cite" href="#/canon/$1">[$1]</a>`);
        out.innerHTML = `<div class="panel"><div class="panel-head">
          <div class="panel-title">回答</div><div class="panel-sub">灰字 = 无出处句已被硬过滤</div></div>
          <div class="panel-body"><div class="ai-box">${html}</div></div></div>`;
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
        out.innerHTML = "";
      }
    });
  }

  async function aiDevil() {
    const d = await api("/api/claims?kind=claim&status=active");
    $("#ai-body").innerHTML = panel("选一条结论", "AI 只用库内证据论证它的反面", `
        <div class="form-row"><select id="dv-code">
          ${d.claims.map((c) => `<option value="${esc(c.code)}">${esc(c.code)} · ${esc(c.statement.slice(0, 40))}…</option>`).join("")}
        </select></div>
        <div style="margin-top:10px"><button class="btn" id="dv-run">论证反面</button>
          <span class="faint" id="dv-msg"></span></div>
        <div id="dv-out" style="margin-top:16px"></div>`);
    $("#dv-run").addEventListener("click", async () => {
      const msg = $("#dv-msg");
      msg.textContent = "论证中…";
      try {
        const r = await post("/api/ai/devil", { code: $("#dv-code").value });
        msg.innerHTML = `<span style="color:var(--green)">✓</span>`;
        $("#dv-out").innerHTML = `<div class="panel"><div class="panel-head">
          <div class="panel-title">反面论证</div><div class="panel-sub">最强反方证据，非最弱</div></div>
          <div class="panel-body"><div class="ai-box">${esc(r.answer)}</div></div></div>`;
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });
  }

  /* ── 视图：库（口径 / 基准率 / 来源）──────────────── */

  let libTab = "calibers";

  async function viewLibrary() {
    const [cal, br, src] = await Promise.all([
      api("/api/calibers"), api("/api/baserates"), api("/api/sources")]);

    const calRows = cal.calibers.map((c) => `
      <div class="br-item">
        <div class="br-head"><span class="mono faint">${esc(c.changed_at)}</span>
          <span class="br-name">${esc(c.series_name || c.series_code)}</span>
          <span class="badge ${c.comparable ? "active" : "contradicted"}">${c.comparable ? "可比" : "不可比"}</span></div>
        <div class="br-q">${esc(c.description)}</div></div>`).join("");

    const brRows = br.baserates.map((b) => `
      <div class="br-item">
        <div class="br-head"><span class="mono faint">${esc(b.code)}</span>
          <span class="br-name">${esc(b.name)}</span>
          <span class="badge ${b.base_rate != null ? "active" : "draft"}">${b.base_rate != null ? "live" : "待回测"}</span></div>
        <div class="br-q">${esc(b.question)}</div>
        <div class="br-meta">
          <span><span class="faint">参考类</span> ${esc(b.reference_class)}</span>
          <span><span class="faint">窗口</span> ${esc(b.window)}</span>
          <span><span class="faint">样本</span> ${b.sample_size ?? "—"}</span>
          <span><span class="faint">基准率</span> ${b.base_rate != null ? (b.base_rate * 100).toFixed(0) + "%" : "—"}</span></div>
        ${b.note ? `<div class="br-note">${esc(b.note)}</div>` : ""}</div>`).join("");

    const srcRows = src.sources.map((s) => `
      <div class="claim-row">
        <div class="claim-id">${esc(s.code)}</div>
        <div><div class="claim-st">${esc(s.title)}</div>
          <div class="claim-meta"><span>${esc(s.publisher || "")}</span>
            <span>${esc(s.published || "")}</span><span>${esc(s.kind)}</span></div></div>
        <div>${tierBadge(s.tier)}</div></div>`).join("");

    main.innerHTML = head("库", "口径登记簿 · 基准率库 · 来源档案")
      + `<div class="tabs">
          <div class="tab ${libTab === "calibers" ? "on" : ""}" data-t="calibers">口径登记簿 ${cal.calibers.length}</div>
          <div class="tab ${libTab === "baserates" ? "on" : ""}" data-t="baserates">基准率库 ${br.baserates.length}</div>
          <div class="tab ${libTab === "sources" ? "on" : ""}" data-t="sources">来源 ${src.sources.length}</div>
        </div>
        <div class="notice small"><div class="notice-body">
          ${libTab === "calibers"
            ? "<strong>口径元数据比数据本身更值钱。</strong>跨口径断点的比较就是错误的来源。积累三年，这是别人没有的资产。"
            : libTab === "baserates"
            ? "<strong>数值刻意留空。</strong>一个编造的基准率会污染此后所有依赖它的预测，比没有危害更大。宁缺毋假。"
            : "<strong>L0 来源层不可变。</strong>链接会死，快照不会——关键网页/PDF 务必本地存档。"}</div></div>`
      + panel(
          libTab === "calibers" ? "口径沿革" : libTab === "baserates" ? "参考类" : "清单", "",
          libTab === "calibers" ? (calRows || empty("无记录"))
            : libTab === "baserates" ? (brRows || empty("无参考类"))
            : (srcRows || empty("无来源")), true);

    $$(".tab").forEach((t) => t.addEventListener("click", () => {
      libTab = t.dataset.t;
      viewLibrary();
    }));
  }

  /* ── 侧栏 / 路由 ───────────────────────────────────── */

  async function refreshSidebar() {
    try {
      const o = await api("/api/overview");
      $("#c-canon").textContent = o.stats.active;
      $("#c-pred").textContent = o.stats.predictions_open;
      const bad = o.stats.conflicts;
      $("#c-conf").textContent = bad || "0";
      $("#c-conf").style.color = bad ? "var(--red)" : "";
      $("#f-db").textContent = `${o.stats.active} claims`;
      const st = await api("/api/ai/status");
      $("#f-ai").textContent = st.ready ? st.model : "未配置";
      $("#f-ai").style.color = st.ready ? "var(--green)" : "";
    } catch (_) { /* 静默 */ }
  }

  const ROUTES = {
    overview: viewOverview,
    canon: (a) => a ? viewClaim(a) : viewCanon(),
    series: (a) => a ? viewSeriesDetail(a) : viewSeries(),
    conflicts: viewConflicts,
    predictions: viewPredictions,
    decisions: viewDecisions,
    ai: viewAI,
    library: viewLibrary,
    entry: viewEntry,
  };

  let routeSeq = 0;

  async function route() {
    const hash = location.hash.replace(/^#\/?/, "") || "overview";
    const slash = hash.indexOf("/");
    const page = slash < 0 ? hash : hash.slice(0, slash);
    const arg = slash < 0 ? "" : decodeURIComponent(hash.slice(slash + 1));

    $$(".nav-item").forEach((el) =>
      el.classList.toggle("active", el.dataset.route === page));

    const seq = ++routeSeq;
    if (!main.querySelector(".loading")) {
      main.classList.add("view-leave");
      await new Promise((r) => setTimeout(r, 110));
      main.classList.remove("view-leave");
      if (seq !== routeSeq) return;
    }
    main.innerHTML = `<div class="loading">载入中…</div>`;
    refreshSidebar();
    try {
      await (ROUTES[page] || ROUTES.overview)(arg);
      if (seq !== routeSeq) return;
      main.classList.remove("view-enter");
      void main.offsetWidth;
      main.classList.add("view-enter");
      applyStagger(main);
    } catch (e) {
      main.innerHTML = head("出错", "") + `<div class="err">${esc(e.message)}</div>`;
    }
    window.scrollTo(0, 0);
  }

  $$(".nav-item").forEach((el) => el.addEventListener("click", () => {
    location.hash = `#/${el.dataset.route}`;
  }));
  window.addEventListener("hashchange", route);
  route();
})();
