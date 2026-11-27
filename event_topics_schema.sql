-- 观澜 · 桌面端事件专题
-- 独立于旧 events 时间锚点；clips 仍是资料的唯一事实源。
CREATE TABLE IF NOT EXISTS event_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','paused','archived')),
    current_understanding TEXT NOT NULL DEFAULT '',
    open_questions TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    archived_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_event_topics_status
    ON event_topics(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS event_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES event_topics(id) ON DELETE CASCADE,
    happened_at TEXT NOT NULL,
    body TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_event_updates_topic
    ON event_updates(topic_id, happened_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS event_clip_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES event_topics(id) ON DELETE CASCADE,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(topic_id, clip_id)
);
CREATE INDEX IF NOT EXISTS idx_event_clip_links_clip ON event_clip_links(clip_id, topic_id);
CREATE INDEX IF NOT EXISTS idx_event_clip_links_topic ON event_clip_links(topic_id, id DESC);
