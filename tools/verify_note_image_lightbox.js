/* 观澜 · 随手记图片灯箱端到端验证
 *
 *   node tools/verify_note_image_lightbox.js
 *
 * 只读验证：不碰数据库。为了让「多图翻页」也能跑到，只在浏览器里把
 * /api/clips/<id> 的响应临时复制一张图，页面关掉就没了。
 * 检查重点：点缩略图不再新开浏览器标签 / 页面，图片直接在应用内放大显示。
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const PW = process.env.PW_PATH ||
  path.join(process.env.USERPROFILE || "", ".workbuddy/binaries/node/versions/22.22.2-6/node_modules/@playwright/cli/node_modules/playwright");
const { chromium } = require(PW);

const ROOT = path.resolve(__dirname, "..");
const BASE = "http://127.0.0.1:8790/";
const CLIP = Number(process.env.CLIP_ID || 30);
const SHOTS = path.join(ROOT, "docs", "shots-note-image-lightbox");
const LOGFILE = path.join(ROOT, "docs", "audit-2026-11-15-note-image-lightbox.log");
const log = [];
const say = (s) => { log.push(s); console.log(s); };
let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed += 1;
  say(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " · " + detail : ""}`);
};
const hash = (p) => crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex");

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  say(`观澜 · 随手记图片灯箱验证  ${new Date().toISOString()}`);
  say(`目标条目 clip_id=${CLIP}`);

  /* 1. 静态：源码与运行时资源一致，且缩略图不再是外跳链接 */
  const pairs = [
    [path.join(ROOT, "static", "app.js"), path.join(ROOT, "dist", "观澜", "_internal", "static", "app.js")],
    [path.join(ROOT, "static", "extra.css"), path.join(ROOT, "dist", "观澜", "_internal", "static", "extra.css")],
  ];
  pairs.forEach(([src, runtime]) => check(`运行时资源与源码一致 · ${path.basename(src)}`,
    fs.existsSync(runtime) && hash(src) === hash(runtime)));

  const js = fs.readFileSync(pairs[0][0], "utf8");
  const css = fs.readFileSync(pairs[1][0], "utf8");
  check("随手记缩略图已改成按钮", /class="note-img" data-note-image="1"/.test(js));
  check("随手记缩略图不再 target=_blank",
    !/class="note-img"[\s\S]{0,240}target="_blank"/.test(js));
  check("灯箱逻辑已接入", js.includes("function openImageLightbox")
    && js.includes('$$("[data-note-image]")'));
  check("灯箱样式已接入", css.includes(".gl-lightbox") && css.includes("body.gl-lightbox-open"));
  check("键盘/焦点/减弱动效兜底齐全",
    css.includes(":focus-visible") && css.includes("prefers-reduced-motion")
    && js.includes("Escape") && js.includes("trigger?.focus?.()"));

  /* 2. 运行时：真实页面点一次 */
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const opened = [];
  context.on("page", (p) => { if (p !== context.pages()[0]) opened.push(p.url()); });
  const pageErrors = [];
  let page = null;

  try {
    page = await context.newPage();
    page.on("pageerror", (e) => pageErrors.push(e.message));
    page.on("popup", (p) => opened.push("popup:" + p.url()));

    // 把这条随手记的一张图临时复制成两张，验证左右翻页；不改服务端数据
    await page.route(`**/api/clips/${CLIP}*`, async (route) => {
      const resp = await route.fetch();
      const body = await resp.text();
      let json;
      try { json = JSON.parse(body); } catch (_) { return route.fulfill({ response: resp, body }); }
      const note = (json.notes || []).find((n) => (n.images || []).length);
      if (note && note.images.length === 1) {
        note.images.push({ ...note.images[0], name: "验证用第二张.jpg" });
      }
      const headers = { ...resp.headers() };
      delete headers["content-length"];
      headers["content-type"] = "application/json; charset=utf-8";
      return route.fulfill({ response: resp, body: JSON.stringify(json), headers });
    });

    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.evaluate((id) => { location.hash = `#/c/${id}`; }, CLIP);
    await page.waitForSelector(".note-img[data-note-image]", { timeout: 15000 });

    const tiles = await page.locator(".note-img[data-note-image]").count();
    check("随手记缩略图渲染成可点按钮", tiles >= 2, `${tiles} 张`);
    const before = page.url();

    await page.locator(".note-img[data-note-image]").first().click();
    await page.waitForSelector(".gl-lightbox", { state: "visible", timeout: 8000 });
    check("没有新页面 / 弹窗（不再打开浏览器）", opened.length === 0, opened.join(", ") || "0 个");
    check("页面地址没变", page.url() === before, page.url());
    check("灯箱已在应用内打开", await page.locator(".gl-lightbox").isVisible());
    check("页面滚动已锁住",
      await page.evaluate(() => document.body.classList.contains("gl-lightbox-open")));

    const first = await page.evaluate(() => {
      const img = document.querySelector(".gl-lb-img");
      return {
        src: img.getAttribute("src"),
        naturalWidth: img.naturalWidth,
        count: document.querySelector(".gl-lb-count").textContent,
        name: document.querySelector(".gl-lb-name").textContent,
        dl: document.querySelector(".gl-lb-download").getAttribute("href"),
        dlTarget: document.querySelector(".gl-lb-download").getAttribute("target"),
      };
    });
    await page.waitForFunction(() => document.querySelector(".gl-lb-img")?.naturalWidth > 0,
      null, { timeout: 8000 }).catch(() => { });
    const loaded = await page.evaluate(() => document.querySelector(".gl-lb-img").naturalWidth);
    check("大图直接内联显示，原图已加载", loaded > 0, `${loaded}px 宽`);
    check("显示的是该条随手记的原图", /\/api\/notes\/img\//.test(first.src), first.src);
    check("多图计数正确", first.count === "1 / 2", first.count);
    check("下载原件走应用内地址、不外跳",
      first.dl.includes("dl=1") && !first.dlTarget, first.dl);

    await page.locator('[data-lb="in"]').click();
    check("放大按钮生效",
      (await page.locator(".gl-lb-scale").textContent()) === "125%");
    await page.locator('[data-lb="out"]').click();
    check("缩小按钮生效",
      (await page.locator(".gl-lb-scale").textContent()) === "适应");
    await page.mouse.move(720, 450);
    await page.mouse.wheel(0, -240);
    check("滚轮缩放生效",
      (await page.locator(".gl-lb-scale").textContent()) !== "适应");
    await page.locator('[data-lb="fit"]').click();
    check("适应窗口可一键复位",
      (await page.locator(".gl-lb-scale").textContent()) === "适应");

    await page.locator(".gl-lb-img").click();
    check("点图片本身不误关", await page.locator(".gl-lightbox").count() === 1);

    await page.keyboard.press("ArrowRight");
    const second = await page.evaluate(() => ({
      count: document.querySelector(".gl-lb-count").textContent,
      name: document.querySelector(".gl-lb-name").textContent,
      scale: document.querySelector(".gl-lb-scale").textContent,
    }));
    check("→ 翻到下一张并复位缩放",
      second.count === "2 / 2" && second.name === "验证用第二张.jpg" && second.scale === "适应",
      `${second.count} / ${second.name} / ${second.scale}`);
    await page.keyboard.press("ArrowLeft");
    check("← 翻回上一张",
      (await page.locator(".gl-lb-count").textContent()) === "1 / 2");

    await page.screenshot({ path: path.join(SHOTS, "随手记图片-灯箱.png") });

    await page.keyboard.press("Escape");
    await page.waitForSelector(".gl-lightbox", { state: "detached", timeout: 5000 });
    check("Esc 关闭灯箱", await page.locator(".gl-lightbox").count() === 0);
    check("关闭后恢复滚动与焦点", await page.evaluate(() =>
      !document.body.classList.contains("gl-lightbox-open")
      && document.activeElement?.classList.contains("note-img")));

    await page.locator(".note-img[data-note-image]").first().click();
    await page.waitForSelector(".gl-lightbox", { state: "visible" });
    await page.locator(".gl-lb-stage").click({ position: { x: 8, y: 8 } });
    await page.waitForSelector(".gl-lightbox", { state: "detached", timeout: 5000 }).catch(() => { });
    check("点背景也能关闭", await page.locator(".gl-lightbox").count() === 0);
    check("全程无前端脚本异常", pageErrors.length === 0, pageErrors.join(" | ") || "0 个");
    check("全程没有新页面 / 弹窗", opened.length === 0, opened.join(", ") || "0 个");
  } catch (e) {
    failed += 1;
    say(`FAIL  运行时验证中断 · ${e.stack || e.message}`);
  } finally {
    await browser.close();
    say(`\n结果：${failed === 0 ? "全部通过" : `${failed} 项未通过`}`);
    fs.writeFileSync(LOGFILE, log.join("\n") + "\n", "utf8");
    process.exitCode = failed === 0 ? 0 : 1;
  }
})();
