/* 验证「阅读中心 · A− / A＋ / ◐ 三个按钮」是否真的作用到正文纸面。
 *
 *   node tools/verify_reader_toolbar.js
 *
 * 依赖本机已装好的 Playwright（WorkBuddy 自带），只跑本地 http://127.0.0.1:8790。
 * 不改数据：跑完把阅读进度与 gl_reading_prefs 偏好写回原值，产物落在 docs/。
 */
const path = require("path");
const fs = require("fs");

const PW = process.env.PW_PATH ||
  path.join(process.env.USERPROFILE || "", ".workbuddy/binaries/node/versions/22.22.2-6/node_modules/@playwright/cli/node_modules/playwright");
const { chromium } = require(PW);

const ROOT = path.resolve(__dirname, "..");
const BASE = "http://127.0.0.1:8790/";
const BOOK = Number(process.env.BOOK || 19);
const OUT = path.join(ROOT, "docs", "shots-toolbar");
const LOGFILE = path.join(ROOT, "docs", "阅读中心-字号纸张按钮修复.verify.log");
const log = [];
const say = (s) => { log.push(s); console.log(s); };
const num = (s) => parseFloat(String(s || "").replace("px", "")) || 0;
let failed = 0;
const check = (name, ok, detail) => {
  if (!ok) failed += 1;
  say(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " · " + detail : ""}`);
};

const readProgress = async () => {
  const r = await fetch(BASE.replace(/\/$/, "") + `/api/reading/book/${BOOK}`);
  const d = await r.json();
  const it = d.item || {};
  return { position: it.position, progress: it.progress, status: it.status, duration: it.duration };
};

const writeProgress = async (p) => {
  await fetch(BASE.replace(/\/$/, "") + `/api/reading/progress/${BOOK}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...p, seconds: 0, chars: 0 }),
  });
};

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const original = await readProgress();
  say(`观澜 · 阅读器工具栏验证  ${new Date().toISOString()}`);
  say(`书目 clip_id=${BOOK} · 原进度 position=${original.position} progress=${original.progress}`);

  const browser = await chromium.launch({ headless: true });
  const pageErrors = [];
  const consoleErrors = [];
  let prefsBefore = null;
  let page = null;

  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", (e) => pageErrors.push(e.message));
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

    await page.goto(BASE, { waitUntil: "networkidle" });
    prefsBefore = await page.evaluate(() => localStorage.getItem("gl_reading_prefs"));
    say(`原偏好 ${prefsBefore === null ? "(无，用默认值)" : prefsBefore}`);

    // 直接带 hash 打开会先落回 #/，这里模拟真实入口：先进首页，再进书
    await page.evaluate((h) => { location.hash = h; }, `#/reading/book/${BOOK}`);
    await page.waitForSelector("#rc-paper-surface", { timeout: 15000 });
    await page.waitForTimeout(700);

    const state = () => page.evaluate(() => {
      const p = document.querySelector("#rc-paper-surface");
      const art = document.querySelector("#rc-article");
      return {
        fs: p ? p.style.getPropertyValue("--rc-fs").trim() : null,
        lh: p ? p.style.getPropertyValue("--rc-lh").trim() : null,
        w: p ? p.style.getPropertyValue("--rc-w").trim() : null,
        paper: p ? p.dataset.paper : null,
        bg: p ? getComputedStyle(p).backgroundColor : null,
        artFs: art ? getComputedStyle(art).fontSize : null,
        minusTitle: (document.querySelector("#rc-font-minus") || {}).title || "",
        plusTitle: (document.querySelector("#rc-font-plus") || {}).title || "",
        paperTitle: (document.querySelector("#rc-paper-btn") || {}).title || "",
        dup: document.querySelectorAll("#rc-paper").length,
        surfaces: document.querySelectorAll("#rc-paper-surface").length,
        btn: !!document.querySelector("#rc-paper-btn"),
      };
    });
    const toastText = () => page.evaluate(() =>
      (document.querySelector("#toast-host .toast-msg") || {}).textContent || "");

    const s0 = await state();
    say("初始状态 " + JSON.stringify(s0));
    check("无重复 id #rc-paper", s0.dup === 0, `命中 ${s0.dup} 个`);
    check("纸面容器唯一且已带 CSS 变量", s0.surfaces === 1 && !!s0.fs, `surfaces=${s0.surfaces} --rc-fs=${s0.fs}`);
    check("切换纸张按钮独立存在", s0.btn === true);
    check("按钮标题初始同步", /当前 \d+px/.test(s0.plusTitle), s0.plusTitle);
    await page.screenshot({ path: path.join(OUT, "1-before.png") });

    await page.click("#rc-font-plus");
    const s1 = await state();
    check("A＋ 纸面 --rc-fs +1px", num(s1.fs) === num(s0.fs) + 1, `${s0.fs} → ${s1.fs}`);
    check("A＋ 正文实际字号 +1px", num(s1.artFs) === num(s0.artFs) + 1, `${s0.artFs} → ${s1.artFs}`);
    check("A＋ 按钮标题同步", s1.plusTitle.includes(`${num(s1.fs)}px`), s1.plusTitle);

    await page.click("#rc-font-plus");
    const s2 = await state();
    check("A＋ 再点继续 +1px", num(s2.fs) === num(s1.fs) + 1, `${s1.fs} → ${s2.fs}`);
    await page.screenshot({ path: path.join(OUT, "2-font-larger.png") });

    await page.click("#rc-font-minus");
    const s3 = await state();
    check("A− 纸面 --rc-fs -1px", num(s3.fs) === num(s2.fs) - 1, `${s2.fs} → ${s3.fs}`);
    check("A− 正文实际字号 -1px", num(s3.artFs) === num(s2.artFs) - 1, `${s2.artFs} → ${s3.artFs}`);
    check("A− 按钮标题同步", s3.minusTitle.includes(`${num(s3.fs)}px`), s3.minusTitle);

    const papers = [s3.paper];
    await page.click("#rc-paper-btn");
    // .rc-paper 上有 background 过渡，等它落定再读 computed style，否则读到起始帧
    await page.waitForTimeout(450);
    const s4 = await state();
    papers.push(s4.paper);
    check("◐ 纸面 data-paper 真的变了", s4.paper !== s3.paper, `${s3.paper} → ${s4.paper}`);
    check("◐ 行距与宽度同步变化", s4.lh !== s3.lh && s4.w !== s3.w,
      `lh ${s3.lh}→${s4.lh} / w ${s3.w}→${s4.w}`);
    check("◐ 纸面背景实际换色", s4.bg !== s3.bg, `${s3.bg} → ${s4.bg}`);
    const t4 = await toastText();
    check("◐ 有可见文字反馈", t4.includes("纸张 ·"), JSON.stringify(t4));
    check("◐ 按钮标题显示当前纸张", s4.paperTitle.includes("当前："), s4.paperTitle);
    await page.screenshot({ path: path.join(OUT, "3-paper-changed.png") });

    await page.click("#rc-paper-btn");
    papers.push((await state()).paper);
    await page.click("#rc-paper-btn");
    papers.push((await state()).paper);
    check("◐ 三态循环且回到起点",
      papers[1] !== papers[0] && papers[2] !== papers[1] && papers[3] === papers[0],
      papers.join(" → "));

    check("无未捕获运行时异常", pageErrors.length === 0, pageErrors.join(" | ") || "0 个");
    if (consoleErrors.length) say(`（参考）console error ${consoleErrors.length} 条：${consoleErrors.slice(0, 3).join(" | ")}`);
  } finally {
    // 用户原有的偏好与阅读进度原样写回，验证不留痕
    try {
      await page?.evaluate((v) => {
        if (v === null) localStorage.removeItem("gl_reading_prefs");
        else localStorage.setItem("gl_reading_prefs", v);
      }, prefsBefore);
    } catch (_) { /* 页面已关闭就算了 */ }
    await writeProgress(original).catch(() => {});
    await browser.close();
  }

  say(`──────── 结果：${failed === 0 ? "全部通过" : failed + " 项失败"} ────────`);
  fs.writeFileSync(LOGFILE, log.join("\n") + "\n", "utf8");
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  say("FATAL " + (e && e.stack ? e.stack : String(e)));
  fs.writeFileSync(LOGFILE, log.join("\n") + "\n", "utf8");
  process.exit(2);
});
