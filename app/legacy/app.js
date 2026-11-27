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

  const api = async (p, opts = {}) => {
    const ctrl = new AbortController();
    const timeout = opts.timeout || (p.startsWith("/api/ai/") ? 250000 : 45000);
    const fetchOpts = { ...opts };
    delete fetchOpts.timeout;
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const r = await fetch(p, { ...fetchOpts, signal: ctrl.signal });
      const text = await r.text();
      let j;
      try { j = text ? JSON.parse(text) : {}; }
      catch (_) { throw new Error(`服务返回了非 JSON 响应（HTTP ${r.status}）`); }
      if (j && j.error) throw new Error(j.error);
      if (!r.ok) throw new Error(`请求失败（HTTP ${r.status}）`);
      return j;
    } catch (e) {
      if (e.name === "AbortError") {
        throw new Error(`请求超时（${Math.round(timeout / 1000)}s）。可重试；已保存的内容不会丢失。`);
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

  function toast(message, type = "ok") {
    let host = $("#toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    const item = document.createElement("div");
    item.className = `toast ${type}`;
    item.textContent = message;
    host.appendChild(item);
    setTimeout(() => item.remove(), 5200);
  }

  function downloadJSON(data, name) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

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

  const claimRow = (c) => {
    const conf = c.kind === "claim" && c.confidence != null ? Number(c.confidence) : null;
    return `<div class="claim-row" data-claim="${esc(c.code)}">
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
      <div class="claim-conf">${conf != null
        ? `<span class="conf-mini"><div style="width:${(conf * 100).toFixed(0)}%"></div></span><span>${conf.toFixed(2)}</span>`
        : ""}</div>
    </div>`;
  };

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

  function barChart(chart) {
    const data = chart.data || [];
    const W = 760, H = 280, pad = { l: 58, r: 20, t: 24, b: 64 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    let lo = Math.min(0, ...data.map((p) => p.value));
    let hi = Math.max(0, ...data.map((p) => p.value));
    if (lo === hi) hi = lo + 1;
    const Y = (v) => pad.t + (hi - v) / (hi - lo) * ih;
    const slot = iw / data.length, bw = Math.max(12, Math.min(72, slot * .62));
    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const value = lo + (hi - lo) * i / 4, y = Y(value);
      grid += `<line class="grid-line" x1="${pad.l}" y1="${y}" x2="${W-pad.r}" y2="${y}"/>
        <text x="${pad.l-8}" y="${y+3}" text-anchor="end">${fmt(value, 2)}</text>`;
    }
    const bars = data.map((p, i) => {
      const x = pad.l + slot * i + (slot - bw) / 2;
      const y = Math.min(Y(p.value), Y(0));
      const h = Math.max(1, Math.abs(Y(p.value) - Y(0)));
      return `<rect class="ai-bar" x="${x}" y="${y}" width="${bw}" height="${h}" rx="3"/>
        <text x="${x+bw/2}" y="${Math.max(12, y-6)}" text-anchor="middle">${fmt(p.value, 2)}</text>
        <text x="${x+bw/2}" y="${H-42}" text-anchor="end" transform="rotate(-28 ${x+bw/2} ${H-42})">${esc(p.label)}</text>`;
    }).join("");
    return `<svg class="chart ai-chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(chart.title)}">
      ${grid}<line class="grid-line" x1="${pad.l}" y1="${Y(0)}" x2="${W-pad.r}" y2="${Y(0)}"/>${bars}</svg>`;
  }

  function aiChart(chart) {
    if (!chart || !chart.suitable) {
      return `<div class="chart-feedback">未生成图表：${esc(chart?.reason || "内容不适合可视化")}</div>`;
    }
    if (!Array.isArray(chart.data) || chart.data.length < 2
        || chart.data.some((p) => !p || !Number.isFinite(Number(p.value)))) {
      return `<div class="chart-feedback">未生成图表：已保存的图表数据格式无效，请编辑修正。</div>`;
    }
    const svg = chart.type === "line"
      ? lineChart((chart.data || []).map((p) => ({ p: p.label, v: p.value })), { w: 760, h: 280 })
      : barChart(chart);
    return `<div class="chart-title">${esc(chart.title || "原文数值")}${chart.unit ? ` · ${esc(chart.unit)}` : ""}</div>${svg}
      <details class="chart-data"><summary>查看图表数据</summary><table class="grid"><tbody>
      ${(chart.data || []).map((p) => `<tr><td>${esc(p.label)}</td><td class="num">${fmt(p.value, 4)} ${esc(chart.unit || "")}</td></tr>`).join("")}
      </tbody></table></details>`;
  }

  /* ── 视图：总览 ────────────────────────────────────── */

  async function viewOverview() {
    const seq = routeSeq;
    const d = await api("/api/overview");
    if (seq !== routeSeq) return;
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
    const seq = routeSeq;
    const q = new URLSearchParams();
    if (canonFilter.kind !== "all") q.set("kind", canonFilter.kind);
    if (canonFilter.tier !== "all") q.set("tier", canonFilter.tier);
    if (canonFilter.domain !== "all") q.set("domain", canonFilter.domain);
    const d = await api(`/api/claims?${q}`);
    if (seq !== routeSeq) return;

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
        if (seq !== routeSeq) return;
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
    const seq = routeSeq;
    const c = await api(`/api/claims/${encodeURIComponent(code)}`);
    if (seq !== routeSeq) return;
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
      await post(`/api/claims/${encodeURIComponent(code)}`, { kind: "claim", status: "active" });
      viewClaim(code);
    });
  }

  /* ── 视图：录入 ────────────────────────────────────── */

  async function viewEntry() {
    const seq = routeSeq;
    const [srcs, seriesList] = await Promise.all([
      api("/api/sources"), api("/api/series")]);
    if (seq !== routeSeq) return;

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
          <div style="margin-top:10px" class="form-actions"><button class="btn ghost" id="e-import">导入 JSON</button>
          <label class="btn small ghost file-btn">选择 JSON 文件<input type="file" id="e-json-file" accept=".json"></label>
          <span class="faint" id="e-import-msg"></span></div><div id="e-import-errors"></div>`);

    const today = new Date().toISOString().slice(0, 10);
    $("#e-asof").value = today;
    $("#e-obs").value = today;

    // 从集藏「转录入」带来的预填
    const pre = sessionStorage.getItem("gl-prefill");
    if (pre) {
      sessionStorage.removeItem("gl-prefill");
      try {
        const p = JSON.parse(pre);
        if (p.statement) $("#e-statement").value = p.statement;
        if (p.source_url) {
          const msg0 = $("#e-msg");
          msg0.innerHTML = `<span class="faint">来源链接：${esc(p.source_url)}</span>`;
        }
      } catch (_) { /* 忽略坏预填 */ }
    }

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

    $("#e-json-file").addEventListener("change", async (e) => {
      const file = e.target.files[0]; if (!file) return;
      $("#e-json").value = await file.text(); $("#e-import-msg").textContent = `已读取 ${file.name}`;
    });
    $("#e-import").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const msg = $("#e-import-msg");
      try {
        const payload = JSON.parse($("#e-json").value);
        const r = await post("/api/import", payload);
        msg.innerHTML = `<span style="color:var(--green)">✓ claims ${r.claims} · clips ${r.clips || 0} · obs ${r.observations} · 修订 ${r.revised || 0} · 重复 ${r.duplicates || 0}</span>`;
        $("#e-import-errors").innerHTML = r.errors.length
          ? `<div class="err">${r.errors.map((x) => `<div>${esc(x)}</div>`).join("")}</div>` : "";
        refreshSidebar();
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    }));
  }

  /* ── 视图：时序 ────────────────────────────────────── */

  async function viewSeries() {
    const seq = routeSeq;
    const d = await api("/api/series");
    if (seq !== routeSeq) return;
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
    const seq = routeSeq;
    const d = await api(`/api/series/${encodeURIComponent(code)}`);
    if (seq !== routeSeq) return;
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
    const seq = routeSeq;
    const d = await api("/api/conflicts");
    if (seq !== routeSeq) return;
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
    const seq = routeSeq;
    const d = await api("/api/predictions");
    if (seq !== routeSeq) return;
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
    const seq = routeSeq;
    const d = await api("/api/decisions");
    if (seq !== routeSeq) return;
    const rows = d.decisions.map((x) => `
      <div class="claim-row">
        <div class="claim-id">${esc(x.code)}</div>
        <div><div class="claim-st">${esc(x.decision)}</div>
          <div class="claim-meta"><span>${esc(x.date)}</span>
            <span>${esc(x.stakes)}</span><span>${x.reversible ? "可逆" : "不可逆"}</span>
            ${x.outcome ? `<span style="color:var(--green)">结果：${esc(x.outcome)}</span>
              <span>${esc(x.reviewed_at || "")}</span>` : ""}</div>
          ${!x.outcome ? `<div class="cf-row" style="margin-top:8px">
              <input type="text" id="rv-${esc(x.code)}" placeholder="事后结果（做了之后发生了什么）"
                     style="flex:1;min-width:200px">
              <button class="btn small ghost" data-review="${esc(x.code)}">记录结果</button></div>` : ""}
        </div>
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
            <div class="form-row"><label>可逆性</label><select id="d-reversible">
              <option value="1">可逆</option><option value="0">不可逆</option></select></div>
            <div class="form-row full"><label>依据（可引用 claim code，也可带入研究记录）</label>
              <textarea id="d-because" placeholder="C-2026-0001, C-2026-0004；或研究记录中的结论与待办"></textarea></div>
          </div>
          <div style="margin-top:14px"><button class="btn" id="d-submit">登记</button>
            <span class="faint" id="d-msg"></span></div>`);

    const decisionPrefill = sessionStorage.getItem("gl-decision-prefill");
    if (decisionPrefill) {
      sessionStorage.removeItem("gl-decision-prefill");
      try {
        const p = JSON.parse(decisionPrefill);
        $("#d-dec").value = p.decision || ""; $("#d-because").value = p.chosen_because || "";
        $("#d-dec").focus();
      } catch (_) { /* 忽略损坏的预填 */ }
    }
    $("#d-submit").addEventListener("click", async () => {
      try {
        const because = $("#d-because").value;
        await post("/api/decisions", {
          decision: $("#d-dec").value, date: $("#d-date").value,
          stakes: $("#d-stakes").value, chosen_because: because,
          reversible: $("#d-reversible").value === "1",
          depends_on: [...new Set(because.match(/[CQ]-\d{4}-\d{3,4}/g) || [])],
        });
        viewDecisions();
      } catch (e) {
        $("#d-msg").innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });
    $$("[data-review]").forEach((b) => b.addEventListener("click", async () => {
      const input = $("#rv-" + b.dataset.review);
      try {
        await post(`/api/decisions/${b.dataset.review}/review`, { outcome: input.value });
        viewDecisions();
      } catch (e) {
        input.value = e.message;
      }
    }));
  }

  /* ── 视图：集藏 ────────────────────────────────────── */
  /* 信息从粗到严的三层流：集藏(raw 进料) → 待验证(半结构) → 正典(台账)。
     标签即分类，folksonomy：不预设体系，标签云自动聚合。 */

  let clipFilter = { tag: "", channel: "", archived: "active", q: "", sort: "newest", offset: 0 };
  let editingClip = null;
  let selectedClips = new Set();
  let clipSearchSeq = 0;
  const CLIP_PAGE_SIZE = 40;

  function clipQuery() {
    const q = new URLSearchParams();
    Object.entries(clipFilter).forEach(([key, value]) => {
      if (value !== "" && !(key === "offset" && !value)) q.set(key, value);
    });
    q.set("limit", CLIP_PAGE_SIZE);
    return q;
  }

  function clipListMarkup(list) {
    const rows = list.clips.length
      ? `<div class="clip-grid">${list.clips.map((c) => clipCard(c)).join("")}</div>`
      : empty("没有匹配剪藏", "可清空搜索或切换归档状态；新内容从上方链接、文本或文件进入");
    const page = Math.floor(list.offset / list.limit) + 1;
    const pages = Math.max(1, Math.ceil(list.total / list.limit));
    return `<div class="list-meta">共 ${list.total} 条 · 第 ${page}/${pages} 页</div>${rows}
      ${pages > 1 ? `<div class="pager">
        <button class="btn small ghost" data-page="prev" ${list.offset <= 0 ? "disabled" : ""}>上一页</button>
        <button class="btn small ghost" data-page="next" ${!list.has_more ? "disabled" : ""}>下一页</button></div>` : ""}`;
  }

  async function viewClips() {
    const seq = routeSeq;
    const [tagData, list, st] = await Promise.all([
      api("/api/clips/tags"), api(`/api/clips?${clipQuery()}`), api("/api/ai/status"),
    ]);
    if (seq !== routeSeq) return;
    const tagChips = tagData.tags.map((t) => `
      <span class="chip ${clipFilter.tag === t.tag ? "on" : ""}" data-tag="${esc(t.tag)}">
        ${esc(t.tag)}<span class="chip-n">${t.n}</span></span>`).join("");
    const chChips = tagData.channels.map((c) => `
      <span class="chip ${clipFilter.channel === c ? "on" : ""}" data-channel="${esc(c)}">
        ${esc(c)}</span>`).join("");
    const knownTags = tagData.tags.map((t) => t.tag);
    const channels = [...new Set(["公众号", "视频", "播客", "书", "文章", "朋友", "新闻", "文件", "其他", ...tagData.channels])];

    main.innerHTML = head("集藏", `${list.total} 条匹配 · 采集 → 整理 → 研究加工 → 复用`)
      + panel(editingClip ? "编辑剪藏" : "收一条", "链接只保存一次并明确报告抓取状态；纯文本可直接保存；AI 加工由你勾选或手动触发。", `
        <div class="clip-form">
          <div class="cf-row">
            <input type="text" id="cl-url" placeholder="粘贴 http(s) 网页链接…">
            <input type="text" id="cl-title" placeholder="标题（留空则自动抓标题或由正文生成）">
          </div>
          <div class="cf-row">
            <input type="text" id="cl-tags" placeholder="标签，逗号分隔…">
            <select id="cl-channel"><option value="">渠道…</option>
              ${channels.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select>
            <button class="btn" id="cl-save">${editingClip ? "保存修改" : "收下"}</button>
            ${editingClip ? `<button class="btn ghost" id="cl-cancel">取消编辑</button>` : ""}
            ${st.ready && !editingClip ? `<button class="btn ghost" id="cl-ai">AI 归类</button>` : ""}
          </div>
          <div class="cf-row"><textarea id="cl-excerpt" style="min-height:72px"
            placeholder="粘贴文本、摘录或笔记；没有标题也可以直接保存…"></textarea></div>
          <div class="collection-tools">
            <label class="btn small ghost file-btn">导入文件<input type="file" id="cl-files" multiple
              accept=".txt,.md,.markdown,.csv,.tsv,.json,.log,.url,.html,.htm,.docx,.pdf,.xml"></label>
            <span class="faint">文本 / HTML / DOCX；PDF 解析需可选 pypdf，单文件 ≤6MB</span>
            ${st.ready && !editingClip ? `<label class="check-label"><input type="checkbox" id="cl-auto-ai"> 收存后进行 AI 研究加工</label>` : ""}
            <span class="grow"></span><button class="btn small ghost" id="cl-export">导出 JSON</button>
          </div>
          <div class="faint" id="cl-msg" role="status"></div>
        </div>
        <datalist id="tag-list">${knownTags.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>`)
      + `<div class="filters">
          <select id="cl-archived" class="compact-select" title="归档状态">
            <option value="active" ${clipFilter.archived === "active" ? "selected" : ""}>未归档</option>
            <option value="archived" ${clipFilter.archived === "archived" ? "selected" : ""}>已归档</option>
            <option value="all" ${clipFilter.archived === "all" ? "selected" : ""}>全部状态</option></select>
          <select id="cl-sort" class="compact-select" title="排序">
            ${[["newest","最新采集"],["updated","最近更新"],["oldest","最早采集"],["title","标题"]].map(([v,l]) =>
              `<option value="${v}" ${clipFilter.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
          <input type="search" id="cl-search" value="${esc(clipFilter.q)}" placeholder="搜标题、正文、摘要、标签…" style="flex:1;min-width:180px">
          <button class="btn small ghost" id="cl-clear-search">清空</button></div>`
      + `<div class="filters filter-scroll">${chChips ? `<span class="f-label">渠道</span>${chChips}` : ""}
          ${tagChips ? `<span class="f-label">标签</span>${tagChips}` : ""}</div>`
      + `<div class="batch-bar"><label class="check-label"><input type="checkbox" id="cl-select-all"> 本页全选</label>
          <span id="cl-selected">已选 ${selectedClips.size} 项</span><span class="grow"></span>
          <button class="btn small ghost" id="cl-batch-tag">加标签</button>
          <button class="btn small ghost" id="cl-batch-archive">${clipFilter.archived === "archived" ? "取消归档" : "归档"}</button>
          ${st.ready ? `<button class="btn small ghost" id="cl-compare">AI 对比（2–4条）</button>` : ""}
          <button class="btn small ghost danger" id="cl-batch-delete">删除</button></div>`
      + `<div class="panel"><div class="panel-body" id="clip-grid-host">${clipListMarkup(list)}</div></div>`;

    const resetForm = () => {
      editingClip = null;
      ["cl-url", "cl-title", "cl-tags", "cl-excerpt"].forEach((id) => { if ($("#"+id)) $("#"+id).value = ""; });
    };
    const refresh = async () => { await viewClips(); refreshSidebar(); };
    $("#cl-cancel")?.addEventListener("click", () => { editingClip = null; viewClips(); });

    $("#cl-save").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const msg = $("#cl-msg");
      msg.textContent = editingClip ? "保存中…" : "收存与解析中…";
      const payload = {
        url: $("#cl-url").value.trim(), title: $("#cl-title").value.trim(),
        tags: $("#cl-tags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        channel: $("#cl-channel").value, excerpt: $("#cl-excerpt").value.trim(),
      };
      let saved;
      try {
        saved = editingClip ? await post(`/api/clips/${editingClip}`, payload) : await post("/api/clips", payload);
      } catch (e) { msg.innerHTML = `<span class="err-text">${esc(e.message)}</span>`; return; }
      const cid = saved.clip.id;
      if (saved.duplicate) toast(`链接或内容已存在，已定位到 #${cid}，未重复录入`, "ok");
      else if (saved.import_status === "saved_fetch_failed") toast(`已保存 #${cid}；网页解析失败，可稍后重试`, "warn");
      else toast(editingClip ? "修改已保存" : `已收下 #${cid}`);
      const autoAI = $("#cl-auto-ai")?.checked;
      resetForm();
      if (autoAI && !saved.duplicate) {
        msg.textContent = "AI 研究加工中…";
        try {
          const r = await post("/api/ai/analyze", { id: cid });
          toast(r.result.chart.suitable ? "研究加工与图表已保存" : `研究加工已保存；${r.chart_message}`, "ok");
        } catch (e) { toast(`剪藏已保存，但 AI 加工失败：${e.message}`, "err"); }
      }
      clipFilter = { ...clipFilter, tag: "", channel: "", archived: "active", q: "", offset: 0 };
      await refresh();
    }));

    $("#cl-ai")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const msg = $("#cl-msg"); msg.textContent = "AI 归类中…";
      try {
        const r = await post("/api/ai/classify", {
          title: $("#cl-title").value || $("#cl-url").value, excerpt: $("#cl-excerpt").value,
        });
        const have = $("#cl-tags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
        $("#cl-tags").value = [...new Set([...have, ...(r.tags || [])])].join(", ");
        msg.textContent = `${r.kind === "fact" ? "更像可验证事实" : "更像观点/资讯"}：${r.reason}`;
      } catch (e) { msg.innerHTML = `<span class="err-text">${esc(e.message)}</span>`; }
    }));

    $("#cl-files").addEventListener("change", async (ev) => {
      const input = ev.currentTarget, files = Array.from(input.files || []);
      if (!files.length) return;
      input.disabled = true;
      const msg = $("#cl-msg");
      let ok = 0, duplicates = 0;
      const readFile = (file) => new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        msg.textContent = `导入 ${i+1}/${files.length}：${file.name}`;
        try {
          if (file.size > 6 * 1024 * 1024) throw new Error("超过 6MB 上限");
          const encoded = await readFile(file);
          const r = await post("/api/clips/file", {
            name: file.name, mime_type: file.type, content_base64: encoded,
            tags: $("#cl-tags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          });
          if (r.duplicate) duplicates++; else ok++;
          if (r.parse_message) toast(`${file.name}：${r.parse_message}`, "warn");
        } catch (e) { toast(`${file.name} 导入失败：${e.message}`, "err"); }
      }
      msg.textContent = `导入完成：新增 ${ok}，重复 ${duplicates}，失败 ${files.length-ok-duplicates}`;
      input.disabled = false; input.value = "";
      clipFilter = { ...clipFilter, tag: "", channel: "", archived: "active", q: "", offset: 0 };
      await refresh();
    });

    $("#cl-export").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const data = await api("/api/clips/export");
      downloadJSON(data, `guanlan-clips-${today()}.json`);
      toast(`已导出 ${data.clips.length} 条剪藏与 ${data.ai_outputs.length} 条研究记录（不含文件原件）`);
    }));

    const resetPage = () => { clipFilter.offset = 0; selectedClips.clear(); };
    $$("[data-tag]").forEach((el) => el.addEventListener("click", () => {
      clipFilter.tag = clipFilter.tag === el.dataset.tag ? "" : el.dataset.tag; resetPage(); viewClips();
    }));
    $$("[data-channel]").forEach((el) => el.addEventListener("click", () => {
      clipFilter.channel = clipFilter.channel === el.dataset.channel ? "" : el.dataset.channel; resetPage(); viewClips();
    }));
    $("#cl-archived").addEventListener("change", (e) => { clipFilter.archived = e.target.value; resetPage(); viewClips(); });
    $("#cl-sort").addEventListener("change", (e) => { clipFilter.sort = e.target.value; clipFilter.offset = 0; viewClips(); });

    let timer = null;
    const runSearch = async () => {
      const request = ++clipSearchSeq;
      clipFilter.q = $("#cl-search").value.trim(); clipFilter.offset = 0;
      try {
        const result = await api(`/api/clips?${clipQuery()}`);
        if (request !== clipSearchSeq || seq !== routeSeq) return;
        $("#clip-grid-host").innerHTML = clipListMarkup(result);
        bindClipCards($("#clip-grid-host")); bindPager();
      } catch (e) { if (request === clipSearchSeq) toast(e.message, "err"); }
    };
    $("#cl-search").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(runSearch, 320); });
    $("#cl-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(timer); runSearch(); } });
    $("#cl-clear-search").addEventListener("click", () => { $("#cl-search").value = ""; runSearch(); $("#cl-search").focus(); });

    function bindPager() {
      $$("[data-page]", $("#clip-grid-host")).forEach((button) => button.addEventListener("click", () => {
        clipFilter.offset = Math.max(0, clipFilter.offset + (button.dataset.page === "next" ? CLIP_PAGE_SIZE : -CLIP_PAGE_SIZE));
        viewClips();
      }));
    }
    bindPager();

    const batch = async (action) => {
      const ids = [...selectedClips];
      if (!ids.length) return toast("请先选择剪藏", "warn");
      const body = { ids, action };
      if (action === "add_tags") {
        const value = prompt("为所选剪藏添加标签（逗号分隔）");
        if (!value) return;
        body.tags = value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      }
      if (action === "delete" && !confirm(`确定永久删除 ${ids.length} 条剪藏及其研究记录？归档通常更合适。`)) return;
      try {
        await post("/api/clips/batch", body); selectedClips.clear(); toast("批量操作已完成"); await refresh();
      } catch (e) { toast(e.message, "err"); }
    };
    $("#cl-batch-tag").addEventListener("click", () => batch("add_tags"));
    $("#cl-batch-archive").addEventListener("click", () => batch(clipFilter.archived === "archived" ? "unarchive" : "archive"));
    $("#cl-batch-delete").addEventListener("click", () => batch("delete"));
    $("#cl-select-all").addEventListener("change", (e) => {
      $$(".clip-select", $("#clip-grid-host")).forEach((box) => {
        box.checked = e.target.checked;
        const id = Number(box.value); if (box.checked) selectedClips.add(id); else selectedClips.delete(id);
      });
      updateSelectionUI();
    });
    $("#cl-compare")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const ids = [...selectedClips];
      if (ids.length < 2 || ids.length > 4) return toast("对比分析请选择 2–4 条剪藏", "warn");
      try {
        await post("/api/ai/analyze", { ids });
        aiTab = "records"; location.hash = "#/ai"; toast("对比结果已保存到研究记录");
      } catch (e) { toast(e.message, "err"); }
    }));
    bindClipCards(main);
  }

  function updateSelectionUI() {
    if ($("#cl-selected")) $("#cl-selected").textContent = `已选 ${selectedClips.size} 项`;
  }

  function clipCard(c) {
    const state = c.archived ? `<span class="badge">已归档</span>` : "";
    const fetchState = c.fetch_status === "failed"
      ? `<span class="badge contradicted" title="${esc(c.fetch_error || "")}">解析失败</span>`
      : c.fetch_status === "unparsed" ? `<span class="badge draft" title="${esc(c.fetch_error || "")}">原件已存·未解析</span>` : "";
    return `<article class="clip-card ${c.archived ? "is-archived" : ""}" data-id="${c.id}">
      <div class="cc-top"><label class="check-label"><input type="checkbox" class="clip-select" value="${c.id}"
        ${selectedClips.has(c.id) ? "checked" : ""} aria-label="选择 ${esc(c.title)}"></label>
        <a class="cc-title-link" href="#/clips/${c.id}">${esc(c.title)}</a></div>
      <div class="cc-domain">${esc(c.domain || c.file_name || c.source_type || "文本")} ${state}${fetchState}</div>
      ${Array.isArray(c.tags) && c.tags.length ? `<div class="cc-tags">${c.tags.map((t) => `<span class="chip mini">${esc(t)}</span>`).join("")}</div>` : ""}
      ${c.summary ? `<div class="cc-excerpt ai-summary">${esc(c.summary)}</div>`
        : c.excerpt ? `<div class="cc-excerpt">${esc(c.excerpt)}</div>` : ""}
      <div class="cc-foot"><span>${esc((c.updated_at || c.created_at || "").slice(5, 16))}</span>
        ${c.channel ? `<span class="badge">${esc(c.channel)}</span>` : ""}<span class="grow"></span>
        <a class="cc-act" href="#/clips/${c.id}">详情</a>
        ${c.url && c.fetch_status === "failed" ? `<span class="cc-act" data-fetch="${c.id}">重试</span>` : ""}
        <span class="cc-act" data-analyze="${c.id}">AI 加工</span>
        <span class="cc-act" data-edit="${c.id}">编辑</span>
        <span class="cc-act" data-archive="${c.id}" data-value="${c.archived ? 0 : 1}">${c.archived ? "恢复" : "归档"}</span>
        <span class="cc-act danger" data-del="${c.id}">删</span></div></article>`;
  }

  function citationHTML(text) {
    return esc(text || "").replace(/\[(C-\d{4}-\d{3,4})\]/g,
      `<a class="answer-cite" href="#/canon/$1">[$1]</a>`).replace(/\n/g, "<br>");
  }

  function researchOutputCard(o) {
    const c = o.content || {};
    const list = (title, values, cls = "") => Array.isArray(values) && values.length
      ? `<section class="research-section ${cls}"><h3>${title}</h3><ul>${values.map((v) => `<li>${esc(v)}</li>`).join("")}</ul></section>` : "";
    const comparison = c.comparison || {};
    return `<article class="research-card" data-output="${o.id}">
      <div class="research-head"><div><span class="badge active">${esc(o.kind)}</span>
        <strong>${esc(o.title)}</strong><div class="faint">#${o.id} · ${esc(o.model || "")} · ${esc(o.updated_at || o.created_at)}</div></div>
        <button class="btn small ghost" data-output-edit="${o.id}">编辑 JSON</button></div>
      ${c.summary ? `<div class="research-summary">${esc(c.summary)}</div>` : ""}
      ${c.answer ? `<div class="ai-box">${citationHTML(c.answer)}</div>` : ""}
      ${c.question ? `<div class="faint">问题：${esc(c.question)}</div>` : ""}
      ${list("关键要点", c.key_points)}
      ${list("共识", comparison.agreements)}${list("差异", comparison.differences, "differences")}
      ${list("尚无法裁决", comparison.uncertainties)}
      ${c.conclusion ? `<section class="research-section conclusion"><h3>研究 / 决策辅助结论</h3><p>${esc(c.conclusion)}</p></section>` : ""}
      ${list("后续待办", c.todos, "todo-list")}
      ${Array.isArray(c.tags) && c.tags.length ? `<div class="cc-tags">${c.tags.map((t) => `<span class="chip mini">${esc(t)}</span>`).join("")}</div>` : ""}
      ${c.chart ? aiChart(c.chart) : ""}
      ${c.raw ? `<details class="raw-output"><summary>原始输出（${esc(c.parse_error || "未解析")}）</summary><pre>${esc(c.raw)}</pre></details>` : ""}
      <div class="output-edit hide"><textarea class="code-editor" id="oe-${o.id}">${esc(JSON.stringify(c, null, 2))}</textarea>
        <button class="btn small" data-output-save="${o.id}">保存修改</button></div>
      <div class="research-actions"><button class="btn small ghost" data-output-copy="${o.id}">复制结果</button>
        <button class="btn small ghost" data-output-decision="${o.id}">用于新决策</button>
        <button class="btn small ghost danger" data-output-delete="${o.id}">删除记录</button></div></article>`;
  }

  function outputPlainText(o) {
    const c = o.content || {};
    const points = Array.isArray(c.key_points) ? c.key_points : [];
    const todos = Array.isArray(c.todos) ? c.todos : [];
    return [c.summary, ...points, c.conclusion, c.answer, ...todos].filter(Boolean).join("\n");
  }

  function bindOutputCards(root, outputs, refresh) {
    const byId = new Map(outputs.map((o) => [Number(o.id), o]));
    root.querySelectorAll("[data-output-edit]").forEach((button) => button.addEventListener("click", () => {
      const editor = button.closest(".research-card").querySelector(".output-edit");
      editor.classList.toggle("hide");
      button.textContent = editor.classList.contains("hide") ? "编辑 JSON" : "收起编辑";
    }));
    root.querySelectorAll("[data-output-save]").forEach((button) => button.addEventListener("click", () => withBusy(button, async () => {
      const id = Number(button.dataset.outputSave);
      try {
        const content = JSON.parse($(`#oe-${id}`).value);
        if (!content || Array.isArray(content) || typeof content !== "object") throw new Error("顶层必须是 JSON 对象");
        await post(`/api/ai/outputs/${id}`, { content }); toast("研究记录已更新"); await refresh();
      } catch (e) { toast(`保存失败：${e.message}`, "err"); }
    })));
    root.querySelectorAll("[data-output-copy]").forEach((button) => button.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(outputPlainText(byId.get(Number(button.dataset.outputCopy)))); toast("已复制"); }
      catch (e) { toast("浏览器未允许剪贴板访问，请使用编辑区复制", "warn"); }
    }));
    root.querySelectorAll("[data-output-decision]").forEach((button) => button.addEventListener("click", () => {
      const o = byId.get(Number(button.dataset.outputDecision));
      sessionStorage.setItem("gl-decision-prefill", JSON.stringify({
        decision: "", chosen_because: `研究记录 #${o.id} · ${o.title}\n${outputPlainText(o)}`,
      }));
      location.hash = "#/decisions";
    }));
    root.querySelectorAll("[data-output-delete]").forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("删除这条研究记录？原始剪藏不会删除。")) return;
      await post(`/api/ai/outputs/${button.dataset.outputDelete}/delete`, {}); toast("研究记录已删除"); await refresh();
    }));
  }

  async function viewClipDetail(cid) {
    const seq = routeSeq;
    const [d, st] = await Promise.all([api(`/api/clips/${encodeURIComponent(cid)}`), api("/api/ai/status")]);
    if (seq !== routeSeq) return;
    const c = d.clip, outputs = d.outputs || [];
    main.innerHTML = back("#/clips", "返回集藏") + head(c.title, `#${c.id} · ${esc(c.source_type)} · ${c.archived ? "已归档" : "使用中"}`)
      + `<div class="detail-grid">
          <div class="detail-cell"><div class="k">来源</div><div class="v">${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noreferrer noopener">${esc(c.domain || "打开原网页")}</a>` : esc(c.file_name || "直接录入")}</div></div>
          <div class="detail-cell"><div class="k">解析状态</div><div class="v">${esc(c.fetch_status)} · ${c.content_chars || 0} 字</div></div>
          <div class="detail-cell"><div class="k">渠道</div><div class="v">${esc(c.channel || "—")}</div></div>
          <div class="detail-cell"><div class="k">采集时间</div><div class="v">${esc(c.created_at)}</div></div></div>`
      + (c.fetch_error ? `<div class="notice small"><div class="notice-body"><strong>解析反馈：</strong>${esc(c.fetch_error)}</div></div>` : "")
      + `<div class="filters">
          ${c.file_name ? `<a class="btn small ghost" href="/api/clips/${c.id}/file">下载文件原件</a>` : ""}
          ${c.url ? `<button class="btn small ghost" id="cd-fetch">重新抓取正文</button>` : ""}
          ${st.ready ? `<button class="btn" id="cd-analyze">AI 研究加工</button>` : ""}
          <button class="btn small ghost" id="cd-archive">${c.archived ? "取消归档" : "归档"}</button>
          <button class="btn small ghost" id="cd-toentry">转为正典录入</button></div>`
      + panel("内容与 AI 摘要", "正文、标签和 AI 摘要都可编辑；保存后用于下一次检索与加工。", `
          <div class="form-grid">
            <div class="form-row full"><label>标题</label><input type="text" id="cd-title" value="${esc(c.title)}"></div>
            <div class="form-row full"><label>标签</label><input type="text" id="cd-tags" value="${esc((c.tags || []).join(", "))}"></div>
            <div class="form-row full"><label>摘录 / 笔记</label><textarea id="cd-excerpt">${esc(c.excerpt || "")}</textarea></div>
            <div class="form-row full"><label>解析正文（可补充或修正）</label><textarea id="cd-content" style="min-height:180px">${esc(c.content_text || "")}</textarea></div>
            <div class="form-row full"><label>AI 摘要（人工可修订）</label><textarea id="cd-summary">${esc(c.summary || "")}</textarea></div>
          </div><div class="form-actions"><button class="btn" id="cd-save">保存内容与摘要</button><span id="cd-msg" class="faint"></span></div>`)
      + `<div class="section-head"><h2>研究记录</h2><span>${outputs.length} 条 · 分析、图表和结论持久保存</span></div>`
      + (outputs.map(researchOutputCard).join("") || empty("尚无研究记录", st.ready ? "点击上方「AI 研究加工」生成结构化要点、待办、结论及适用时的图表" : "配置 AI 后可从这里启动研究加工"));
    const refresh = () => viewClipDetail(cid);
    bindOutputCards(main, outputs, refresh);
    $("#cd-save").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        await post(`/api/clips/${c.id}`, {
          title: $("#cd-title").value, tags: $("#cd-tags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          excerpt: $("#cd-excerpt").value, content_text: $("#cd-content").value, summary: $("#cd-summary").value,
        });
        toast("内容、标签和摘要已保存"); await refresh();
      } catch (e) { $("#cd-msg").textContent = e.message; }
    }));
    $("#cd-analyze")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try {
        const r = await post("/api/ai/analyze", { id: c.id });
        toast(r.result.chart.suitable ? "分析、图表和研究记录已保存" : `分析已保存；${r.chart_message}`); await refresh();
      } catch (e) { toast(e.message, "err"); }
    }));
    $("#cd-fetch")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      try { await post(`/api/clips/${c.id}/fetch`, {}); toast("正文已更新"); await refresh(); }
      catch (e) { toast(e.message, "err"); }
    }));
    $("#cd-archive").addEventListener("click", async () => {
      await post(`/api/clips/${c.id}`, { archived: c.archived ? 0 : 1 }); toast(c.archived ? "已恢复" : "已归档"); await refresh();
    });
    $("#cd-toentry").addEventListener("click", () => {
      sessionStorage.setItem("gl-prefill", JSON.stringify({ statement: c.title, source_url: c.url || "" }));
      location.hash = "#/entry";
    });
  }

  function bindClipCards(root) {
    root.querySelectorAll(".clip-select").forEach((box) => box.addEventListener("change", () => {
      const id = Number(box.value); if (box.checked) selectedClips.add(id); else selectedClips.delete(id); updateSelectionUI();
    }));
    root.querySelectorAll("[data-del]").forEach((el) => el.addEventListener("click", async () => {
      if (!confirm("永久删除这条剪藏及其研究记录？若只是暂时不用，建议归档。")) return;
      await post("/api/clips/delete", { id: Number(el.dataset.del) }); selectedClips.delete(Number(el.dataset.del));
      toast("剪藏已删除"); await viewClips(); refreshSidebar();
    }));
    root.querySelectorAll("[data-archive]").forEach((el) => el.addEventListener("click", async () => {
      await post(`/api/clips/${el.dataset.archive}`, { archived: Number(el.dataset.value) });
      toast(Number(el.dataset.value) ? "已归档" : "已恢复"); await viewClips();
    }));
    root.querySelectorAll("[data-fetch]").forEach((el) => el.addEventListener("click", async () => {
      el.textContent = "重试中…";
      try { await post(`/api/clips/${el.dataset.fetch}/fetch`, {}); toast("正文抓取成功"); }
      catch (e) { toast(e.message, "err"); }
      await viewClips();
    }));
    root.querySelectorAll("[data-analyze]").forEach((el) => el.addEventListener("click", async () => {
      el.textContent = "加工中…";
      try {
        const r = await post("/api/ai/analyze", { id: Number(el.dataset.analyze) });
        toast(r.result.chart.suitable ? "研究加工与图表已保存" : `研究加工已保存；${r.chart_message}`);
        location.hash = `#/clips/${el.dataset.analyze}`;
      } catch (e) { toast(e.message, "err"); el.textContent = "AI 加工"; }
    }));
    root.querySelectorAll("[data-edit]").forEach((el) => el.addEventListener("click", async () => {
      const id = Number(el.dataset.edit);
      try {
        const d = await api(`/api/clips/${id}`); editingClip = id; await viewClips();
        const c = d.clip;
        $("#cl-url").value = c.url || ""; $("#cl-title").value = c.title || "";
        $("#cl-tags").value = (c.tags || []).join(", "); $("#cl-channel").value = c.channel || "";
        $("#cl-excerpt").value = c.excerpt || ""; window.scrollTo(0, 0); $("#cl-title").focus();
      } catch (e) { toast(e.message, "err"); }
    }));
  }

  /* ── 视图：AI ──────────────────────────────────────── */

  let aiTab = "ingest";

  async function viewAI() {
    const seq = routeSeq;
    const st = await api("/api/ai/status");
    if (seq !== routeSeq) return;
    main.innerHTML = head("AI 研究台", st.ready ? `${esc(st.channel)} · ${esc(st.model)} · ${esc(st.endpoint)}` : "未配置或配置不完整")
      + `<div class="notice small"><div class="notice-body">
          ${st.ready ? "AI 只在你点击或明确勾选时处理内容；分析、问答与反方结果会保存为可编辑研究记录。"
            : `<strong>AI 当前不可用。</strong>${esc(st.config_error || "请完成配置")}。设置环境变量后重启服务：<br>
              <code>set GL_AI_BASE_URL=https://api.deepseek.com</code><br>
              <code>set GL_AI_MODEL=deepseek-chat</code><br><code>set GL_AI_KEY=sk-...</code><br>
              也可用 <code>GL_AI_DISABLE=1</code> 明确停用默认云通道。`}
          ${st.ready ? `<div style="margin-top:8px"><button class="btn small ghost" id="ai-test">测试连接</button>
            <span class="faint" id="ai-test-msg"></span></div>` : ""}</div></div>`
      + `<div class="tabs">
          <div class="tab ${aiTab === "ingest" ? "on" : ""}" data-tab="ingest">A · 摄取</div>
          <div class="tab ${aiTab === "ask" ? "on" : ""}" data-tab="ask">B · 综合（引用强制）</div>
          <div class="tab ${aiTab === "devil" ? "on" : ""}" data-tab="devil">C · 反方论证</div>
          <div class="tab ${aiTab === "records" ? "on" : ""}" data-tab="records">研究记录</div>
        </div><div id="ai-body"></div>`;

    $$(".tab").forEach((t) => t.addEventListener("click", () => { aiTab = t.dataset.tab; viewAI(); }));
    $("#ai-test")?.addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
      const msg = $("#ai-test-msg"); msg.textContent = " 测试中…";
      try { const r = await post("/api/ai/test", {}); msg.textContent = ` 连接成功 · ${r.model}`; }
      catch (e) { msg.textContent = ` ${e.message}`; }
    }));

    if (aiTab === "ingest") await aiIngest();
    if (aiTab === "ask") await aiAsk();
    if (aiTab === "devil") await aiDevil();
    if (aiTab === "records") await aiRecords();
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
          <div class="panel-title">回答</div><div class="panel-sub">${r.output_id ? `已保存研究记录 #${r.output_id} · ` : ""}无出处或无效引用句已过滤</div></div>
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
          <div class="panel-title">反面论证</div><div class="panel-sub">相关证据检索${r.output_id ? ` · 已保存研究记录 #${r.output_id}` : ""}</div></div>
          <div class="panel-body"><div class="ai-box">${esc(r.answer)}</div></div></div>`;
      } catch (e) {
        msg.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
      }
    });
  }

  async function aiRecords() {
    const d = await api("/api/ai/outputs");
    $("#ai-body").innerHTML = panel("可编辑研究记录", "分析、对比、问答与反方论证统一保存；可修订、复制或带入新决策。",
      d.outputs.map(researchOutputCard).join("") || empty("还没有研究记录", "在集藏中选择一条做 AI 加工，或选择 2–4 条做对比"));
    bindOutputCards($("#ai-body"), d.outputs, aiRecords);
  }

  /* ── 视图：库（口径 / 基准率 / 来源）──────────────── */

  let libTab = "calibers";

  async function viewLibrary() {
    const seq = routeSeq;
    const [cal, br, src] = await Promise.all([
      api("/api/calibers"), api("/api/baserates"), api("/api/sources")]);
    if (seq !== routeSeq) return;

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
      $("#c-clips").textContent = o.stats.clips;
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
    clips: (a) => a ? viewClipDetail(a) : viewClips(),
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
