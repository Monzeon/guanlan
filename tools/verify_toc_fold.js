/* 验证「阅读中心 · 目录可收起」的交互闭环（桌面折列 + 窄屏抽屉 + 状态记忆 + 视线不丢）。
 *
 *   node tools/verify_toc_fold.js
 *
 * 依赖本机已装好的 Playwright（WorkBuddy 自带），产物落在 docs/shots-toc-fold/。
 * 只跑本地 http://127.0.0.1:8790：不改数据，跑完把这本书的阅读进度写回原值。
 */
const path = require("path");
const fs = require("fs");

const PW = process.env.PW_PATH ||
  path.join(process.env.USERPROFILE || "", ".workbuddy/binaries/node/versions/22.22.2-6/node_modules/@playwright/cli/node_modules/playwright");
const { chromium } = require(PW);

const ROOT = path.resolve(__dirname, "..");
const BASE = "http://127.0.0.1:8790/";
const BOOK = 19;
const BOOK_URL = `/api/reading/book/${BOOK}`;
const OUT = path.join(ROOT, "docs", "shots-toc-fold");
const log = [];
const say = (s) => { log.push(s); console.log(s); };

// 直接带 hash 打开时 app.js 会先落回 #/，这里模拟真实入口：先进首页，再进书
async function openBook(page) {
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.evaluate((h) => { location.hash = h; }, `#/reading/book/${BOOK}`);
  await page.waitForSelector(".rc-toc-item");
  await page.waitForTimeout(2200);   // 等「续读位置」回填 + 平滑滚动落定
}

// 往上挪到章节中段，别停在末尾：末尾没有下滑空间，锚点必然对不齐
async function scrollMid(page) {
  await page.evaluate(() => {
    const c = document.querySelector("#rc-center");
    c.style.scrollBehavior = "auto";
    c.scrollTop = Math.round((c.scrollHeight - c.clientHeight) * 0.42);
    c.style.scrollBehavior = "";
  });
  await page.waitForTimeout(150);
}

const readProgress = async () => {
  const r = await fetch(BASE.replace(/\/$/, "") + BOOK_URL);
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
  say(`原进度  position=${original.position} progress=${original.progress} status=${original.status}`);
  const browser = await chromium.launch({ headless: true });
  const errors = [];

  try {
    // ── 桌面 1440：折列 / 视线 / 记忆 / 回开 ──
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.evaluate(() => localStorage.removeItem("gl_reading_prefs"));
    await openBook(page);
    await scrollMid(page);
    await page.screenshot({ path: path.join(OUT, "1-desktop-open.png") });

    const cols = () => page.evaluate(() =>
      getComputedStyle(document.querySelector(".rc-rgrid")).gridTemplateColumns);
    const topPara = () => page.evaluate(() => {
      const c = document.querySelector("#rc-center");
      const top = c.scrollTop, cTop = c.getBoundingClientRect().top;
      const p = [...c.querySelectorAll("#rc-article p[data-a-start]")]
        .find((x) => (x.getBoundingClientRect().top - cTop + top) + x.offsetHeight > top + 2);
      return p ? p.dataset.aStart : null;
    });
    const para0 = await topPara();
    say("桌面 · 初始列宽  " + (await cols()) + " · 视线段 " + para0);

    await page.click("#rc-fold-left");
    await page.waitForTimeout(700);
    say("桌面 · 点「收起」后列宽  " + (await cols()));
    say("桌面 · reader class  " + (await page.evaluate(() =>
      document.querySelector("#rc-reader").className)));
    say("桌面 · 收起后视线段仍在  " + (await topPara() === para0));
    say("桌面 · 左边缘「目录」边签可见  " + (await page.isVisible("#rc-rail-left")));
    await page.screenshot({ path: path.join(OUT, "2-desktop-folded.png") });

    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.evaluate((h) => { location.hash = h; }, `#/reading/book/${BOOK}`);
    await page.waitForSelector(".rc-toc-item");
    await page.waitForTimeout(2200);
    say("桌面 · 刷新后仍收起（localStorage 记忆）  " + (await page.evaluate(() =>
      document.querySelector("#rc-reader").classList.contains("no-left"))));
    await page.screenshot({ path: path.join(OUT, "3-desktop-persisted.png") });

    const para1 = await topPara();
    await page.click("#rc-rail-left");
    await page.waitForTimeout(700);
    say("桌面 · 点边签回开后列宽  " + (await cols()));
    say("桌面 · 回开后视线段仍在  " + (await topPara() === para1));
    await page.screenshot({ path: path.join(OUT, "4-desktop-reopened.png") });

    // 右栏同样能收
    await page.click("#rc-fold-right");
    await page.waitForTimeout(700);
    say("桌面 · 右栏收起后列宽  " + (await cols()));
    await page.screenshot({ path: path.join(OUT, "5-desktop-right-folded.png") });
    await page.click("#rc-rail-right");
    await page.waitForTimeout(500);

    // 键盘快捷键 [ ]
    const box = await page.locator("#rc-article").boundingBox();
    await page.mouse.click(box.x + 40, box.y + 40);
    await page.keyboard.press("[");
    await page.waitForTimeout(600);
    say("桌面 · 按 [ 后 is no-left  " + (await page.evaluate(() =>
      document.querySelector("#rc-reader").classList.contains("no-left"))));
    await page.keyboard.press("[");
    await page.waitForTimeout(600);

    // ── 窄屏 390：抽屉开 / 点章节自动收 / Esc 收 / 点遮罩收 ──
    const m = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    m.on("pageerror", (e) => errors.push("mobile pageerror: " + e.message));
    await openBook(m);
    say("窄屏 · 初始抽屉在屏外  " + (await m.evaluate(() =>
      document.querySelector("#rc-left").getBoundingClientRect().right <= 1)));

    await m.click("#rc-toggle-left");
    await m.waitForTimeout(320);
    say("窄屏 · 抽屉打开后有遮罩  " + (await m.isVisible("#rc-scrim")));
    await m.screenshot({ path: path.join(OUT, "6-mobile-drawer.png") });

    await m.click(".rc-toc-item:nth-of-type(3)");
    await m.waitForTimeout(400);
    say("窄屏 · 点章节后抽屉自动收起  " + (await m.evaluate(() =>
      !document.querySelector("#rc-reader").classList.contains("show-left"))));
    await m.screenshot({ path: path.join(OUT, "7-mobile-after-pick.png") });

    await m.click("#rc-toggle-left");
    await m.waitForTimeout(300);
    await m.keyboard.press("Escape");
    await m.waitForTimeout(300);
    say("窄屏 · Esc 收起抽屉  " + (await m.evaluate(() =>
      !document.querySelector("#rc-reader").classList.contains("show-left"))));

    await m.click("#rc-toggle-left");
    await m.waitForTimeout(300);
    await m.click("#rc-scrim", { position: { x: 355, y: 400 } });
    await m.waitForTimeout(300);
    say("窄屏 · 点遮罩收起抽屉  " + (await m.evaluate(() =>
      !document.querySelector("#rc-reader").classList.contains("show-left"))));

    say("控制台错误  " + (errors.length ? errors.join(" | ") : "无"));
    if (errors.length) process.exitCode = 1;
  } finally {
    await browser.close();
    await writeProgress(original);
    say("已把这本书的阅读进度写回原值");
    fs.writeFileSync(path.join(OUT, "verify.log"), log.join("\n") + "\n", "utf-8");
  }
})().catch((e) => { console.error(e); process.exit(1); });
