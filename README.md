# 观澜 · GUANLAN

**个人认知平台——不是信息仓库，是判断方差的降低仪。**

> 观水有术，必观其澜。——《孟子》

四器官：**档案**（确证事实）· **台账**（判断预测）· **推理台**（AI 分析）· **决策日志**（闭环复盘）。
五层数据流：L0 来源 → L1 正典/时序 → 矛盾/检索 → AI 推理 → 预测结算。

## 启动

```bash
python server.py            # 零依赖，Python ≥ 3.10 即可
# 首次启动自动建库；想带示例数据：
python server.py --seed
```

打开 http://127.0.0.1:8790/

## 接入 AI（可选，OpenAI 兼容端点通吃）

```bash
set GL_AI_BASE_URL=https://api.deepseek.com
set GL_AI_MODEL=deepseek-chat
set GL_AI_KEY=sk-...
python server.py
```

未配置 AI 时全部手动功能可用——这个平台不靠 AI 也站得住。

## 不变量（写进了代码和 CHECK 约束，不是文档）

| # | 不变量 | 实现 |
|---|---|---|
| I3 | 双时间戳 as_of / observed_at | 表结构 NOT NULL |
| I4 | 时序只追加（vintage） | 主键 (code, period, vintage) |
| I5 | 置信度 ≤ 层级上限 | validate_claim 硬校验 |
| I6 | AI 提议，人批准 | AI 只写 ai_drafts，收编才进正典 |
| I7 | T3/T4 不做证据 | tier×kind 联合校验 |
| I8 | falsifier 必填 | CHECK + 校验层 |

## AI 三职能

- **摄取**：文章 → claim 草稿（逐条 quote 带原文），人工收编
- **综合**：句句挂 `[C-编号]`，无出处句在服务端被丢弃后才返回
- **反方**：只许用库内证据论证你结论的反面

## 数据在哪

`data/guanlan.db` 单文件 SQLite（WAL 模式）。**复制这一个文件 = 备份整个平台。**
