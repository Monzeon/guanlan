#!/usr/bin/env python
"""观澜 · 桌面端事件专题。

独立模块：不 import server；由 server.install 注入路由和参数校验。
旧 events 时间锚点保持不变；资料原文仍只存在 clips。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import app_paths

SCHEMA_FILE = app_paths.RES_DIR / "event_topics_schema.sql"
_CTX: dict = {}
_READY = False
STATUSES = {"active", "paused", "archived"}
GET_ROUTES = {}
POST_ROUTES = {}


def _get(path):
    def deco(fn):
        GET_ROUTES[path] = fn
        return fn
    return deco


def _post(path):
    def deco(fn):
        POST_ROUTES[path] = fn
        return fn
    return deco


def install(route_get, route_post, **ctx) -> None:
    global _CTX
    _CTX = ctx
    for path, fn in GET_ROUTES.items():
        route_get(path)(fn)
    for path, fn in POST_ROUTES.items():
        route_post(path)(fn)


def ensure(conn) -> None:
    """幂等追加新表。绝不修改或迁移旧 events 与 clips。"""
    global _READY
    if _READY:
        return
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _READY = True


def purge_clip(conn, cid: int) -> None:
    """显式清理关联；外键 ON DELETE CASCADE 是第二道保护。"""
    ensure(conn)
    conn.execute("DELETE FROM event_clip_links WHERE clip_id=?", (cid,))


def _err(message: str) -> dict:
    return {"error": message}


def _id(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        exc = _CTX.get("ValidationError", ValueError)
        raise exc("编号必须是正整数")
    return value


def _text(value, field: str, limit: int, required: bool = False) -> str:
    value = str(value or "").strip()
    if required and not value:
        raise _CTX.get("ValidationError", ValueError)(f"{field}不能为空")
    if len(value) > limit:
        raise _CTX.get("ValidationError", ValueError)(f"{field}最多 {limit} 字")
    return value


def _tags(value) -> str:
    if isinstance(value, str):
        value = re.split(r"[,，;；]", value)
    if value is None:
        value = []
    if not isinstance(value, list):
        raise _CTX.get("ValidationError", ValueError)("标签格式不正确")
    result = []
    for item in value:
        tag = str(item or "").strip()[:24]
        if tag and tag not in result:
            result.append(tag)
        if len(result) == 12:
            break
    return json.dumps(result, ensure_ascii=False)


def _like(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def _touch(conn, topic_id: int) -> None:
    conn.execute("UPDATE event_topics SET updated_at=datetime('now','localtime') WHERE id=?",
                 (topic_id,))


def _topic(conn, topic_id: int):
    return conn.execute("SELECT * FROM event_topics WHERE id=?", (topic_id,)).fetchone()


def _topic_dict(row, latest: str = "") -> dict:
    data = dict(row)
    data["tags"] = json.loads(data.get("tags") or "[]")
    data["latest_update"] = latest
    return data


def _detail(conn, topic_id: int) -> dict:
    topic = _topic(conn, topic_id)
    if not topic:
        return _err("事件不存在")
    updates = conn.execute(
        "SELECT * FROM event_updates WHERE topic_id=? ORDER BY happened_at DESC,id DESC",
        (topic_id,)).fetchall()
    links = conn.execute(
        """SELECT l.id,l.topic_id,l.clip_id,l.note,l.created_at,l.updated_at,
                  c.title,c.source_type,c.file_name,c.url,c.created_at AS clip_created_at
           FROM event_clip_links l JOIN clips c ON c.id=l.clip_id
           WHERE l.topic_id=? ORDER BY l.id DESC""", (topic_id,)).fetchall()
    return {"topic": _topic_dict(topic), "updates": [dict(r) for r in updates],
            "links": [dict(r) for r in links]}


@_get("/api/event-topics")
def api_list(conn, qs):
    ensure(conn)
    status = (qs.get("status") or ["active"])[0]
    query = (qs.get("q") or [""])[0].strip()[:100]
    where, params = [], []
    if status != "all":
        if status not in STATUSES:
            return _err("事件状态不正确")
        where.append("t.status=?")
        params.append(status)
    if query:
        pattern = _like(query)
        where.append("(t.title LIKE ? ESCAPE '\\' OR t.purpose LIKE ? ESCAPE '\\'"
                     " OR t.tags LIKE ? ESCAPE '\\')")
        params.extend([pattern] * 3)
    sql = """SELECT t.*,
               (SELECT COUNT(*) FROM event_updates u WHERE u.topic_id=t.id) AS update_count,
               (SELECT COUNT(*) FROM event_clip_links l WHERE l.topic_id=t.id) AS link_count,
               COALESCE((SELECT u.body FROM event_updates u WHERE u.topic_id=t.id
                         ORDER BY u.happened_at DESC,u.id DESC LIMIT 1),'') AS latest_update
             FROM event_topics t"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.updated_at DESC,t.id DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status,COUNT(*) n FROM event_topics GROUP BY status")}
    return {"topics": [_topic_dict(r, r["latest_update"]) for r in rows],
            "counts": {key: counts.get(key, 0) for key in sorted(STATUSES)},
            "total": sum(counts.values())}


@_get("/api/event-topics/{tid}")
def api_one(conn, qs, tid):
    ensure(conn)
    return _detail(conn, _id(tid))


@_get("/api/event-topics/clips/{cid}")
def api_for_clip(conn, qs, cid):
    ensure(conn)
    clip_id = _id(cid)
    rows = conn.execute(
        """SELECT t.id,t.title,t.status,l.id AS link_id,l.note
           FROM event_clip_links l JOIN event_topics t ON t.id=l.topic_id
           WHERE l.clip_id=? ORDER BY t.status='archived',t.updated_at DESC""", (clip_id,)).fetchall()
    return {"links": [dict(r) for r in rows]}


@_get("/api/event-topics/clip-search")
def api_clip_search(conn, qs):
    """关联资料时只读标题等轻字段，不加载正文、图片和附件。"""
    ensure(conn)
    query = (qs.get("q") or [""])[0].strip()[:100]
    if not query:
        return {"clips": []}
    pattern = _like(query)
    rows = conn.execute(
        """SELECT id,title,source_type,file_name,url,created_at FROM clips
           WHERE title LIKE ? ESCAPE '\\' OR file_name LIKE ? ESCAPE '\\'
           ORDER BY updated_at DESC,id DESC LIMIT 20""", (pattern, pattern)).fetchall()
    return {"clips": [dict(r) for r in rows]}


@_get("/api/event-topics/export")
def api_export(conn, qs):
    ensure(conn)
    topics = conn.execute("SELECT * FROM event_topics ORDER BY id").fetchall()
    updates = conn.execute("SELECT * FROM event_updates ORDER BY topic_id,id").fetchall()
    links = conn.execute("SELECT * FROM event_clip_links ORDER BY topic_id,id").fetchall()
    return {"version": 1, "exported_at": datetime.now().isoformat(timespec="seconds"),
            "topics": [_topic_dict(r) for r in topics],
            "updates": [dict(r) for r in updates], "links": [dict(r) for r in links]}


@_post("/api/event-topics")
def api_create(conn, qs, body):
    ensure(conn)
    title = _text(body.get("title"), "事件名称", 120, True)
    cur = conn.execute(
        """INSERT INTO event_topics(title,purpose,tags,status,current_understanding,open_questions)
           VALUES (?,?,?,'active',?,?)""",
        (title, _text(body.get("purpose"), "跟踪目的", 2000),
         _tags(body.get("tags")),
         _text(body.get("current_understanding"), "当前认识", 12000),
         _text(body.get("open_questions"), "待解问题", 12000)))
    return {"ok": True, "id": cur.lastrowid}


@_post("/api/event-topics/{tid}")
def api_update(conn, qs, body, tid):
    ensure(conn)
    topic_id = _id(tid)
    row = _topic(conn, topic_id)
    if not row:
        return _err("事件不存在")
    allowed = {"title": (120, True), "purpose": (2000, False),
               "current_understanding": (12000, False), "open_questions": (12000, False),
               "summary": (20000, False)}
    sets, params = [], []
    for field, (limit, required) in allowed.items():
        if field in body:
            sets.append(field + "=?")
            params.append(_text(body[field], field, limit, required))
    if "tags" in body:
        sets.append("tags=?")
        params.append(_tags(body["tags"]))
    if "status" in body:
        status = str(body["status"])
        if status not in STATUSES:
            return _err("事件状态不正确")
        sets += ["status=?", "archived_at=?"]
        params += [status, datetime.now().isoformat(timespec="seconds")
                   if status == "archived" else ""]
    if not sets:
        return _err("没有可保存的修改")
    params.append(topic_id)
    conn.execute("UPDATE event_topics SET " + ",".join(sets)
                 + ",updated_at=datetime('now','localtime') WHERE id=?", params)
    return {"ok": True}


@_post("/api/event-topics/{tid}/delete")
def api_delete(conn, qs, body, tid):
    ensure(conn)
    topic_id = _id(tid)
    if not _topic(conn, topic_id):
        return _err("事件不存在")
    conn.execute("DELETE FROM event_topics WHERE id=?", (topic_id,))
    return {"ok": True}


def _happened_at(value) -> str:
    text = _text(value, "发生时间", 30, True)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            datetime.strptime(text, "%Y-%m-%d")
            return text
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.isoformat(timespec="minutes")
    except ValueError:
        raise _CTX.get("ValidationError", ValueError)("发生时间格式不正确") from None


@_post("/api/event-topics/{tid}/updates")
def api_update_create(conn, qs, body, tid):
    ensure(conn)
    topic_id = _id(tid)
    if not _topic(conn, topic_id):
        return _err("事件不存在")
    cur = conn.execute(
        "INSERT INTO event_updates(topic_id,happened_at,body) VALUES (?,?,?)",
        (topic_id, _happened_at(body.get("happened_at")),
         _text(body.get("body"), "进展内容", 12000, True)))
    _touch(conn, topic_id)
    return {"ok": True, "id": cur.lastrowid}


@_post("/api/event-topics/updates/{uid}")
def api_update_edit(conn, qs, body, uid):
    ensure(conn)
    update_id = _id(uid)
    row = conn.execute("SELECT topic_id FROM event_updates WHERE id=?", (update_id,)).fetchone()
    if not row:
        return _err("进展不存在")
    topic_id = row["topic_id"]
    row = conn.execute("SELECT * FROM event_updates WHERE id=? AND topic_id=?",
                       (update_id, topic_id)).fetchone()
    if not row:
        return _err("进展不存在")
    sets, params = [], []
    if "happened_at" in body:
        sets.append("happened_at=?")
        params.append(_happened_at(body["happened_at"]))
    if "body" in body:
        sets.append("body=?")
        params.append(_text(body["body"], "进展内容", 12000, True))
    if not sets:
        return _err("没有可保存的修改")
    params.extend([update_id, topic_id])
    conn.execute("UPDATE event_updates SET " + ",".join(sets)
                 + ",updated_at=datetime('now','localtime') WHERE id=? AND topic_id=?", params)
    _touch(conn, topic_id)
    return {"ok": True}


@_post("/api/event-topics/updates/{uid}/delete")
def api_update_delete(conn, qs, body, uid):
    ensure(conn)
    update_id = _id(uid)
    row = conn.execute("SELECT topic_id FROM event_updates WHERE id=?", (update_id,)).fetchone()
    if not row:
        return {"ok": True}
    topic_id = row["topic_id"]
    conn.execute("DELETE FROM event_updates WHERE id=?", (update_id,))
    _touch(conn, topic_id)
    return {"ok": True}


@_post("/api/event-topics/{tid}/links")
def api_link_create(conn, qs, body, tid):
    ensure(conn)
    topic_id, clip_id = _id(tid), _id(body.get("clip_id"))
    if not _topic(conn, topic_id):
        return _err("事件不存在")
    if not conn.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone():
        return _err("收存资料不存在")
    note = _text(body.get("note"), "关联说明", 2000)
    conn.execute(
        """INSERT INTO event_clip_links(topic_id,clip_id,note) VALUES (?,?,?)
           ON CONFLICT(topic_id,clip_id) DO UPDATE SET note=excluded.note,
           updated_at=datetime('now','localtime')""", (topic_id, clip_id, note))
    _touch(conn, topic_id)
    return {"ok": True}


@_post("/api/event-topics/links/{lid}/delete")
def api_link_delete(conn, qs, body, lid):
    ensure(conn)
    link_id = _id(lid)
    row = conn.execute("SELECT topic_id FROM event_clip_links WHERE id=?", (link_id,)).fetchone()
    if not row:
        return {"ok": True}
    topic_id = row["topic_id"]
    conn.execute("DELETE FROM event_clip_links WHERE id=?", (link_id,))
    _touch(conn, topic_id)
    return {"ok": True}
