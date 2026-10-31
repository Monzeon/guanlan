#!/usr/bin/env python
"""
观澜 GUANLAN · 个人认知平台
单文件后端：标准库 HTTP 服务 + SQLite + AI 接入。零第三方依赖。

    python server.py                 启动（默认 127.0.0.1:8790）
    python server.py --port 9000     指定端口
    set GL_AI_BASE_URL=...           OpenAI 兼容端点（可选）
    set GL_AI_MODEL=deepseek-chat    模型名（可选）
    set GL_AI_KEY=sk-...             API Key（可选）

AI 三职能（章程：AI 提议，人批准）：
    A 摄取   文章 → claim 草稿（落 ai_drafts，人工收编进正典）
    B 综合   问题 → 检索 + 句句挂 [C-code]，无引用句自动丢弃
    C 反方   给一条 claim，AI 只用库内证据论证反面

数据流不变量：
    I3 双时间戳 as_of / observed_at    I4 时序只追加（vintage）
    I5 置信度 ≤ 层级上限               I6 AI 永不直接写正典
    I7 T3/T4 不做证据                  I8 falsifier 必填
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "guanlan.db"
STATIC = ROOT / "static"
SCHEMA = ROOT / "schema.sql"

TIER_CEILING = {"T0": 0.95, "T1": 0.85, "T2": 0.70, "T3": 0.50, "T4": 0.25}
DOMAINS = {
    "economy": "经济", "population": "人口", "history": "历史",
    "society": "社会", "culture": "文化", "world": "世界", "cross": "跨域",
}
SOURCE_KINDS = {"web", "pdf", "report", "book", "data"}

# ── 数据库 ───────────────────────────────────────────

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def next_code(conn: sqlite3.Connection, prefix: str, table: str) -> str:
    """生成 C-2026-0001 式编号，永不复用。"""
    year = __import__("datetime").date.today().year
    row = conn.execute(
        f"SELECT code FROM {table} WHERE code LIKE ? ORDER BY code DESC LIMIT 1",
        (f"{prefix}-{year}-%",),
    ).fetchone()
    seq = int(row["code"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{year}-{seq:04d}"


def row_to_claim(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["entities"] = json.loads(d["entities"] or "[]")
    d["links"] = json.loads(d["links"] or "{}")
    d["tags"] = json.loads(d["tags"] or "[]")
    d["ceiling"] = TIER_CEILING.get(d["tier"], 0.25)
    d["domain_label"] = DOMAINS.get(d["domain"], d["domain"])
    return d


# ── 校验 ─────────────────────────────────────────────

class ValidationError(ValueError):
    pass


def validate_claim(data: dict, conn: sqlite3.Connection) -> dict:
    st = str(data.get("statement") or "").strip()
    if len(st) < 6:
        raise ValidationError("statement 至少 6 个字，且必须可判定真伪（禁止『可能/或许/据说』）")
    if re.search(r"可能|或许|据说|大概", st):
        raise ValidationError("statement 含模糊词（可能/或许/据说/大概）。这是观点，请改为待验证命题（kind=question）")

    tier = data.get("tier")
    if tier not in TIER_CEILING:
        raise ValidationError(f"tier 必须是 {'/'.join(TIER_CEILING)}")

    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ValidationError("confidence 必须是 0-1 的数字")
    ceiling = TIER_CEILING[tier]
    if conf > ceiling + 1e-9:
        raise ValidationError(f"I5 违规：{tier} 的置信度上限是 {ceiling}，给了 {conf}")

    kind = data.get("kind", "claim")
    if kind == "claim" and tier in ("T3", "T4"):
        raise ValidationError("I7 违规：T3/T4 不能作为证据 claim。博主观点应录为待验证命题（kind=question, question=...）")

    as_of = str(data.get("as_of") or "").strip()
    observed = str(data.get("observed_at") or "").strip()
    if not re.match(r"^\d{4}(-\d{2}-\d{2})?$", as_of):
        raise ValidationError("as_of 必填，格式 YYYY-MM-DD（历史可用 YYYY）")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", observed):
        raise ValidationError("observed_at 必填，格式 YYYY-MM-DD")

    falsifier = str(data.get("falsifier") or "").strip()
    if len(falsifier) < 4:
        raise ValidationError("I8 违规：falsifier 必填——写不出证伪条件的不是 claim，是态度")

    source_code = str(data.get("source_code") or "").strip()
    source_id = None
    if source_code:
        row = conn.execute("SELECT id FROM sources WHERE code=?", (source_code,)).fetchone()
        if not row:
            raise ValidationError(f"来源 {source_code} 不存在，先在来源页建档")
        source_id = row["id"]

    return {
        "kind": kind,
        "statement": st,
        "question": str(data.get("question") or "").strip(),
        "resolution": str(data.get("resolution") or "").strip(),
        "domain": data.get("domain") if data.get("domain") in DOMAINS else "economy",
        "entities": json.dumps(data.get("entities") or [], ensure_ascii=False),
        "source_id": source_id,
        "tier": tier,
        "confidence": conf,
        "as_of": as_of,
        "observed_at": observed,
        "falsifier": falsifier,
        "status": "draft" if kind == "question" else "active",
        "links": json.dumps(data.get("links") or {}, ensure_ascii=False),
        "tags": json.dumps(data.get("tags") or [], ensure_ascii=False),
        "notes": str(data.get("notes") or ""),
    }


# ── 矛盾检测（结构化规则，非语义）────────────────────

def detect_conflicts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT * FROM claims WHERE status='active' AND kind='claim'").fetchall()
    claims = [row_to_claim(r) for r in rows]

    conflicts, inverted = [], []
    num_re = re.compile(r"\d+(?:\.\d+)?")

    def numbers(text):
        out = set()
        for tok in num_re.findall(text or ""):
            v = float(tok)
            if not (v.is_integer() and 1900 <= v <= 2100):
                out.add(f"{v:g}")
        return out

    def ents(c):
        return {e.strip() for e in c["entities"] if len(e.strip()) >= 2}

    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            shared = ents(a) & ents(b)
            ya, yb = a["as_of"][:4], b["as_of"][:4]
            if not shared or ya != yb:
                continue
            va, vb = numbers(a["statement"]), numbers(b["statement"])
            if va and vb and not (va & vb):
                conflicts.append({
                    "a": a["code"], "b": b["code"],
                    "reason": f"同实体（{'、'.join(sorted(shared))}）同年（{ya}）数值不同，需人工裁决",
                    "a_conf": a["confidence"], "b_conf": b["confidence"],
                    "a_tier": a["tier"], "b_tier": b["tier"],
                })

    order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if not (ents(a) & ents(b)) or a["as_of"][:4] != b["as_of"][:4]:
                continue
            oa, ob = order[a["tier"]], order[b["tier"]]
            if oa != ob and abs(a["confidence"] - b["confidence"]) > 0.05:
                lo, hi = (a, b) if oa > ob else (b, a)
                if lo["confidence"] > hi["confidence"]:
                    inverted.append({
                        "a": lo["code"], "b": hi["code"],
                        "reason": f"{lo['tier']} 置信度 {lo['confidence']:.2f} 高于 {hi['tier']} 的 {hi['confidence']:.2f}，层级与确定性倒挂",
                    })

    # 单向 contradicts 声明
    by_code = {c["code"]: c for c in claims}
    asymmetric = []
    for c in claims:
        for target in c["links"].get("contradicts") or []:
            other = by_code.get(target)
            if other and c["code"] not in (other["links"].get("contradicts") or []):
                asymmetric.append({"a": c["code"], "b": target,
                                   "reason": f"{c['code']} 声明与 {target} 矛盾，但对方未反向声明（通常漏写）"})

    # 实体关联网络
    ent_map = {}
    for c in claims:
        for e in ents(c):
            ent_map.setdefault(e, []).append(c["code"])
    links = [{"entity": e, "ids": ids} for e, ids in
             sorted(ent_map.items(), key=lambda kv: -len(kv[1])) if len(ids) > 1]

    return {
        "conflicts": conflicts, "inverted": inverted, "asymmetric": asymmetric,
        "entity_links": links,
        "honesty": "结构化检测只抓『形式打架』：同实体+同年+数值不同。措辞不同但意思相反的语义冲突，交给 AI 反方论证。",
    }


# ── Brier / 校准 ─────────────────────────────────────

def calibration(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT probability, outcome, brier FROM predictions WHERE status='resolved'").fetchall()
    resolved = [dict(r) for r in rows]
    bins = [{"lo": p / 100, "hi": (p + 10) / 100, "n": 0, "hits": 0}
            for p in range(0, 100, 10)]
    briers = []
    for r in resolved:
        briers.append(r["brier"])
        idx = min(9, max(0, int(r["probability"] * 10)))
        bins[idx]["n"] += 1
        bins[idx]["hits"] += r["outcome"]
    for b in bins:
        b["observed"] = (b["hits"] / b["n"]) if b["n"] else None
        b["label"] = f"{int(b['lo']*100)}–{int(b['hi']*100)}%"
    return {
        "resolved": resolved,
        "brier_avg": round(sum(briers) / len(briers), 4) if briers else None,
        "n_resolved": len(resolved),
        "bins": bins,
        "note": "样本 < 20 时校准曲线不做趋势判断（单bin标准误≈0.15）。",
    }


# ── AI 接入（OpenAI 兼容，零依赖）────────────────────

def ai_config() -> dict:
    return {
        "base_url": (os.environ.get("GL_AI_BASE_URL") or "").rstrip("/"),
        "model": os.environ.get("GL_AI_MODEL") or "",
        "key": os.environ.get("GL_AI_KEY") or "",
    }


def ai_chat(messages: list[dict], max_tokens: int = 4000) -> str:
    cfg = ai_config()
    if not cfg["base_url"] or not cfg["key"]:
        raise RuntimeError("AI 未配置。设置环境变量 GL_AI_BASE_URL / GL_AI_MODEL / GL_AI_KEY（OpenAI 兼容端点），重启服务。")
    body = json.dumps({
        "model": cfg["model"] or "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['key']}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return (payload["choices"][0]["message"].get("content") or "").strip()


INGEST_SYSTEM = """你是「观澜」认知平台的摄取模块。输入一篇文章，输出结构化 YAML。
对每一条可判定真伪的陈述，输出：
- statement: 一句话命题（禁止"可能/或许/据说"）
- tier: T0一手数据/T1机构共识/T2专业分析/T3自媒体观点 之一
- confidence: 0-1（T0≤0.95 T1≤0.85 T2≤0.70 T3≤0.50）
- as_of: 事实时点 YYYY-MM-DD，原文没有就填 null
- entities: [实体]
- falsifier: 什么情况下这句是错的
- quote: 支撑它的原文片段（逐字）

规则：
1. 观点而非事实 → 输出 - question: 疑问句 + origin_tier + resolution: 如何裁决
2. 数字原样摘录，不换算不四舍五入
3. 只输出 YAML 列表，不要解释、不要代码围栏"""

SYNTH_SYSTEM = """你是「观澜」认知平台的综合模块。只使用提供的正典条目回答，不用你的先验知识。
规则：
1. 每一句结论后用 [C-编号] 标注出处（编号如 C-2026-0001）。
2. 无法标注出处的句子，直接不要写。
3. 证据不足就直接说"库内证据不足"，并列出还缺什么。
4. 相互矛盾的条目必须并列呈现，不许替用户选边。
5. 区分「数据说了什么」和「这意味着什么」，分段陈述。"""

DEVIL_SYSTEM = """你是「观澜」的魔鬼代言人。任务：用库内证据论证用户给定结论的反面。
规则：
1. 必须引用具体 [C-编号]，不许空泛地说"另一方面"。
2. 找支持反面的最强证据，不是最弱的。
3. 指出用户结论依赖的条目里哪条置信度最低、最可能被修订。
4. 最后一句：如果用户是对的，最可能是因为什么。"""


def parse_ingest_yaml(text: str) -> list[dict]:
    """极简 YAML 列表解析（- key: value 结构），不引第三方库。"""
    items, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            if cur:
                items.append(cur)
            cur = {}
            line = line.lstrip()[2:]
        if cur is None:
            continue
        m = re.match(r"([a-zA-Z_]+)\s*:\s*(.*)", line.strip())
        if m:
            k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
            if v == "null" or v == "":
                v = None
            cur[k] = v
    if cur:
        items.append(cur)
    return items


# ── HTTP 服务 ────────────────────────────────────────

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, parse_qs, unquote  # noqa: E402

ROUTES_GET = {}
ROUTES_POST = {}


def route_get(path):
    def deco(fn):
        ROUTES_GET[path] = fn
        return fn
    return deco


def route_post(path):
    def deco(fn):
        ROUTES_POST[path] = fn
        return fn
    return deco


@route_get("/api/overview")
def api_overview(conn, qs):
    stats = {
        "claims": conn.execute("SELECT COUNT(*) n FROM claims WHERE kind='claim'").fetchone()["n"],
        "questions": conn.execute("SELECT COUNT(*) n FROM claims WHERE kind='question'").fetchone()["n"],
        "t0": conn.execute("SELECT COUNT(*) n FROM claims WHERE tier='T0' AND kind='claim'").fetchone()["n"],
        "active": conn.execute("SELECT COUNT(*) n FROM claims WHERE status='active' AND kind='claim'").fetchone()["n"],
        "series": conn.execute("SELECT COUNT(*) n FROM series").fetchone()["n"],
        "predictions_open": conn.execute("SELECT COUNT(*) n FROM predictions WHERE status='open'").fetchone()["n"],
        "predictions_resolved": conn.execute("SELECT COUNT(*) n FROM predictions WHERE status='resolved'").fetchone()["n"],
        "decisions": conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"],
        "calibers": conn.execute("SELECT COUNT(*) n FROM calibers").fetchone()["n"],
        "baserates": conn.execute("SELECT COUNT(*) n FROM baserates").fetchone()["n"],
        "sources": conn.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"],
    }
    conf = detect_conflicts(conn)
    stats["conflicts"] = len(conf["conflicts"]) + len(conf["inverted"]) + len(conf["asymmetric"])

    by_domain = conn.execute(
        "SELECT domain, COUNT(*) n FROM claims WHERE kind='claim' GROUP BY domain").fetchall()
    recent = conn.execute(
        "SELECT * FROM claims ORDER BY id DESC LIMIT 6").fetchall()
    due = conn.execute(
        "SELECT * FROM predictions WHERE status='open' ORDER BY resolve_by LIMIT 5").fetchall()
    calib = calibration(conn)

    return {
        "stats": stats,
        "by_domain": [{"domain": r["domain"], "label": DOMAINS.get(r["domain"], r["domain"]),
                       "n": r["n"]} for r in by_domain],
        "recent": [row_to_claim(r) for r in recent],
        "due_predictions": [dict(r) for r in due],
        "calibration": calib,
        "ai_ready": bool(ai_config()["base_url"] and ai_config()["key"]),
    }


@route_get("/api/claims")
def api_claims(conn, qs):
    domain = (qs.get("domain") or [None])[0]
    tier = (qs.get("tier") or [None])[0]
    kind = (qs.get("kind") or [None])[0]
    status = (qs.get("status") or [None])[0]
    q = "SELECT * FROM claims WHERE 1=1"
    args = []
    if domain:
        q += " AND domain=?"
        args.append(domain)
    if tier:
        q += " AND tier=?"
        args.append(tier)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id DESC"
    rows = conn.execute(q, args).fetchall()
    return {"claims": [row_to_claim(r) for r in rows]}


@route_get("/api/search")
def api_search(conn, qs):
    term = (qs.get("q") or [""])[0].strip()
    if not term:
        return {"results": []}
    # trigram 分词器需要 ≥3 字符；短词（如"口径""GDP"）走 LIKE 子串
    if len(term) < 3:
        rows = conn.execute(
            "SELECT * FROM claims WHERE statement LIKE ? OR notes LIKE ? OR question LIKE ? LIMIT 40",
            (f"%{term}%", f"%{term}%", f"%{term}%")).fetchall()
        return {"results": [row_to_claim(r) for r in rows]}
    safe = term.replace('"', '""')
    try:
        rows = conn.execute(
            """SELECT c.* FROM claims_fts f JOIN claims c ON c.id=f.rowid
               WHERE claims_fts MATCH ? ORDER BY rank LIMIT 40""",
            (f'"{safe}"',)).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT * FROM claims WHERE statement LIKE ? OR notes LIKE ? LIMIT 40",
            (f"%{term}%", f"%{term}%")).fetchall()
    return {"results": [row_to_claim(r) for r in rows]}


@route_get("/api/claims/{code}")
def api_claim_one(conn, qs, code):
    r = conn.execute("SELECT * FROM claims WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": f"找不到 {code}"}
    d = row_to_claim(r)
    if d["source_id"]:
        s = conn.execute("SELECT * FROM sources WHERE id=?", (d["source_id"],)).fetchone()
        d["source"] = dict(s) if s else None
    return d


@route_post("/api/claims")
def api_claim_create(conn, qs, body):
    data = validate_claim(body, conn)
    code = next_code(conn, "C" if data["kind"] == "claim" else "Q", "claims")
    conn.execute(
        """INSERT INTO claims (code,kind,statement,question,resolution,domain,entities,
           source_id,tier,confidence,as_of,observed_at,falsifier,status,links,tags,notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, data["kind"], data["statement"], data["question"], data["resolution"],
         data["domain"], data["entities"], data["source_id"], data["tier"],
         data["confidence"], data["as_of"], data["observed_at"], data["falsifier"],
         data["status"], data["links"], data["tags"], data["notes"]))
    conn.commit()
    return {"ok": True, "code": code}


@route_post("/api/claims/{code}")
def api_claim_update(conn, qs, body, code):
    r = conn.execute("SELECT * FROM claims WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": f"找不到 {code}"}
    allowed = {"status", "confidence", "notes", "falsifier", "links", "tags", "resolution"}
    sets, args = [], []
    for k, v in (body or {}).items():
        if k in allowed:
            if k in ("links", "tags"):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return {"error": "没有可更新字段"}
    sets.append("updated_at=datetime('now','localtime')")
    args.append(code)
    conn.execute(f"UPDATE claims SET {', '.join(sets)} WHERE code=?", args)
    conn.commit()
    return {"ok": True}


@route_post("/api/import")
def api_import(conn, qs, body):
    """JSON 导入：{"claims":[...]} 或 {"series":{"code":..,"name":..,"unit":..,"rows":[{"period","value"}]}}"""
    report = {"claims": 0, "errors": [], "observations": 0, "revised": 0}

    for item in body.get("claims") or []:
        try:
            data = validate_claim(item, conn)
            code = next_code(conn, "C" if data["kind"] == "claim" else "Q", "claims")
            conn.execute(
                """INSERT INTO claims (code,kind,statement,question,resolution,domain,entities,
                   source_id,tier,confidence,as_of,observed_at,falsifier,status,links,tags,notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, data["kind"], data["statement"], data["question"], data["resolution"],
                 data["domain"], data["entities"], data["source_id"], data["tier"],
                 data["confidence"], data["as_of"], data["observed_at"], data["falsifier"],
                 data["status"], data["links"], data["tags"], data["notes"]))
            report["claims"] += 1
        except ValidationError as e:
            report["errors"].append(str(e))
        except sqlite3.IntegrityError as e:
            report["errors"].append(f"数据库约束: {e}")

    ser = body.get("series")
    if ser:
        code = ser.get("code", "")
        if not code:
            report["errors"].append("series.code 缺失")
        else:
            conn.execute(
                """INSERT INTO series (code,name,unit,freq,source) VALUES (?,?,?,?,?)
                   ON CONFLICT(code) DO NOTHING""",
                (code, ser.get("name", code), ser.get("unit", ""),
                 ser.get("freq", "M"), ser.get("source", "")))
            today = __import__("datetime").date.today().isoformat()
            for row in ser.get("rows") or []:
                period = str(row.get("period", "")).strip()
                value = str(row.get("value", "")).strip()
                vintage = str(row.get("vintage") or today)
                if not period or value == "":
                    continue
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO observations (code,period,value,unit,vintage) VALUES (?,?,?,?,?)",
                        (code, period, value, ser.get("unit", ""), vintage))
                    report["observations"] += 1
                except sqlite3.IntegrityError:
                    pass
    conn.commit()
    report["ok"] = True
    return report


@route_get("/api/series")
def api_series(conn, qs):
    rows = conn.execute("""
        SELECT s.*, COUNT(o.period) n_obs,
               COUNT(DISTINCT o.vintage) n_vintage,
               MAX(o.period) latest_period
        FROM series s LEFT JOIN observations o ON o.code=s.code
        GROUP BY s.code ORDER BY s.code""").fetchall()
    return {"series": [dict(r) for r in rows]}


@route_get("/api/series/{code}")
def api_series_one(conn, qs, code):
    s = conn.execute("SELECT * FROM series WHERE code=?", (code,)).fetchone()
    if not s:
        return {"error": f"未注册的序列 {code}"}
    rows = conn.execute(
        "SELECT * FROM observations WHERE code=? ORDER BY period, vintage", (code,)).fetchall()
    latest = {}
    for r in rows:
        latest[r["period"]] = r  # 后 vintage 覆盖（已排序）
    points = [{"p": p, "v": float(v["value"]) if _num(v["value"]) else None,
               "vintage": v["vintage"]} for p, v in sorted(latest.items())]
    # 修订检测
    by_period = {}
    for r in rows:
        by_period.setdefault(r["period"], []).append(r)
    revisions = [{"period": p,
                  "history": [{"value": x["value"], "vintage": x["vintage"]} for x in h]}
                 for p, h in by_period.items() if len(h) > 1]
    calibers = conn.execute("SELECT * FROM calibers WHERE series_code=? ORDER BY changed_at",
                            (code,)).fetchall()
    return {"series": dict(s), "points": points, "revisions": revisions,
            "n_vintage": len({r["vintage"] for r in rows}),
            "calibers": [dict(c) for c in calibers]}


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


@route_get("/api/conflicts")
def api_conflicts(conn, qs):
    return detect_conflicts(conn)


@route_get("/api/predictions")
def api_predictions(conn, qs):
    rows = conn.execute(
        "SELECT * FROM predictions ORDER BY status DESC, resolve_by").fetchall()
    return {"predictions": [dict(r) for r in rows], "calibration": calibration(conn)}


@route_post("/api/predictions")
def api_prediction_create(conn, qs, body):
    prop = str(body.get("proposition") or "").strip()
    if len(prop) < 8:
        return {"error": "命题太短"}
    try:
        p = float(body.get("probability"))
    except (TypeError, ValueError):
        return {"error": "probability 必须是 0-1 的数字（不许『可能会』）"}
    if not (0 < p < 1):
        return {"error": "probability 必须在 (0,1) 开区间——100% 和 0% 不是预测"}
    criteria = str(body.get("criteria") or "").strip()
    if len(criteria) < 8:
        return {"error": "判定条件必填，且必须可第三方复核"}
    resolve_by = str(body.get("resolve_by") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", resolve_by):
        return {"error": "到期日必填（YYYY-MM-DD）。没有到期日的预测是感想"}
    code = next_code(conn, "P", "predictions")
    conn.execute(
        """INSERT INTO predictions (code,proposition,probability,criteria,resolve_by,
           base_rate,depends_on,pre_mortem) VALUES (?,?,?,?,?,?,?,?)""",
        (code, prop, p, criteria, resolve_by,
         str(body.get("base_rate") or ""),
         json.dumps(body.get("depends_on") or [], ensure_ascii=False),
         str(body.get("pre_mortem") or "")))
    conn.commit()
    return {"ok": True, "code": code}


@route_post("/api/predictions/{code}/settle")
def api_prediction_settle(conn, qs, body, code):
    r = conn.execute("SELECT * FROM predictions WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": f"找不到 {code}"}
    if r["status"] == "resolved":
        return {"error": "已结算，预测不可改判"}
    outcome = body.get("outcome")
    if outcome not in (0, 1, "0", "1", True, False):
        return {"error": "outcome 必须是 1（发生）或 0（未发生）"}
    outcome = int(outcome)
    brier = round((r["probability"] - outcome) ** 2, 4)
    conn.execute(
        """UPDATE predictions SET status='resolved', outcome=?, brier=?,
           resolved_at=date('now','localtime'), lesson=? WHERE code=?""",
        (outcome, brier, str(body.get("lesson") or ""), code))
    conn.commit()
    return {"ok": True, "brier": brier, "note": "0.25 = 随机猜测基线，你的均值必须显著低于它"}


@route_get("/api/decisions")
def api_decisions(conn, qs):
    rows = conn.execute("SELECT * FROM decisions ORDER BY date DESC").fetchall()
    return {"decisions": [dict(r) for r in rows]}


@route_post("/api/decisions")
def api_decision_create(conn, qs, body):
    dec = str(body.get("decision") or "").strip()
    if len(dec) < 4:
        return {"error": "决策内容太短"}
    date = str(body.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "日期必填 YYYY-MM-DD"}
    code = next_code(conn, "D", "decisions")
    conn.execute(
        """INSERT INTO decisions (code,decision,date,stakes,reversible,depends_on,chosen_because)
           VALUES (?,?,?,?,?,?,?)""",
        (code, dec, date, body.get("stakes", "medium"),
         1 if body.get("reversible", True) else 0,
         json.dumps(body.get("depends_on") or [], ensure_ascii=False),
         str(body.get("chosen_because") or "")))
    conn.commit()
    return {"ok": True, "code": code}


@route_get("/api/baserates")
def api_baserates(conn, qs):
    rows = conn.execute("SELECT * FROM baserates ORDER BY code").fetchall()
    return {"baserates": [dict(r) for r in rows]}


@route_get("/api/calibers")
def api_calibers(conn, qs):
    rows = conn.execute("""
        SELECT c.*, s.name AS series_name FROM calibers c
        LEFT JOIN series s ON s.code=c.series_code
        ORDER BY c.changed_at DESC""").fetchall()
    return {"calibers": [dict(r) for r in rows]}


@route_get("/api/sources")
def api_sources(conn, qs):
    rows = conn.execute("SELECT * FROM sources ORDER BY id DESC").fetchall()
    return {"sources": [dict(r) for r in rows]}


@route_post("/api/sources")
def api_source_create(conn, qs, body):
    title = str(body.get("title") or "").strip()
    if len(title) < 2:
        return {"error": "标题必填"}
    tier = body.get("tier")
    if tier not in TIER_CEILING:
        return {"error": "tier 必填（T0-T4）"}
    code = next_code(conn, "S", "sources")
    conn.execute(
        """INSERT INTO sources (code,title,publisher,url,tier,published,kind,notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (code, title, str(body.get("publisher") or ""), str(body.get("url") or ""),
         tier, str(body.get("published") or ""),
         body.get("kind", "web") if body.get("kind") in SOURCE_KINDS else "web",
         str(body.get("notes") or "")))
    conn.commit()
    return {"ok": True, "code": code}


@route_get("/api/events")
def api_events(conn, qs):
    rows = conn.execute("SELECT * FROM events ORDER BY when_at DESC LIMIT 100").fetchall()
    return {"events": [dict(r) for r in rows]}


# ── AI 路由 ──────────────────────────────────────────

@route_post("/api/ai/ingest")
def api_ai_ingest(conn, qs, body):
    text = str(body.get("text") or "").strip()
    if len(text) < 50:
        return {"error": "文章太短（≥50 字）"}
    try:
        raw = ai_chat([
            {"role": "system", "content": INGEST_SYSTEM},
            {"role": "user", "content": text[:60000]},
        ], max_tokens=6000)
    except RuntimeError as e:
        return {"error": str(e)}
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        return {"error": f"AI 调用失败: {e}"}

    items = parse_ingest_yaml(raw)
    parsed = []
    for it in items:
        try:
            tier = it.get("tier") or "T2"
            if tier not in TIER_CEILING:
                continue
            conf = float(it.get("confidence") or TIER_CEILING[tier])
            conf = min(conf, TIER_CEILING[tier])
            parsed.append({
                "kind": "question" if it.get("question") else "claim",
                "statement": it.get("statement") or "",
                "question": it.get("question") or "",
                "resolution": it.get("resolution") or "",
                "tier": tier, "confidence": round(conf, 2),
                "as_of": it.get("as_of") or "",
                "entities": it.get("entities") or "",
                "falsifier": it.get("falsifier") or "",
                "quote": it.get("quote") or "",
            })
        except (ValueError, TypeError):
            continue

    conn.execute(
        "INSERT INTO ai_drafts (source_text,payload,parsed,model) VALUES (?,?,?,?)",
        (text[:60000], raw, json.dumps(parsed, ensure_ascii=False),
         ai_config()["model"] or "unknown"))
    conn.commit()
    return {"ok": True, "count": len(parsed), "items": parsed}


@route_post("/api/ai/ask")
def api_ai_ask(conn, qs, body):
    question = str(body.get("q") or "").strip()
    if len(question) < 2:
        return {"error": "问题太短"}
    # 检索：FTS 命中 + 同域活跃 claim 兜底
    try:
        rows = conn.execute(
            """SELECT c.* FROM claims_fts f JOIN claims c ON c.id=f.rowid
               WHERE claims_fts MATCH ? ORDER BY rank LIMIT 24""",
            (f'"{question.replace(chr(34), chr(34)*2)}"',)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if len(rows) < 6:
        rows = conn.execute(
            "SELECT * FROM claims WHERE status='active' AND kind='claim' ORDER BY id DESC LIMIT 24").fetchall()
    ctx = []
    for r in rows:
        c = row_to_claim(r)
        ctx.append(f"[{c['code']}] (tier={c['tier']} conf={c['confidence']} as_of={c['as_of']}) {c['statement']}")
    prompt = "问题：" + question + "\n\n正典条目：\n" + "\n".join(ctx)
    try:
        answer = ai_chat([
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": prompt[:60000]},
        ], max_tokens=4000)
    except RuntimeError as e:
        return {"error": str(e)}
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        return {"error": f"AI 调用失败: {e}"}

    # 铁律：无引用句自动丢弃
    sents = re.split(r"(?<=[。！？!?；;\n])", answer)
    kept = [s for s in sents if not s.strip() or re.search(r"\[[CPQ]-\d{4}-\d{3,4}\]", s)]
    dropped = len([s for s in sents if s.strip()]) - len(kept)
    return {"ok": True, "answer": "".join(kept).strip(), "dropped": dropped,
            "used": [row_to_claim(r)["code"] for r in rows[:12]]}


@route_post("/api/ai/devil")
def api_ai_devil(conn, qs, body):
    code = str(body.get("code") or "").strip()
    r = conn.execute("SELECT * FROM claims WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": f"找不到 {code}"}
    c = row_to_claim(r)
    related = conn.execute(
        "SELECT * FROM claims WHERE status='active' AND kind='claim' AND id != ? ORDER BY RANDOM() LIMIT 20",
        (r["id"],)).fetchall()
    ctx = "\n".join(
        f"[{row_to_claim(x)['code']}] ({row_to_claim(x)['tier']} {row_to_claim(x)['confidence']}) {row_to_claim(x)['statement']}"
        for x in related)
    prompt = f"目标结论：「{c['statement']}」（{c['code']}，tier={c['tier']}，conf={c['confidence']}）\n\n库内证据：\n{ctx}"
    try:
        answer = ai_chat([
            {"role": "system", "content": DEVIL_SYSTEM},
            {"role": "user", "content": prompt[:60000]},
        ], max_tokens=3000)
    except RuntimeError as e:
        return {"error": str(e)}
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        return {"error": f"AI 调用失败: {e}"}
    return {"ok": True, "answer": answer}


@route_get("/api/ai/drafts")
def api_ai_drafts(conn, qs):
    rows = conn.execute(
        "SELECT * FROM ai_drafts WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parsed"] = json.loads(d["parsed"] or "[]")
        out.append(d)
    return {"drafts": out}


@route_post("/api/ai/drafts/{id}/accept")
def api_ai_draft_accept(conn, qs, body, id):
    r = conn.execute("SELECT * FROM ai_drafts WHERE id=?", (id,)).fetchone()
    if not r:
        return {"error": "草稿不存在"}
    parsed = json.loads(r["parsed"] or "[]")
    accepted, errors = [], []
    for item in body.get("items") or parsed:
        try:
            data = validate_claim(item, conn)
            code = next_code(conn, "C", "claims")
            conn.execute(
                """INSERT INTO claims (code,kind,statement,question,resolution,domain,entities,
                   source_id,tier,confidence,as_of,observed_at,falsifier,status,links,tags,notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, data["kind"], data["statement"], data["question"], data["resolution"],
                 data["domain"], data["entities"], data["source_id"], data["tier"],
                 data["confidence"], data["as_of"], data["observed_at"], data["falsifier"],
                 data["status"], data["links"], data["tags"],
                 f"AI 摄取，quote: {item.get('quote', '')}"))
            accepted.append(code)
        except (ValidationError, sqlite3.IntegrityError) as e:
            errors.append(str(e))
    conn.execute("UPDATE ai_drafts SET status='accepted' WHERE id=?", (id,))
    conn.commit()
    return {"ok": True, "accepted": accepted, "errors": errors}


@route_post("/api/ai/drafts/{id}/discard")
def api_ai_draft_discard(conn, qs, body, id):
    conn.execute("UPDATE ai_drafts SET status='discarded' WHERE id=?", (id,))
    conn.commit()
    return {"ok": True}


@route_get("/api/ai/status")
def api_ai_status(conn, qs):
    cfg = ai_config()
    return {"ready": bool(cfg["base_url"] and cfg["key"]),
            "model": cfg["model"] or "(未配置)",
            "base_url": cfg["base_url"] or "(未配置)"}


# ── Handler ──────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "guanlan/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or not path.startswith("/api/"):
            return self._static(path)
        if path in ROUTES_GET:
            conn = db()
            try:
                return self._json(ROUTES_GET[path](conn, qs))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            finally:
                conn.close()
        # 参数化路由
        for pattern, fn in ROUTES_GET.items():
            if "{" in pattern:
                base = pattern.split("/{")[0]
                if path.startswith(base + "/"):
                    arg = unquote(path[len(base) + 1:])
                    conn = db()
                    try:
                        return self._json(fn(conn, qs, arg))
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                    finally:
                        conn.close()
        self._json({"error": f"未知接口 {path}"}, 404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except json.JSONDecodeError:
            return self._json({"error": "请求体不是合法 JSON"}, 400)

        if path in ROUTES_POST:
            conn = db()
            try:
                return self._json(ROUTES_POST[path](conn, qs, body))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            finally:
                conn.close()
        for pattern, fn in ROUTES_POST.items():
            if "{" in pattern:
                base, _, tail_full = pattern.partition("/{")
                arg_name, _, rest = tail_full.partition("}")  # rest = "/settle" 或 ""
                if not path.startswith(base + "/"):
                    continue
                if rest:
                    if not path.endswith(rest):
                        continue
                    arg = unquote(path[len(base) + 1:len(path) - len(rest)])
                else:
                    arg = unquote(path[len(base) + 1:])
                if not arg:
                    continue
                conn = db()
                try:
                    return self._json(fn(conn, qs, body, arg))
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                finally:
                    conn.close()
        self._json({"error": f"未知接口 {path}"}, 404)

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}.get(
                     target.suffix.lstrip("."), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    ap = argparse.ArgumentParser(prog="guanlan", description="观澜 · 个人认知平台")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default=None)
    ap.add_argument("--seed", action="store_true", help="灌入示例数据后启动")
    args = ap.parse_args()

    init_db()
    if args.seed or os.environ.get("GL_SEED"):
        import seed
        seed.run(db())

    env_port = os.environ.get("PORT")
    port = int(env_port) if env_port else args.port
    host = args.host or os.environ.get("GL_BIND") or (
        "0.0.0.0" if env_port else "127.0.0.1")

    url = f"http://127.0.0.1:{port}/"
    print(f"观澜 GUANLAN · {url}")
    print(f"数据库   · {DB_PATH}")
    ai = ai_config()
    print(f"AI       · {'已配置 ' + ai['model'] if ai['base_url'] and ai['key'] else '未配置（AI 页可看配置指引）'}")
    print("Ctrl+C 停止\n")

    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if "in use" in str(e).lower() or getattr(e, "errno", None) in (48, 98, 10048):
            print(f"服务已在运行 · {url}")
            return 0
        raise
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
