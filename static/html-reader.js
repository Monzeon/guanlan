/* 观澜 · HTML 原件直读器
 *
 * 背景：服务端 _clip_file 对 .html 仍返回 Content-Disposition: attachment
 * （历史白名单为了避免同源 XSS 把 html/svg 排除在内联之外）。fetch 不受
 * Content-Disposition 影响，能拿到原件文本；这里清洗后用 sandbox iframe
 * 渲染，脚本/表单/弹窗/同源一律禁用，因此在观澜里「打开 HTML 直接读」，
 * 既不用下载，也不给存储型 XSS 开门。
 *
 * 只读同源 /api/clips/<id>/file，不接受任意 ?src=，避免变成 SSRF 跳板。
 */
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var params = new URLSearchParams(location.search);
  var clipId = params.get("clip") || "";
  var stage = $("#gl-stage");
  var statusEl = $("#gl-status");
  var titleEl = $("#gl-title");
  var backEl = $("#gl-back");
  var dlEl = $("#gl-download");

  var FILE_URL = "/api/clips/" + encodeURIComponent(clipId) + "/file";

  function say(msg, isError) {
    statusEl.textContent = msg;
    statusEl.classList.toggle("err", !!isError);
    statusEl.style.display = "";
  }
  function ready(note) {
    say(note);
    document.documentElement.setAttribute("data-gl-ready", "1");
    document.title = "观澜 · " + (titleEl.textContent || "HTML 原件") + " · " + note;
  }
  function fail(msg) { say(msg, true); }

  /* ---------- 编码：原件可能是 UTF-8 / GBK / 其它 ---------- */
  function decodeBody(buf) {
    var bytes = new Uint8Array(buf);
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch (e) { /* 不是 UTF-8，继续按声明的字符集解 */ }
    var probe = new TextDecoder("latin1").decode(bytes.slice(0, 4096));
    var m = /<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)/i.exec(probe) ||
            /^\s*<\?xml[^>]+encoding\s*=\s*["']([\w-]+)/i.exec(probe);
    var enc = m ? m[1] : "gb18030";
    try { return new TextDecoder(enc).decode(bytes); }
    catch (e2) { return new TextDecoder("utf-8").decode(bytes); }
  }

  /* ---------- 净化：DOMParser 不执行脚本，再逐项收紧 ---------- */
  var DROP_TAGS = [
    "script", "noscript", "iframe", "object", "embed", "applet",
    "link", "meta", "base", "frame", "frameset", "template",
    "audio", "video", "source", "track", "portal", "foreignobject"
  ];
  var BAD_ATTRS = {
    srcdoc: 1, formaction: 1, "http-equiv": 1, ping: 1, manifest: 1,
    dynsrc: 1, lowsrc: 1, background: 1
  };
  var URL_ATTRS = {
    href: 1, src: 1, poster: 1, "xlink:href": 1, action: 1,
    cite: 1, longdesc: 1, usemap: 1, data: 1
  };

  function cleanCss(css) {
    return String(css || "")
      .replace(/@import[^;]*;/gi, "")
      .replace(/-moz-binding\s*:[^;}]*;?/gi, "")
      .replace(/expression\s*\([^)]*\)/gi, "")
      .replace(/behavior\s*:[^;}]*;?/gi, "")
      .replace(/url\(\s*(['"]?)\s*(?:javascript|vbscript):[^)]*\)/gi, "url()");
  }

  function safeUrl(raw) {
    var t = String(raw || "").replace(/[\u0000-\u0020]+/g, "").toLowerCase();
    if (!t) return true;
    if (/^(javascript|vbscript|livescript|mocha):/.test(t)) return false;
    if (/^data:/.test(t)) {
      // 只放行位图与字体；data:text/html、data:image/svg+xml 一律不放行
      return /^data:image\/(png|jpe?g|gif|webp|bmp|avif);/i.test(t) ||
             /^data:(?:font\/|application\/(?:font-woff2?|octet-stream))/i.test(t);
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(t)) {
      return /^(https?|mailto|tel|blob):/.test(t);
    }
    return true;                       // 相对路径、#锚点、/绝对路径
  }

  function sanitize(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");

    DROP_TAGS.forEach(function (tag) {
      doc.querySelectorAll(tag).forEach(function (n) { n.remove(); });
    });

    var styles = [];
    doc.querySelectorAll("style").forEach(function (s) {
      styles.push(cleanCss(s.textContent));
      s.remove();
    });

    doc.querySelectorAll("*").forEach(function (el) {
      Array.prototype.slice.call(el.attributes).forEach(function (attr) {
        var name = attr.name.toLowerCase();
        var val = String(attr.value || "");
        var compact = val.replace(/[\u0000-\u0020]+/g, "");

        if (name.indexOf("on") === 0 || BAD_ATTRS[name]) {
          el.removeAttribute(attr.name); return;
        }
        if (name === "style") {
          if (/(expression\s*\(|behavior\s*:|@import|-moz-binding|javascript:)/i.test(val)) {
            el.removeAttribute(attr.name);
          } else {
            el.setAttribute(attr.name, cleanCss(val));
          }
          return;
        }
        if (URL_ATTRS[name] && !safeUrl(compact)) {
          el.removeAttribute(attr.name); return;
        }
        if (name === "srcset" && /(javascript:|data:text\/html)/i.test(compact)) {
          el.removeAttribute(attr.name); return;
        }
        if (/^\s*(?:javascript|vbscript|data:text\/html)/i.test(val)) {
          el.removeAttribute(attr.name); return;
        }
      });
    });

    var baseCss = [
      "html{-webkit-text-size-adjust:100%}",
      "body{margin:0;padding:24px 28px;background:#fff;color:#23211e;",
      "font:15px/1.75 -apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif}",
      "img,canvas,svg{max-width:100%;height:auto}",
      "table{max-width:100%;border-collapse:collapse}",
      "pre,code{white-space:pre-wrap;word-break:break-word}",
      "a{color:#0f766e}"
    ].join("");

    var styleTags = styles.map(function (s) { return "<style>" + s + "</style>"; }).join("");
    var body = doc.body ? doc.body.innerHTML : "";
    var head = '<meta charset="utf-8"><meta name="viewport" ' +
      'content="width=device-width, initial-scale=1">' +
      "<style>" + baseCss + "</style>" + styleTags;

    return {
      doc: '<!DOCTYPE html><html><head>' + head + "</head><body>" + body + "</body></html>",
      body: body
    };
  }

  /* ---------- 文件名 / 返回链接 ---------- */
  function nameFromDisposition(resp) {
    var cd = resp.headers.get("content-disposition") || "";
    var m = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(cd);
    if (m) { try { return decodeURIComponent(m[1].trim().replace(/^"|"$/g, "")); } catch (e) {} }
    m = /filename\s*=\s*"([^"]+)"/i.exec(cd);
    return m ? m[1] : "";
  }

  function setMeta(name) {
    titleEl.textContent = name || "HTML 原件";
    document.title = "观澜 · " + (name || "原件直读");
    if (name) dlEl.download = name;
  }

  /* ---------- 渲染 ---------- */
  function render(safe) {
    var frame = document.createElement("iframe");
    frame.className = "gl-frame";
    frame.title = "HTML 原件";
    frame.setAttribute("sandbox", "");     // 空值 = 禁脚本/表单/弹窗/同源/顶层跳转
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.setAttribute("loading", "eager");

    var settled = false;
    function done() {
      if (settled) return;
      settled = true;
      ready("已按原版式直读 · " + Math.max(1, Math.round(safe.doc.length / 1024)) +
            "KB · 脚本与外链已隔离");
    }
    frame.addEventListener("load", function () {
      try {
        // sandbox 无 allow-same-origin 时访问 contentDocument 会抛错，
        // 抛错恰好说明文档已正常加载；两条路都算成功。
        var d = frame.contentDocument;
        if (d && d.body && d.body.innerHTML.length) done();
        else done();
      } catch (e) { done(); }
    });
    // 某些宿主（WebView2 旧版 / 严格 CSP）会拦 srcdoc，降级到已净化内容直插
    var timer = setTimeout(function () {
      if (settled) return;
      settled = true;
      frame.remove();
      var div = document.createElement("div");
      div.className = "gl-fallback";
      div.innerHTML = safe.body;          // 已去脚本/事件/js: URL，父页 CSP 再兜一层
      stage.appendChild(div);
      ready("兼容模式直读 · " + Math.max(1, Math.round(safe.doc.length / 1024)) +
            "KB · 脚本已隔离");
    }, 1800);
    frame.addEventListener("load", function () { clearTimeout(timer); });

    frame.srcdoc = safe.doc;
    stage.appendChild(frame);
  }

  async function load() {
    if (!/^\d+$/.test(clipId)) {
      fail("缺少有效的条目编号：请用详情页的「直接读」打开。");
      return;
    }
    backEl.href = "/#/c/" + clipId;
    dlEl.href = FILE_URL + "?dl=1";

    var resp;
    try {
      resp = await fetch(FILE_URL, { credentials: "same-origin", cache: "no-store" });
    } catch (e) {
      fail("读取原件失败：" + (e && e.message ? e.message : e)); return;
    }
    if (!resp.ok) { fail("读取原件失败（HTTP " + resp.status + "）"); return; }

    setMeta(nameFromDisposition(resp));
    try {
      var meta = await fetch("/api/clips/" + encodeURIComponent(clipId),
                             { credentials: "same-origin", cache: "no-store" });
      if (meta.ok) {
        var data = await meta.json();
        var c = data.clip || {};
        setMeta(c.file_name || c.title || nameFromDisposition(resp));
      }
    } catch (e) { /* 文件名拿不到不影响直读 */ }

    var buf;
    try { buf = await resp.arrayBuffer(); }
    catch (e) { fail("原件内容读取中断"); return; }

    var safe;
    try { safe = sanitize(decodeBody(buf)); }
    catch (e) { fail("版式整理失败：" + (e && e.message ? e.message : e)); return; }

    say("原件已就绪，正在按原版式直读…");
    render(safe);
  }

  load();
})();
