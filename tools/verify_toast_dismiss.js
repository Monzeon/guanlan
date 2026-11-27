/* 验证「提示条会自动消失 / 可点掉」——专治右下角「纸张 · 夜间纸」常驻。
 *
 *   node tools/verify_toast_dismiss.js
 *
 * 依赖本机已装好的 Playwright（WorkBuddy 自带），只跑本地 http://127.0.0.1:8790。
 * 不改数据：跑完把阅读进度与 gl_reading_prefs 偏好写回原值。
 */
const path = require("path");
const fs = require("fs");

const PW = process.env.PW_PATH ||
  path.join(process.env.USERPROFILE || "", ".workbuddy/binaries/node/versions/22.22.2-6/node_modules/@playwright/cli/node_modules/playwright");
const { chromium } = require(PW);

const ROOT = path.resolve(__dirname, "..");
const BASE = "http://127.0.0.1:8790/";
const BOOK = Number(process.env.BOOK || 19);
const OUT = path.join(ROOT, "docs", "shots-toast");
const LOGFILE = path.join(ROOT, "docs", "阅读中心-toast常驻修复.verify.log");
const log = [];
const say = (s) => { log.push(s); console.log(s); };
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
  say(`观澜 · 提示条生命周期验证  ${new Date().toISOString()}`);
  say(`书目 clip_id=${BOOK} · 原进度 position=${original.position} progress=${original.progress}`);

  const browser = await chromium.launch({ headless: true });
  const pageErrors = [];
  let prefsBefore = null;
  let page = null;

  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", (e) => pageErrors.push(e.message));

    await page.goto(BASE, { waitUntil: "networkidle" });
    prefsBefore = await page.evaluate(() => localStorage.getItem("gl_reading_prefs"));
    await page.evaluate((h) => { location.hash = h; }, `#/reading/book/${BOOK}`);
    await page.waitForSelector("#rc-paper-surface", { timeout: 15000 });
    await page.waitForTimeout(700);

    const clearToasts = () => page.evaluate(() =>
      document.querySelectorAll("#toast-host .toast").forEach((el) => el.remove()));
    const live = () => page.evaluate(() =>
      [...document.querySelectorAll("#toast-host .toast")]
        .filter((el) => !el.classList.contains("out")).length);
    const paper = () => page.evaluate(() =>
      (document.querySelector("#rc-paper-surface") || {}).dataset?.paper || null);

    await clearToasts();                  // 把加载期的其它提示清干净，只测本次产生的
    check("起始无残留提示", (await live()) === 0, `${await live()} 条`);
    const p0 = await paper();

    // 两次切换间隔 > 800ms，绕过「同类复用」，确保真的产生两条独立提示
    await page.click("#rc-paper-btn");
    await page.waitForTimeout(120);
    const n1 = await live();
    check("第一次切纸出现 1 条", n1 === 1, `${n1} 条`);
    const p1 = await paper();

    await page.waitForTimeout(1000);
    await page.click("#rc-paper-btn");
    await page.waitForTimeout(120);
    const n2 = await live();
    check("间隔 1s 再切，出现 2 条独立提示", n2 === 2, `${n2} 条`);
    const p2 = await paper();
    check("切纸按钮本身仍生效", p1 !== p0 && p2 !== p1, `${p0} → ${p1} → ${p2}`);
    await page.screenshot({ path: path.join(OUT, "1-two-toasts.png") });

    // 短停留 1600ms + 170ms 退场，给足 2500ms：两条都应各自被自己的定时器收走
    await page.waitForTimeout(2500);
    const n3 = await live();
    check("两条提示均自动消失（无僵尸常驻）", n3 === 0, `${n3} 条`);
    await page.screenshot({ path: path.join(OUT, "2-all-dismissed.png") });

    // 手动兜底：点一下就关
    await page.click("#rc-paper-btn");
    await page.waitForTimeout(200);
    const n4 = await live();
    check("再次切纸出现 1 条", n4 === 1, `${n4} 条`);
    await page.click("#toast-host .toast", { timeout: 3000 });
    await page.waitForTimeout(300);
    const n5 = await live();
    check("点击提示立即关闭", n5 === 0, `${n5} 条`);

    check("无未捕获运行时异常", pageErrors.length === 0, pageErrors.join(" | ") || "0 个");
  } finally {
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
