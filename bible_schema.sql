-- ══════════════════════════════════════════════════════════
-- 观澜 GUANLAN · 圣经阅读地（独立板块）表结构
--
-- 设计要点：
--   B1 全板块自有一套表，不碰 clips / reading_* 等任何现有表。
--   B2 bible_verses 以 (book, chapter, verse) 为主键，导入用 upsert 覆盖；
--      version 区分「演示选段(demo)」与用户自行导入的译本。
--   B3 进度只有一行（id=1），read_json 存 "book:chapter" 去重集合。
--   B4 标注 / 书签 / 笔记都按 (book, chapter, verse) 定位，建联合索引
--      供按章拉取；不建外键，删除经文时不影响用户笔记。
--   B5 全部 CREATE TABLE / CREATE INDEX IF NOT EXISTS，ensure() 可反复执行。
-- ══════════════════════════════════════════════════════════

-- ── 板块级元数据（是否演示数据 / 当前译本 / 导入时间）────────
CREATE TABLE IF NOT EXISTS bible_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ── 经文正文 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bible_verses (
    book    TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    verse   INTEGER NOT NULL,
    text    TEXT NOT NULL,
    text_en TEXT NOT NULL DEFAULT '',   -- 中英对照译本的第二语言（可空）
    version TEXT NOT NULL DEFAULT 'demo',
    PRIMARY KEY (book, chapter, verse)
);
CREATE INDEX IF NOT EXISTS idx_bv_version ON bible_verses(version);

-- ── 阅读进度（单行）─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bible_progress (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    book       TEXT,
    chapter    INTEGER,
    verse      INTEGER,
    read_json  TEXT DEFAULT '[]',
    updated_at TEXT
);

-- ── 高亮标注（start / end 为节内字符偏移）────────────────────
CREATE TABLE IF NOT EXISTS bible_highlights (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book       TEXT,
    chapter    INTEGER,
    verse      INTEGER,
    start      INTEGER,
    end        INTEGER,
    quote      TEXT,
    color      TEXT,
    note       TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bh_ref ON bible_highlights(book, chapter, verse);

-- ── 书签 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bible_bookmarks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book       TEXT,
    chapter    INTEGER,
    verse      INTEGER,
    label      TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bb_ref ON bible_bookmarks(book, chapter, verse);

-- ── 笔记（Markdown）─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bible_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book       TEXT,
    chapter    INTEGER,
    verse      INTEGER,
    title      TEXT,
    content_md TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bn_ref ON bible_notes(book, chapter, verse);
