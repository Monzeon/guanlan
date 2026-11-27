# 阅读中心 · 提示条常驻不消失修复

记录时间：2026-11-09
现象：右下角「纸张 · 夜间纸」「纸张 · 白纸」两条深色提示一直挂着，点也点不掉。

## 一、根因：全模块共用一个销毁定时器

`static/app.js` 的 `toast()` 用 `toastState.timer` 这一个全局变量保存「4.2 秒后移除」的定时器：

```js
toastState.timer = setTimeout(() => dismissToast(item), 4200);
```

每次新提示都会先 `clearTimeout(toastState.timer)`。于是：

| 时刻 | 动作 | 结果 |
| --- | --- | --- |
| t0 | 第一条 toast（夜间纸） | 定时器 T1 排上，4200ms 后移除第一条 |
| t1 > 800ms | 第二条 toast（白纸） | `clearTimeout` 把 T1 清掉，只给第二条排 T2 |
| t1 + 4200ms | T2 触发 | 只移除第二条 |

第一条的移除定时器被后一条顺手清掉，从此永久留在 DOM 里。800ms 内的同类复用分支同样会 `clearTimeout`，所以连点也会留僵尸。
再叠加 `static/style.css:596` 的 `#toast-host { pointer-events: none; }` —— 提示条整块不吃鼠标事件，用户连手动关掉的入口都没有，于是「一直在这，不消除」。

上一轮给阅读器加的切纸反馈属于连续两条同类提示，把这个既有缺陷稳定地暴露了出来。

## 二、改动清单

| 文件 | 改动 |
| --- | --- |
| `static/app.js` | 定时器改为**每条自带**：`toastTimers` WeakMap + `armToast(item, ms)`；`dismissToast()` 清自己的定时器并防重入；`toast()` 增加可选第 4 参数 `ms`（默认 4200，旧调用方零改动）；复用判定排除正在退场的条；新建条加「点击关闭」 |
| `static/style.css` | `.toast` 加 `pointer-events: auto; cursor: pointer;`，在 `none` 的宿主上放开命中，点一下即关 |
| `static/reading.js` | 工具栏三处反馈（切纸、字号上下限）改用 1600ms 短停留，即时状态不再占着右下角 |
| `android-local/app/src/main/python/static/{app.js,reading.js,style.css}` | 同一份修复同步到 Android 内嵌静态资源 |

三对文件的变更集逐行比对完全一致（25 / 7 / 1 行增删），`node --check` 全部通过，`toastState.timer` 已无残留。
备份：`*.bak-20261109-toast`（六份）。

## 三、验证记录

命令：`node tools/verify_toast_dismiss.js`
环境：Playwright / Chromium headless，1440×900，真实走 `http://127.0.0.1:8790/` → `#/reading/book/19`。

```
PASS  起始无残留提示 · 0 条
PASS  第一次切纸出现 1 条 · 1 条
PASS  间隔 1s 再切，出现 2 条独立提示 · 2 条
PASS  切纸按钮本身仍生效 · paper → sepia → night
PASS  两条提示均自动消失（无僵尸常驻） · 0 条
PASS  再次切纸出现 1 条 · 1 条
PASS  点击提示立即关闭 · 0 条
PASS  无未捕获运行时异常 · 0 个
──────── 结果：全部通过 ────────
```

- 完整日志：`docs/阅读中心-toast常驻修复.verify.log`
- 截图：`docs/shots-toast/1-two-toasts.png`（两条共存）、`2-all-dismissed.png`（自动清空后）
- 关键时序：两次切换间隔 1000ms（> 800ms 复用窗口，确保产生两条独立提示），随后等待 2500ms，两条都被各自的定时器收走。
- 回归：`node tools/verify_reader_toolbar.js` 重跑 18 项断言全部通过，A− / A＋ / ◐ 功能与纸面换色未受影响。
- 验证前后把阅读进度与 `gl_reading_prefs` 写回原值。

## 四、生效方式

服务按请求读磁盘 `static/`，改动已即时生效。已经开着的页面按 **Ctrl+F5** 强制刷新一次：
旧脚本在那次页面会话里生成的僵尸提示随 DOM 重建一起清掉，之后每条提示 1.6 秒自动收起，也可以直接点掉。

## 五、回滚

```bash
python tools/rollback_toast_sticky.py --dry-run    # 预览
python tools/rollback_toast_sticky.py --apply      # 写盘还原六份文件
python tools/rollback_toast_sticky.py --apply --keep-docs   # 保留验证产物
```

脚本用「改动后特征串」确认文件确属本次版本，对不上就跳过，避免覆盖之后的手改；备份保留原地。

## 六、四件交付索引

| 交付 | 路径 |
| --- | --- |
| 改后文件 | `static/{app.js,reading.js,style.css}` 与 `android-local/app/src/main/python/static/` 下同名三份 |
| 对照差分 | `docs/阅读中心-toast常驻修复.patch`（六段 unified diff） |
| 验证记录 | `docs/阅读中心-toast常驻修复.md`（本文件第三节）、`…verify.log`、`docs/shots-toast/`、`tools/verify_toast_dismiss.js` |
| 回滚脚本 | `tools/rollback_toast_sticky.py` |
