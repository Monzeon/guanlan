-- ══════════════════════════════════════════════════════════
-- 观澜 GUANLAN · 阅读中心（独立大板块）表结构
--
-- 设计要点：
--   R1 一份阅读资料 = 一条 clips。clips 仍是原件、标签、板块、检索、知识卡的
--      唯一事实源；reading_items 只挂「阅读态」扩展，避免两套内容各写一份。
--   R2 doc_text / chapters 自己持久化：server.init_db() 每次启动会清掉
--      文件件的 clips.content_text，这里留一份结构化正文用于自愈回填，
--      保证 clip_annotations 的字符偏移永远锚得住。
--   R3 所有可回溯对象（标注 / 书签 / 笔记 / 摘录）统一用
--      (kind, id) 记入 reading_links，既能从阅读中心指向知识库，
--      也能从收存/正典反查来源。
--   R4 reading_fts 是外部内容 FTS5 虚表，按 clip_id 重建索引；
--      trigram 分词器对中文子串可用，短词由应用层 LIKE 兜底。
-- ══════════════════════════════════════════════════════════

-- ── 阅读资料（clips 的 1:1 扩展）─────────────────────────
CREATE TABLE IF NOT EXISTS reading_items (
    clip_id       INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'book',      -- book | doc | audio | video | web | text
    format        TEXT NOT NULL DEFAULT '',          -- pdf/epub/txt/md/docx/mp3/mp4/...
    shelf_id      INTEGER,                          -- 书架分类（reading_shelves）
    doc_text      TEXT NOT NULL DEFAULT '',          -- 结构化正文，重启不丢（R2）
    chapters      TEXT NOT NULL DEFAULT '[]',        -- [{id,title,level,start,end,page}]
    char_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'unread'
                  CHECK (status IN ('unread','reading','done','paused')),
    progress      REAL NOT NULL DEFAULT 0,           -- 0–100
    position      REAL NOT NULL DEFAULT 0,           -- 字符偏移或音视频秒数
    position_unit TEXT NOT NULL DEFAULT 'char',      -- char | second
    duration      REAL NOT NULL DEFAULT 0,           -- 音视频时长（秒），由前端回填
    read_seconds  INTEGER NOT NULL DEFAULT 0,
    last_read_at  TEXT DEFAULT '',
    added_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    meta          TEXT NOT NULL DEFAULT '{}'         -- 作者/出版社/语言/封面等
);
CREATE INDEX IF NOT EXISTS idx_ri_shelf   ON reading_items(shelf_id, clip_id);
CREATE INDEX IF NOT EXISTS idx_ri_status  ON reading_items(status, last_read_at);
CREATE INDEX IF NOT EXISTS idx_ri_kind    ON reading_items(kind);

-- ── 书架分类（阅读中心自己的分类树，可映射到收存板块）──
CREATE TABLE IF NOT EXISTS reading_shelves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    parent_id   INTEGER,
    board_id    INTEGER,                            -- 可选：汇入知识库时的默认板块
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 书签（正文按字符偏移，音视频按秒）──────────────────
CREATE TABLE IF NOT EXISTS reading_bookmarks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    offset      REAL NOT NULL DEFAULT 0,
    time_sec    REAL,
    chapter_id  TEXT DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rb_clip ON reading_bookmarks(clip_id, offset);

-- ── 笔记沉淀（Markdown；可带来源标注 / 摘录）────────────
CREATE TABLE IF NOT EXISTS reading_notes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id               INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    title                 TEXT NOT NULL DEFAULT '',
    content_md            TEXT NOT NULL DEFAULT '',
    chapter_id            TEXT DEFAULT '',
    source_annotation_id  INTEGER,
    source_quote          TEXT NOT NULL DEFAULT '',
    kind                  TEXT NOT NULL DEFAULT 'note',   -- note | excerpt | digest | ai
    archived_clip_id      INTEGER,                        -- 已汇入知识库生成的 clip id
    tags                  TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at            TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rn_clip ON reading_notes(clip_id, id);
CREATE INDEX IF NOT EXISTS idx_rn_kind ON reading_notes(kind, id);

-- ── 双向引用：可检索、可回溯的知识网络（R3）────────────
-- src/dst kind: annotation | bookmark | note | excerpt | clip | claim | board | ai_output
-- src/dst id  : 统一字符串（claim 用 C-2026-0001 这类 code）
CREATE TABLE IF NOT EXISTS reading_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_kind    TEXT NOT NULL,
    src_id      TEXT NOT NULL,
    dst_kind    TEXT NOT NULL,
    dst_id      TEXT NOT NULL,
    relation    TEXT NOT NULL DEFAULT 'refs',   -- refs | cites | derived_from | belongs_to | supports
    clip_id     INTEGER,                        -- 归属的阅读资料，便于按书列出关系
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(src_kind, src_id, dst_kind, dst_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_rl_src ON reading_links(src_kind, src_id);
CREATE INDEX IF NOT EXISTS idx_rl_dst ON reading_links(dst_kind, dst_id);
CREATE INDEX IF NOT EXISTS idx_rl_clip ON reading_links(clip_id, id);

-- ── 阅读会话：热力图 / 连续天数 / 时长统计 ──────────────
CREATE TABLE IF NOT EXISTS reading_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,                  -- YYYY-MM-DD
    clip_id     INTEGER,
    seconds     INTEGER NOT NULL DEFAULT 0,
    chars       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rs_day ON reading_sessions(day);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rs_once ON reading_sessions(day, clip_id);

-- ── 正文 + 批注 + 笔记 + 书签 的全文检索索引 ────────────
-- 每行一个可跳转单元：章节正文 / 一条标注 / 一条笔记 / 一个书签。
CREATE VIRTUAL TABLE IF NOT EXISTS reading_fts USING fts5(
    kind      UNINDEXED,       -- chapter | annotation | note | bookmark
    clip_id   UNINDEXED,
    ref_id    UNINDEXED,       -- chapter id 或对象 id
    offset    UNINDEXED,       -- 跳转用字符偏移 / 秒
    title,
    body,
    tokenize='trigram'
);
