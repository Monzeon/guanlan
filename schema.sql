-- ══════════════════════════════════════════════════════════
-- 观澜 GUANLAN · 数据库 Schema (SQLite)
--
-- 设计原则（源自项目章程）：
--   I2  任何结论可溯源 → claims.source 指向 sources
--   I3  双时间戳 → as_of（事实时点）/ observed_at（看到时点）
--   I4  时序只追加 → observations 主键 (code, period, vintage)，永不 UPDATE
--   I5  置信度受层级硬约束 → CHECK tier + 应用层 ceiling 校验
--   I7  T3/T4 不做证据 → CHECK，博主观点走 open_questions
--   I8  falsifier 必填 → CHECK length > 0
-- ══════════════════════════════════════════════════════════

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── L0 来源层 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- S-2026-0001
    title       TEXT NOT NULL,
    publisher   TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    tier        TEXT NOT NULL CHECK (tier IN ('T0','T1','T2','T3','T4')),
    published   TEXT DEFAULT '',               -- 来源发布日
    kind        TEXT DEFAULT 'web',            -- web | pdf | report | book | data
    notes       TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ── L1 正典层：Claim ─────────────────────────────────
CREATE TABLE IF NOT EXISTS claims (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,         -- C-2026-0001
    kind         TEXT NOT NULL DEFAULT 'claim' CHECK (kind IN ('claim','question')),
    statement    TEXT NOT NULL,
    domain       TEXT NOT NULL DEFAULT 'economy',   -- economy|population|history|society|culture|world|cross
    entities     TEXT NOT NULL DEFAULT '[]',   -- JSON 数组
    source_id    INTEGER REFERENCES sources(id),
    tier         TEXT NOT NULL CHECK (tier IN ('T0','T1','T2','T3','T4')),
    confidence   REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 0.95),
    as_of        TEXT NOT NULL,                -- 事实时点 YYYY-MM-DD（历史可 YYYY）
    observed_at  TEXT NOT NULL,                -- 录入时点
    falsifier    TEXT NOT NULL CHECK (length(falsifier) > 0),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','draft','superseded','contradicted','retired')),
    single_source INTEGER NOT NULL DEFAULT 1,  -- 1=单一来源
    links        TEXT NOT NULL DEFAULT '{}',   -- {"supports":[code],"contradicts":[],"refines":[]}
    tags         TEXT NOT NULL DEFAULT '[]',
    notes        TEXT DEFAULT '',
    question     TEXT DEFAULT '',              -- kind=question 时的疑问句
    resolution   TEXT DEFAULT '',              -- 裁决标准
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 全文检索（trigram 分词器对中文可用：子串命中）
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    code, statement, notes, tags,
    content='claims', content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, code, statement, notes, tags)
    VALUES (new.id, new.code, new.statement, new.notes, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, code, statement, notes, tags)
    VALUES ('delete', old.id, old.code, old.statement, old.notes, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS claims_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, code, statement, notes, tags)
    VALUES ('delete', old.id, old.code, old.statement, old.notes, old.tags);
    INSERT INTO claims_fts(rowid, code, statement, notes, tags)
    VALUES (new.id, new.code, new.statement, new.notes, new.tags);
END;

-- ── L1 时序层：追加式 vintage ────────────────────────
CREATE TABLE IF NOT EXISTS series (
    code        TEXT PRIMARY KEY,              -- CN.GDP.REAL.YOY
    name        TEXT NOT NULL,
    unit        TEXT DEFAULT '',
    freq        TEXT DEFAULT 'M',              -- M|Q|A
    source      TEXT DEFAULT '',
    definition  TEXT DEFAULT '',               -- 口径说明
    tier        TEXT DEFAULT 'T0'
);

CREATE TABLE IF NOT EXISTS observations (
    code     TEXT NOT NULL REFERENCES series(code),
    period   TEXT NOT NULL,                    -- YYYY | YYYY-MM | YYYY-QN
    value    TEXT NOT NULL,                    -- 原始字符串，绝不加工
    unit     TEXT DEFAULT '',
    vintage  TEXT NOT NULL,                    -- 发布批次日期
    PRIMARY KEY (code, period, vintage)        -- I4：追加式
);

-- ── 口径登记簿 ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS calibers (
    id          INTEGER PRIMARY KEY,
    series_code TEXT NOT NULL,
    changed_at  TEXT NOT NULL,                 -- 变更生效时点
    description TEXT NOT NULL,                 -- 改了什么
    comparable  INTEGER NOT NULL DEFAULT 0,    -- 变更前后是否可比
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── L2 待验证命题（T3 的合法形态）───────────────────
-- 落在 claims 表 kind='question'，status='draft'（open），
-- 裁决后转 claim 或 retired。

-- ── L4 预测层 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,         -- P-2026-0001
    proposition  TEXT NOT NULL,
    probability  REAL NOT NULL CHECK (probability > 0 AND probability < 1),
    criteria     TEXT NOT NULL,                -- 可第三方复核的判定标准
    resolve_by   TEXT NOT NULL,                -- 到期日（无到期日不是预测）
    base_rate    TEXT DEFAULT '',              -- 参考的基准率类 ID
    depends_on   TEXT NOT NULL DEFAULT '[]',   -- JSON [claim codes]
    pre_mortem   TEXT NOT NULL DEFAULT '',     -- 如果错了最可能错在哪
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    outcome      INTEGER,                      -- 1 发生 / 0 未发生
    brier        REAL,                         -- (p-o)^2
    resolved_at  TEXT,
    lesson       TEXT DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── L4 决策日志 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,        -- D-2026-0001
    decision      TEXT NOT NULL,
    date          TEXT NOT NULL,
    stakes        TEXT DEFAULT 'medium',       -- low|medium|high
    reversible    INTEGER NOT NULL DEFAULT 1,
    depends_on    TEXT NOT NULL DEFAULT '[]',  -- JSON [claim codes]
    chosen_because TEXT DEFAULT '',
    outcome       TEXT DEFAULT '',             -- 结果（事后填）
    reviewed_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 基准率库 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS baserates (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,       -- BR-MACRO-001
    name           TEXT NOT NULL,
    question       TEXT NOT NULL,
    reference_class TEXT NOT NULL,
    window         TEXT NOT NULL,              -- 统计窗口
    sample_size    INTEGER,
    base_rate      REAL,                       -- NULL = 待回测（宁可空，不可编）
    low_confidence INTEGER NOT NULL DEFAULT 0,
    note           TEXT DEFAULT ''
);

-- ── AI 摄取草稿 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_drafts (
    id          INTEGER PRIMARY KEY,
    source_text TEXT NOT NULL,                 -- 原文
    payload     TEXT NOT NULL,                 -- AI 输出的 YAML/JSON 原文
    parsed      TEXT NOT NULL DEFAULT '[]',    -- 解析后的 claim 草稿 JSON
    model       TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','discarded')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 事件（时间锚点）──────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,           -- E-2026-0001
    what       TEXT NOT NULL,
    when_at    TEXT NOT NULL,
    domain     TEXT DEFAULT 'economy',
    importance INTEGER NOT NULL DEFAULT 2,     -- 1 低 2 中 3 高
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 索引 ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_claims_domain  ON claims(domain, status);
CREATE INDEX IF NOT EXISTS idx_claims_tier    ON claims(tier);
CREATE INDEX IF NOT EXISTS idx_obs_code       ON observations(code, period);
CREATE INDEX IF NOT EXISTS idx_pred_status    ON predictions(status, resolve_by);
CREATE INDEX IF NOT EXISTS idx_sources_tier   ON sources(tier);
