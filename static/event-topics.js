/* 观澜 · 桌面端事件专题：独立视图、独立 ev- 样式，复用 GL 的接口与对话框。 */
(() => {
  "use strict";
  const G = window.GL;
  if (!G) return;
  const { $, $$, esc, api, post, toast, glConfirm, withBusy, mdBody, currentPath } = G;
  const main = $("#main");
  const labels = { active: "跟踪中", paused: "已暂停", archived: "已归档" };
  let listState = { status: "active", q: "" };
  let searchTimer = 0;
  let requestSeq = 0;

  const pathOf = () => String(currentPath() || location.hash.replace(/^#/, "") || "/");
  const isEvents = (path = pathOf()) => path === "/events" || path.startsWith("/events/");
  const safe = (value) => esc(value == null ? "" : value);
  const fmt = (value) => String(value || "").replace("T", " ").slice(0, 16);
  const nowLocal = () => {
    const d = new Date();
    const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  };
  const statusName = (status) => labels[status] || status;
  const draftOpen = () => !!$("#ev-modal,.ev-edit-area:focus,#ev-update-body:focus");
  const eventOrder = () => {
    try { return sessionStorage.getItem("ev_order") === "asc" ? "asc" : "desc"; }
    catch (_) { return "desc"; }
  };
  const notify = (message, type = "ok") => toast(message, type);
  const save = (button, action) => withBusy(button, action).catch((err) => notify(err.message, "err"));

  function onRoute(path) {
    const active = isEvents(path);
    document.body.classList.toggle("ev-active", active);
    const vault = $("#nav-vault"), reading = $("#nav-reading"), bible = $("#nav-bible");
    const events = $("#nav-events");
    if (active) {
      if (vault) vault.hidden = true;
      if (reading) reading.hidden = true;
      if (bible) bible.hidden = true;
      if (events) events.hidden = false;
      // 只在进入事件时接管入口选中态；离开时交给阅读/圣经/收存各自的 onRoute，
      // 否则会把收存入口的高亮一起清掉。
      $$("#sect-switch .sect-btn").forEach((b) =>
        b.classList.toggle("on", b.dataset.sect === "events"));
    } else if (events) events.hidden = true;
  }

  function modal(title, body, onReady) {
    closeModal();
    const prev = document.activeElement;
    const back = document.createElement("div");
    back.className = "ev-modal-backdrop";
    back.id = "ev-modal";
    back.innerHTML = `<section class="ev-modal" role="dialog" aria-modal="true" aria-labelledby="ev-modal-title">
      <div class="ev-modal-head"><h2 id="ev-modal-title">${safe(title)}</h2>
        <button class="ev-icon-btn" type="button" data-ev-close aria-label="关闭">×</button></div>
      <div class="ev-modal-body">${body}</div></section>`;
    document.body.appendChild(back);
    back.addEventListener("mousedown", (e) => { if (e.target === back) closeModal(); });
    $("[data-ev-close]", back).addEventListener("click", closeModal);
    back._restoreFocus = prev;
    onReady?.(back);
    $("input,textarea", back)?.focus();
  }

  function closeModal() {
    const old = $("#ev-modal");
    if (!old) return;
    const prev = old._restoreFocus;
    old.remove();
    if (prev?.isConnected) prev.focus();
  }

  document.addEventListener("keydown", (e) => {
    const dialog = $("#ev-modal");
    if (!dialog) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeModal();
    } else if (e.key === "Tab") {
      const items = $$("button,input,textarea,select,a[href]", dialog).filter((el) => !el.disabled);
      if (!items.length) return;
      const index = items.indexOf(document.activeElement);
      e.preventDefault();
      items[e.shiftKey ? (index <= 0 ? items.length - 1 : index - 1)
        : (index + 1) % items.length].focus();
    }
  }, true);

  function topicForm(topic = {}) {
    return `<form class="ev-form" id="ev-topic-form">
      <label class="ev-label" for="ev-topic-title">事件名称 <span>必填</span></label>
      <input class="ev-input" id="ev-topic-title" maxlength="120" required
        value="${safe(topic.title)}" placeholder="例如：某项政策后续调整">
      <label class="ev-label" for="ev-topic-purpose">跟踪目的</label>
      <textarea class="ev-input" id="ev-topic-purpose" rows="2" maxlength="2000"
        placeholder="我想持续看清什么？">${safe(topic.purpose)}</textarea>
      <label class="ev-label" for="ev-topic-tags">标签 <span>逗号分隔，最多 12 个</span></label>
      <input class="ev-input" id="ev-topic-tags" value="${safe((topic.tags || []).join("，"))}"
        placeholder="政策，经济，教育">
      <button class="btn small" type="submit">${topic.id ? "保存事件" : "创建事件"}</button>
    </form>`;
  }

  function openTopicEditor(topic = {}) {
    modal(topic.id ? "编辑事件" : "新建事件", topicForm(topic), (root) => {
      $("#ev-topic-form", root).addEventListener("submit", (e) => {
        e.preventDefault();
        const button = $("button[type=submit]", e.currentTarget);
        const payload = {
          title: $("#ev-topic-title", root).value,
          purpose: $("#ev-topic-purpose", root).value,
          tags: $("#ev-topic-tags", root).value,
        };
        save(button, async () => {
          const result = topic.id
            ? await post(`/api/event-topics/${topic.id}`, payload)
            : await post("/api/event-topics", payload);
          closeModal();
          location.hash = `#/events/${id}`;
          if (topic.id && currentPath() === `/events/${id}`) await renderDetail(id);
          notify(topic.id ? "事件已保存" : "事件已创建");
        });
      });
    });
  }

  function openInsightsEditor(topic) {
    modal("当前认识与待解问题", `<form class="ev-form" id="ev-insights-form">
      <label class="ev-label" for="ev-understanding">当前认识</label>
      <textarea class="ev-input" id="ev-understanding" rows="7" maxlength="12000"
        placeholder="基于目前材料，我如何判断这件事？">${safe(topic.current_understanding)}</textarea>
      <label class="ev-label" for="ev-questions">待解问题</label>
      <textarea class="ev-input" id="ev-questions" rows="5" maxlength="12000"
        placeholder="哪些事实还缺少证据？下一步要核对什么？">${safe(topic.open_questions)}</textarea>
      <button class="btn small" type="submit">保存</button></form>`, (root) => {
      $("#ev-insights-form", root).addEventListener("submit", (e) => {
        e.preventDefault();
        save($("button[type=submit]", root), async () => {
          await post(`/api/event-topics/${topic.id}`, {
            current_understanding: $("#ev-understanding", root).value,
            open_questions: $("#ev-questions", root).value,
          });
          closeModal();
          await renderDetail(topic.id);
          notify("已保存");
        });
      });
    });
  }

  async function renderList() {
    const seq = ++requestSeq;
    const focused = document.activeElement;
    const focusId = focused?.id || "";
    const focusStatus = focused?.dataset?.status || "";
    const selection = focusId === "ev-search" ? [focused.selectionStart, focused.selectionEnd] : null;
    const params = new URLSearchParams({ status: listState.status, q: listState.q });
    const data = await api(`/api/event-topics?${params}`);
    if (seq !== requestSeq || currentPath() !== "/events") return;
    const topics = data.topics || [];
    main.innerHTML = `<div class="ev-page ev-list-page">
      <div class="ev-topline"><div><div class="ev-kicker">持续跟踪 · 资料归集 · 时间线</div>
        <h1>事件</h1><p>把一条条资料，整理成一件事的来龙去脉。</p></div>
        <button class="btn small" id="ev-new" type="button">＋ 新建事件</button></div>
      <div class="ev-toolbar" role="group" aria-label="事件筛选">
        ${["active", "paused", "archived", "all"].map((key) => `<button type="button"
          class="ev-filter ${listState.status === key ? "on" : ""}" data-status="${key}"
          aria-pressed="${listState.status === key}">${key === "all" ? "全部" : labels[key]}
          <span>${key === "all" ? data.total : (data.counts?.[key] || 0)}</span></button>`).join("")}
        <label class="ev-search"><span class="ev-sr-only">搜索事件</span><input id="ev-search" type="search"
          value="${safe(listState.q)}" placeholder="搜索名称、目的或标签"></label></div>
      <div id="ev-results">${topics.length ? `<div class="ev-card-grid">${topics.map(topicCard).join("")}</div>`
        : `<div class="ev-empty"><h2>${listState.q ? "没有匹配的事件" : (listState.status === "active" ? "这里还没有事件" : "当前状态下没有事件")}</h2>
          <p>${listState.q ? "换个关键词，或清除搜索与筛选。" : (listState.status === "active" ? "从一件需要持续关注的事开始。" : "可以切换状态查看，或清除当前筛选。")}</p>
          ${listState.q || listState.status !== "active" ? `<button class="btn small ghost" id="ev-clear">清除筛选</button>`
            : `<button class="btn small" id="ev-empty-new">新建第一个事件</button>`}</div>`}</div></div>`;
    $("#ev-new").addEventListener("click", () => openTopicEditor());
    $("#ev-empty-new")?.addEventListener("click", () => openTopicEditor());
    $("#ev-clear")?.addEventListener("click", () => {
      listState = { status: "active", q: "" };
      renderList().catch((e) => notify(e.message, "err"));
    });
    $$("[data-status]", main).forEach((button) => button.addEventListener("click", () => {
      listState.status = button.dataset.status;
      renderList().catch((e) => notify(e.message, "err"));
    }));
    $("#ev-search").addEventListener("input", (e) => {
      listState.q = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => renderList().catch((err) => notify(err.message, "err")), 250);
    });
    const count = $("#c-events");
    if (count) count.textContent = String(data.counts?.active || 0);
    const navCount = $("#c-event-topics");
    if (navCount) navCount.textContent = String(data.total || 0);
    if (focusId) {
      const target = document.getElementById(focusId);
      target?.focus();
      if (selection) target?.setSelectionRange(...selection);
    } else if (focusStatus) $(`[data-status="${focusStatus}"]`, main)?.focus();
  }

  function topicCard(t) {
    return `<a class="ev-card" href="#/events/${t.id}">
      <div class="ev-card-top"><span class="ev-status ${safe(t.status)}">${safe(statusName(t.status))}</span>
        <span class="ev-date">${safe(fmt(t.updated_at))}</span></div>
      <h2>${safe(t.title)}</h2>
      ${t.purpose ? `<p class="ev-purpose">${safe(t.purpose)}</p>` : ""}
      <p class="ev-latest">${safe(t.latest_update || "还没有进展，打开后写下第一条。")}</p>
      <div class="ev-card-foot"><span>${t.update_count} 条进展 · ${t.link_count} 份资料</span>
        <span aria-hidden="true">→</span></div></a>`;
  }

  async function renderDetail(id) {
    const seq = ++requestSeq;
    const data = await api(`/api/event-topics/${id}`);
    if (seq !== requestSeq || currentPath() !== `/events/${id}`) return;
    const t = data.topic, updates = [...(data.updates || [])], links = data.links || [];
    main.innerHTML = `<div class="ev-page ev-detail-page">
      <a class="back" href="#/events">← 全部事件</a>
      <div class="ev-detail-head"><div><div class="ev-kicker"><span class="ev-status ${safe(t.status)}">${safe(statusName(t.status))}</span>
        <span>最近更新 ${safe(fmt(t.updated_at))}</span></div><h1>${safe(t.title)}</h1>
        ${t.purpose ? `<p>${safe(t.purpose)}</p>` : ""}
        ${(t.tags || []).length ? `<div class="ev-tags">${t.tags.map((tag) => `<span>${safe(tag)}</span>`).join("")}</div>` : ""}</div>
        <div class="ev-actions"><button class="btn small ghost" id="ev-edit" type="button">编辑事件</button>
          <select class="compact-select" id="ev-state" aria-label="事件状态">
            ${Object.entries(labels).map(([key, label]) => `<option value="${key}" ${t.status === key ? "selected" : ""}>${label}</option>`).join("")}</select></div></div>
      <div class="ev-layout"><div class="ev-main-column">
        <section class="ev-panel"><div class="ev-panel-head"><h2>进展时间线</h2>
          <button class="btn small ghost" id="ev-order" type="button">${eventOrder() === "asc" ? "正序复盘" : "最新在前"}</button></div>
          <form id="ev-update-form" class="ev-update-form"><div class="ev-update-fields">
            <label><span>实际发生时间</span><input class="ev-input" id="ev-happened" type="datetime-local" required value="${nowLocal()}"></label>
            <span class="ev-time-note">不同于收录时间，后补旧闻也会按实际时间排列。</span></div>
            <textarea class="ev-input" id="ev-update-body" rows="3" maxlength="12000" required
              placeholder="发生了什么？有什么新证据或变化？"></textarea>
            <button class="btn small" type="submit">追加进展</button></form>
          <div class="ev-timeline">${updates.length ? updates.map(updateRow).join("")
            : `<div class="ev-inline-empty">还没有进展。写下第一条，时间线就从这里开始。</div>`}</div></section>
        <section class="ev-panel"><div class="ev-panel-head"><h2>关联资料</h2><span>${links.length} 份</span></div>
          <div class="ev-link-tools"><input class="ev-input" id="ev-clip-search" type="search"
            placeholder="搜索已有收存，选择后关联"><div id="ev-clip-hits"></div></div>
          <div class="ev-link-list">${links.length ? links.map(linkRow).join("")
            : `<div class="ev-inline-empty">只建立关联，不复制资料；原收存卡片仍可独立使用。</div>`}</div></section>
      </div><aside class="ev-side-column">
        <section class="ev-panel ev-thinking"><div class="ev-panel-head"><h2>当前认识</h2>
          <button class="ev-text-btn" id="ev-edit-insights" type="button">编辑</button></div>
          <div>${t.current_understanding ? mdBody(t.current_understanding) : `<p class="ev-placeholder">基于目前的材料，我怎么看？</p>`}</div>
          <h3>待解问题</h3><div>${t.open_questions ? mdBody(t.open_questions) : `<p class="ev-placeholder">还缺哪些证据，下一步核对什么？</p>`}</div></section>
        ${t.status === "archived" ? `<section class="ev-panel"><div class="ev-panel-head"><h2>归档总结</h2>
          <button class="ev-text-btn" id="ev-edit-summary" type="button">编辑</button></div>
          <div>${t.summary ? mdBody(t.summary) : `<p class="ev-placeholder">为这段跟踪留下结论与关键依据。</p>`}</div></section>` : ""}
        <section class="ev-panel ev-manage"><button class="ev-text-btn" id="ev-export" type="button">导出此事件</button>
          <button class="ev-text-btn danger" id="ev-delete" type="button">删除事件</button>
          <p>删除只移除事件、进展和关联，不删除任何原收存资料。</p></section>
      </aside></div></div>`;
    bindDetail(t, updates, links);
  }

  function updateRow(u) {
    return `<article class="ev-update" data-update="${u.id}"><div class="ev-dot" aria-hidden="true"></div>
      <div class="ev-update-content"><div class="ev-update-meta"><time>发生于 ${safe(fmt(u.happened_at))}</time>
        <span>记录于 ${safe(fmt(u.observed_at))}</span><button type="button" class="ev-text-btn" data-update-edit="${u.id}">编辑</button>
        <button type="button" class="ev-text-btn danger" data-update-delete="${u.id}">删除</button></div>
        <div class="ev-update-view">${mdBody(u.body)}</div><div class="ev-update-editor" hidden></div></div></article>`;
  }

  function linkRow(l) {
    return `<article class="ev-linked"><a href="#/c/${l.clip_id}"><strong>${safe(l.title)}</strong>
      <span>${safe(l.source_type === "file" ? (l.file_name || "文件") : (l.url || "文字收存"))}</span></a>
      ${l.note ? `<p>${safe(l.note)}</p>` : ""}<button class="ev-text-btn danger" type="button"
        data-link-delete="${l.id}">解除关联</button></article>`;
  }

  function bindDetail(topic, updates, links) {
    const id = topic.id;
    $("#ev-edit").addEventListener("click", () => openTopicEditor(topic));
    $("#ev-edit-insights").addEventListener("click", () => openInsightsEditor(topic));
    $("#ev-edit-summary")?.addEventListener("click", () => {
      modal("归档总结", `<form class="ev-form" id="ev-summary-form"><textarea class="ev-input"
        id="ev-summary" rows="12" maxlength="20000" placeholder="结论、关键依据与复盘。">${safe(topic.summary)}</textarea>
        <button class="btn small" type="submit">保存总结</button></form>`, (root) => {
        $("#ev-summary-form", root).addEventListener("submit", (e) => {
          e.preventDefault();
          save($("button[type=submit]", root), async () => {
            await post(`/api/event-topics/${id}`, { summary: $("#ev-summary", root).value });
            closeModal();
            await renderDetail(id);
            notify("归档总结已保存");
          });
        });
      });
    });
    $("#ev-state").addEventListener("change", async (e) => {
      const previous = topic.status;
      try {
        await post(`/api/event-topics/${id}`, { status: e.target.value });
        await renderDetail(id);
        notify("事件状态已更新");
      } catch (err) { e.target.value = previous; notify(err.message, "err"); }
    });
    $("#ev-update-form").addEventListener("submit", (e) => {
      e.preventDefault();
      save($("button[type=submit]", e.currentTarget), async () => {
        await post(`/api/event-topics/${id}/updates`, {
          happened_at: $("#ev-happened").value, body: $("#ev-update-body").value,
        });
        await renderDetail(id);
        notify("进展已追加");
      });
    });
    $("#ev-order").addEventListener("click", () => {
      const order = eventOrder() === "asc" ? "desc" : "asc";
      try { sessionStorage.setItem("ev_order", order); } catch (_) { /* 仅本页有效 */ }
      renderDetail(id).catch((e) => notify(e.message, "err"));
    });
    const orderedUpdates = eventOrder() === "asc" ? [...updates].reverse() : updates;
    orderedUpdates.forEach((u) => {
      $(`[data-update-edit="${u.id}"]`).addEventListener("click", () => openUpdateEditor(u));
      $(`[data-update-delete="${u.id}"]`).addEventListener("click", async () => {
        if (!await glConfirm("删除这条进展？关联的收存资料不会受影响。", { danger: true, confirmText: "删除" })) return;
        try { await post(`/api/event-topics/updates/${u.id}/delete`, {}); await renderDetail(id); notify("进展已删除"); }
        catch (err) { notify(err.message, "err"); }
      });
    });
    links.forEach((l) => {
      $(`[data-link-delete="${l.id}"]`).addEventListener("click", async () => {
        if (!await glConfirm(`解除与《${l.title}》的关联？原收存资料会完整保留。`, { confirmText: "解除关联" })) return;
        try { await post(`/api/event-topics/links/${l.id}/delete`, {}); await renderDetail(id); notify("已解除关联"); }
        catch (err) { notify(err.message, "err"); }
      });
    });
    bindClipSearch(id, links);
    $("#ev-export").addEventListener("click", () => exportTopic(topic, updates, links));
    $("#ev-delete").addEventListener("click", async () => {
      if (!await glConfirm(`删除事件《${topic.title}》？进展与关联将一并删除，原收存资料完整保留。`,
        { danger: true, confirmText: "删除事件" })) return;
      try { await post(`/api/event-topics/${id}/delete`, {}); location.hash = "#/events"; notify("事件已删除"); }
      catch (err) { notify(err.message, "err"); }
    });
  }

  function openUpdateEditor(update) {
    const host = $(`[data-update="${update.id}"] .ev-update-editor`);
    const view = $(`[data-update="${update.id}"] .ev-update-view`);
    if (!host || !host.hidden) return;
    host.hidden = false;
    view.hidden = true;
    host.innerHTML = `<form class="ev-form ev-edit-area"><input class="ev-input" type="datetime-local"
      id="ev-edit-happened-${update.id}" value="${safe(update.happened_at)}" required>
      <textarea class="ev-input" id="ev-edit-body-${update.id}" rows="5" maxlength="12000" required>${safe(update.body)}</textarea>
      <div><button class="btn small" type="submit">保存进展</button>
      <button class="btn small ghost" type="button" data-edit-cancel>取消</button></div></form>`;
    const form = $("form", host);
    $("[data-edit-cancel]", host).addEventListener("click", () => { host.hidden = true; view.hidden = false; });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      save($("button[type=submit]", form), async () => {
        await post(`/api/event-topics/updates/${update.id}`, {
          happened_at: $(`#ev-edit-happened-${update.id}`).value,
          body: $(`#ev-edit-body-${update.id}`).value,
        });
        await renderDetail(update.topic_id);
        notify("进展已保存");
      });
    });
    $("textarea", host).focus();
  }

  function bindClipSearch(topicId, links) {
    const input = $("#ev-clip-search"), hits = $("#ev-clip-hits");
    let timer = 0, seq = 0;
    const linked = new Set(links.map((l) => l.clip_id));
    const search = async () => {
      const q = input.value.trim(), current = ++seq;
      if (!q) { hits.innerHTML = ""; return; }
      try {
        const data = await api(`/api/event-topics/clip-search?q=${encodeURIComponent(q)}`);
        if (current !== seq || !input.isConnected) return;
        const rows = (data.clips || []).filter((c) => !linked.has(c.id));
        hits.innerHTML = rows.length ? rows.map((c) => `<div class="ev-clip-hit"><span>${safe(c.title)}</span>
          <button type="button" class="btn small ghost" data-clip-add="${c.id}">关联</button></div>`).join("")
          : `<div class="ev-inline-empty">没有可关联的匹配资料。</div>`;
        $$("[data-clip-add]", hits).forEach((button) => button.addEventListener("click", () => {
          save(button, async () => {
            await post(`/api/event-topics/${topicId}/links`, { clip_id: button.dataset.clipAdd, note: "" });
            await renderDetail(topicId);
            notify("资料已关联");
          });
        }));
      } catch (err) { hits.innerHTML = `<div class="ev-inline-empty">${safe(err.message)}</div>`; }
    };
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 250); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); search(); } });
  }

  function exportTopic(topic, updates, links) {
    const lines = [`# ${topic.title}`, "", `状态：${statusName(topic.status)}`,
      `跟踪目的：${topic.purpose || "未填写"}`, "", "## 当前认识", topic.current_understanding || "未填写",
      "", "## 待解问题", topic.open_questions || "未填写", "", "## 进展时间线"];
    [...updates].reverse().forEach((u) => lines.push("", `### ${fmt(u.happened_at)}`, u.body));
    lines.push("", "## 关联资料");
    links.forEach((l) => lines.push(`- [${l.title}](#/c/${l.clip_id})${l.note ? `：${l.note}` : ""}`));
    if (topic.summary) lines.push("", "## 归档总结", topic.summary);
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url;
    a.download = `${topic.title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 80) || "事件"}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("事件已导出");
  }

  // 收存详情页不再挂任何事件内容：事件只在 /events 下创建、关联、更新与复盘。
  G.addRoute(/^\/events$/, renderList);
  G.addRoute(/^\/events\/(\d+)$/, (m) => renderDetail(Number(m[1])));
  $("#sect-switch .sect-btn[data-sect=events]")?.addEventListener("click", () => { location.hash = "#/events"; });
  window.GLEvents = { onRoute, draftOpen };
})();
