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

-- ── 集藏层：轻量信息剪藏（标签即分类， folksonomy）──
-- 集藏（raw 进料）→ 待验证（半结构）→ 正典（严谨台账）
CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    url          TEXT DEFAULT '',
    channel      TEXT DEFAULT '',               -- 渠道：公众号/视频/播客/书/文章/朋友/… 自由文本
    board_id     INTEGER,                       -- 板块（库的抽屉），NULL = 未分类
    tier         TEXT DEFAULT '',               -- 信源分级 T0–T4，'' = 未分级
    grade        TEXT NOT NULL DEFAULT '',      -- 材料级：canon 权威 / ref 参考 / daily 资讯 / '' 未定
    grade_hint   TEXT DEFAULT '',               -- AI 建议的材料级，只作建议，不写进 grade
    grade_hint_reason TEXT DEFAULT '',          -- 建议的理由，一句话
    tags         TEXT NOT NULL DEFAULT '[]',    -- JSON 数组，自由标签
    excerpt      TEXT DEFAULT '',               -- 摘录 / 笔记
    summary      TEXT DEFAULT '',               -- 可编辑的 AI 摘要
    content_text TEXT DEFAULT '',               -- 已解析正文，仅供本机检索与按需 AI 加工
    body_images  TEXT DEFAULT '',               -- 正文配图 JSON：[{f,n,c,at,w,h}]，at=插在第几段之后
    fetch_status TEXT NOT NULL DEFAULT 'idle',  -- idle | ok | failed | stored | unparsed
    fetch_error  TEXT DEFAULT '',
    archived     INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT DEFAULT '',               -- URL 规范化或文件内容去重
    source_type  TEXT NOT NULL DEFAULT 'web',   -- web | text | file
    file_name    TEXT DEFAULT '',
    file_path    TEXT DEFAULT '',               -- 仅存 data/uploads 内的随机文件名
    mime_type    TEXT DEFAULT '',
    file_size    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    viewed_at    TEXT DEFAULT '',               -- 最近一次打开的时间，用来算“该重看”
    updated_at   TEXT DEFAULT ''
);

-- 我的判断：一条材料对应一句结论，轻量版正典
CREATE TABLE IF NOT EXISTS verdicts (
    clip_id    INTEGER PRIMARY KEY,
    text       TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'mid',     -- low | mid | high
    refs       TEXT NOT NULL DEFAULT '[]',      -- [{"id": 12, "kind": "counter"}]
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT ''
);

-- 保存下来的检索
CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    q          TEXT NOT NULL DEFAULT '',
    filter     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 随手记：每条收存下面自己写的东西，可以带图
CREATE TABLE IF NOT EXISTS clip_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id    INTEGER NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    images     TEXT NOT NULL DEFAULT '[]',      -- JSON 数组，值只存 data/notes 里的随机文件名
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT ''
);

-- 主收存图：一条收存可以直接带多张图，和随手记附件分开记，卡片上直接显示。
CREATE TABLE IF NOT EXISTS clip_images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id    INTEGER NOT NULL,
    file       TEXT NOT NULL,                   -- data/notes 内的随机文件名
    name       TEXT DEFAULT '',                 -- 上传时的原始文件名
    size       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(clip_id, file)
);

-- 正文批注：落在已抓正文的字符区间上，quote 与前后文用于正文重抓后的重新锚定。
CREATE TABLE IF NOT EXISTS clip_annotations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    start_offset INTEGER NOT NULL DEFAULT 0,
    end_offset   INTEGER NOT NULL DEFAULT 0,
    quote        TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL DEFAULT 'amber',  -- amber | sage | blue | rose
    prefix       TEXT NOT NULL DEFAULT '',
    suffix       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
    title, excerpt, tags,
    content='clips', content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS clips_ai AFTER INSERT ON clips BEGIN
    INSERT INTO clips_fts(rowid, title, excerpt, tags)
    VALUES (new.id, new.title, new.excerpt, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS clips_ad AFTER DELETE ON clips BEGIN
    INSERT INTO clips_fts(clips_fts, rowid, title, excerpt, tags)
    VALUES ('delete', old.id, old.title, old.excerpt, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS clips_au AFTER UPDATE ON clips BEGIN
    INSERT INTO clips_fts(clips_fts, rowid, title, excerpt, tags)
    VALUES ('delete', old.id, old.title, old.excerpt, old.tags);
    INSERT INTO clips_fts(rowid, title, excerpt, tags)
    VALUES (new.id, new.title, new.excerpt, new.tags);
END;

-- AI 研究记录：摘要以外的分析、对比、问答均持久化，可继续编辑。
CREATE TABLE IF NOT EXISTS ai_outputs (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER REFERENCES clips(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                  -- analyze | compare | ask | devil
    title       TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '{}',     -- JSON，结构化结果或可编辑回答
    source_ids  TEXT NOT NULL DEFAULT '[]',     -- 关联的 clip id 或 claim code
    model       TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 编号序列：写入计数器时串行化，避免多线程同时生成相同 code。
CREATE TABLE IF NOT EXISTS code_counters (
    prefix TEXT NOT NULL,
    year   INTEGER NOT NULL,
    seq    INTEGER NOT NULL,
    PRIMARY KEY (prefix, year)
);

-- ── 板块：库的抽屉。一条剪藏最多属于一个板块，标签仍是细分类 ──
CREATE TABLE IF NOT EXISTS boards (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL DEFAULT '',        -- '' 普通板块 | 'archive' 资料库（原件与权威原文）
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 索引 ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_claims_domain  ON claims(domain, status);
CREATE INDEX IF NOT EXISTS idx_claims_tier    ON claims(tier);
CREATE INDEX IF NOT EXISTS idx_obs_code       ON observations(code, period);
CREATE INDEX IF NOT EXISTS idx_pred_status    ON predictions(status, resolve_by);
CREATE INDEX IF NOT EXISTS idx_sources_tier   ON sources(tier);
CREATE INDEX IF NOT EXISTS idx_clips_channel  ON clips(channel);
CREATE INDEX IF NOT EXISTS idx_clips_archived ON clips(archived, id);
CREATE INDEX IF NOT EXISTS idx_clips_url      ON clips(url);
CREATE INDEX IF NOT EXISTS idx_clips_hash     ON clips(content_hash);
CREATE INDEX IF NOT EXISTS idx_clips_board    ON clips(board_id, id);
CREATE INDEX IF NOT EXISTS idx_clips_tier     ON clips(tier);
CREATE INDEX IF NOT EXISTS idx_clips_grade    ON clips(grade, id);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_clip ON ai_outputs(clip_id, id);
CREATE INDEX IF NOT EXISTS idx_clip_notes      ON clip_notes(clip_id, id);
CREATE INDEX IF NOT EXISTS idx_clip_images     ON clip_images(clip_id, id);
CREATE INDEX IF NOT EXISTS idx_clip_annotations ON clip_annotations(clip_id, start_offset, id);
CREATE INDEX IF NOT EXISTS idx_clips_viewed      ON clips(viewed_at);
