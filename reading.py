#!/usr/bin/env python
"""观澜 GUANLAN · 阅读中心（独立大板块）服务端模块

与 server.py 的边界：
  * 本模块不 import server，避免 __main__ 双实例；依赖通过 install(**ctx) 注入。
  * 只注册 /api/reading/* 与 /api/ai/reading 两类 JSON 路由。
  * 一份阅读资料 = 一条 clips；reading_items 是它的阅读态扩展（R1）。
  * 原件、标签、板块、知识卡仍由 clips 体系管；这里不另起一套内容库。

启动装配见 server.py 末尾的 reading.install(...)。
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import posixpath
import re
import zipfile
from datetime import datetime
from pathlib import Path

import app_paths

ROOT = app_paths.RES_DIR
SCHEMA_FILE = ROOT / "reading_schema.sql"

# 依赖由 server.py 注入
_CTX: dict = {}
_READY = False

BOOK_EXTS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".azw4", ".fb2", ".fbz",
             ".cbz", ".cbr", ".txt", ".md", ".markdown", ".rtf", ".html", ".htm",
             ".xhtml", ".docx", ".doc"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".xml",
             ".html", ".htm", ".xhtml", ".srt", ".vtt"}
ZIP_DOC_EXTS = {".docx", ".pptx", ".xlsx", ".odt", ".epub"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac",
              ".wma", ".aiff", ".webm"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".ts"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
ALL_EXTS = BOOK_EXTS | TEXT_EXTS | MEDIA_EXTS | {".pptx", ".xlsx", ".ppt", ".xls", ".doc"}

MAX_DOC_CHARS = 300_000
CHAPTER_TARGET = 4_200

NOTE_KINDS = {"note", "excerpt", "digest", "ai"}
LINK_KINDS = {"annotation", "bookmark", "note", "excerpt", "clip", "claim",
              "board", "ai_output", "verdict"}
RELATIONS = {"refs", "cites", "derived_from", "belongs_to", "supports"}
ITEM_KINDS = {"book", "doc", "audio", "video", "web", "text"}


# ══════════════════════════════════════════════════════════
# 装配 / 通用
# ══════════════════════════════════════════════════════════

def install(route_get, route_post, **ctx) -> None:
    """由 server.py 在路由表构建完毕后调用，注入依赖并注册路由。"""
    global _CTX
    _CTX = ctx
    for path, fn in GET_ROUTES.items():
        route_get(path)(fn)
    for path, fn in POST_ROUTES.items():
        route_post(path)(fn)


def ensure(conn) -> None:
    """幂等建表。老库在原地升级，不需要停服导数据。"""
    global _READY
    if _READY:
        return
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_items'").fetchone()
    if not exists:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _READY = True


def purge_clip(conn, cid: int) -> None:
    """删除 clip 时收拾阅读中心的散件（虚表没有外键，必须手工清）。"""
    ensure(conn)
    for table in ("reading_bookmarks", "reading_notes", "reading_links", "reading_sessions"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE clip_id=?", (cid,))
        except Exception:
            pass
    try:
        conn.execute("DELETE FROM reading_fts WHERE clip_id=?", (str(cid),))
    except Exception:
        pass
    conn.execute("DELETE FROM reading_items WHERE clip_id=?", (cid,))


def _j(raw, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except (ValueError, TypeError):
        return fallback


def _clean_tags(raw, limit=20) -> list[str]:
    if isinstance(raw, str):
        raw = [t.strip() for t in re.split(r"[,，;；]", raw)]
    out, seen = [], set()
    for t in (raw or [])[:40]:
        t = str(t or "").strip()[:24]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _err(msg: str) -> dict:
    return {"error": msg}


def _like(q: str) -> str:
    """LIKE 模式，转义 % / _ / \\ 本身，避免用户输入变成通配符。

    用法：SQL 里必须同时写 ``LIKE ? ESCAPE '\\'``，否则转义不生效。
    与 bible.py 的同名 helper 保持一致。
    """
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + esc + "%"


def _int_id(raw, field: str = "id") -> int:
    """路径参数 → 正整数。非法值抛 ValidationError（server 映射成 400）。

    直接 int() 会让 ``/api/reading/book/abc`` 抛 ValueError，被 server 的
    兜底分支当成内部错误回 500；路径段写错是客户端错误，应该是 400。
    ValidationError 由 server.py 通过 install(**ctx) 注入，本模块不 import server。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        exc = _CTX.get("ValidationError")
        raise (exc(f"{field} 必须是正整数") if exc
               else ValueError(f"{field} 必须是正整数"))
    return n


def _clip_exists(conn, cid) -> bool:
    return bool(conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone())


def _item_row(conn, cid):
    return conn.execute("SELECT * FROM reading_items WHERE clip_id=?", (cid,)).fetchone()


def _doc_has_real_text(doc: str) -> bool:
    """结构化解析的结果里是否真有可读正文。

    `_parse_pdf` 以前给空页写「〔第 N 页〕」占位符，乱码页也会整段进
    doc；这种 doc 非空但没内容，绝不能拿去覆盖 clips 的正文与状态。
    """
    import pdf_text
    stripped = re.sub(r"〔第\s*\d+\s*页〕", "", doc or "")
    stripped = re.sub(r"\s+", "", stripped)
    return bool(stripped) and not pdf_text.looks_garbled(stripped)


def _write_clip_text(conn, cid: int, doc: str, *, trusted: bool = False) -> bool:
    """只有解析出真实正文才回写 clips，且保留已有的截断提示。

    ensure_text / api_reading_add / api_transcript / api_reparse 以前各自
    无条件写 `fetch_status='stored', fetch_error=''`，会把「没正文」的
    历史条目洗成「已入库」，也会抹掉刚写进去的截断提示。
    `trusted=True` 用于用户亲手粘贴的文字稿：那是人的输入，不该被
    「有没有可读字符」的启发式挡掉。
    """
    if not doc or (not trusted and not _doc_has_real_text(doc)):
        return False
    row = conn.execute("SELECT fetch_error FROM clips WHERE id=?", (cid,)).fetchone()
    old_error = (row["fetch_error"] if row else "") or ""
    keep = old_error if "截断" in old_error else ""
    conn.execute(
        """UPDATE clips SET content_text=?, fetch_status='stored', fetch_error=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (doc, keep, cid))
    return True


def ensure_text(conn, cid: int) -> str:
    """R2 自愈：init_db() 会清空文件件正文，这里从 doc_text 回填，
    保证 clip_annotations 的字符偏移永远与正文一致。"""
    item = _item_row(conn, cid)
    if not item:
        return ""
    doc = item["doc_text"] or ""
    cur = conn.execute("SELECT content_text FROM clips WHERE id=?", (cid,)).fetchone()
    if doc and (not cur or (cur["content_text"] or "") != doc):
        _write_clip_text(conn, cid, doc)
    return doc


# ══════════════════════════════════════════════════════════
# 结构化解析：把任何原件变成 (doc_text, chapters)
# ══════════════════════════════════════════════════════════

def _html_to_text(raw: str) -> str:
    """XHTML/HTML → 纯文本。保留段落与标题的换行，不做正文打分。"""
    s = re.sub(r"(?is)<(script|style|svg|head)[^>]*>.*?</\1>", " ", raw or "")
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|blockquote|section|article|figcaption)\s*>", "\n", s)
    s = re.sub(r"(?i)<(p|div|h[1-6]|li|tr|blockquote|section|article)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[\u200b-\u200f\ufeff\u2060\xa0]", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


HEAD_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|"
    r"第[一二三四五六七八九十百千零〇\d]+[章节部篇卷回折][\s:：、.．\-—]*|"
    r"(?:Chapter|CHAPTER|Part|PART|Section|SECTION)\s+[\dIVXLC]+[\s:.：\-—]*|"
    r"[一二三四五六七八九十]+[、．.]\s*|"
    r"\d{1,3}[、．.]\s*[\u4e00-\u9fff])"
    r"(?P<title>\S.{0,58})$"
)


def _split_by_headings(text: str, base_title: str = "") -> list[tuple]:
    """按标题行切章；没有标题就按段落切成长度相近的「节」，避免一页几万字。"""
    lines = text.split("\n")
    heads: list[tuple[int, str]] = []
    cursor = 0
    for line in lines:
        m = HEAD_RE.match(line) if len(line) <= 80 else None
        if m:
            title = (m.group("title") or "").strip(" #\t")
            heads.append((cursor, title or line.strip()[:40]))
        cursor += len(line) + 1
    if heads and heads[0][0] > 200:
        heads.insert(0, (0, "开篇"))
    if not heads:
        heads = [(0, base_title or "正文")]
    parts: list[tuple] = []
    for i, (start, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        chunk = text[start:end].strip()
        if not chunk:
            continue
        # 超长无标题章节：再按段落切成可读的分节
        if len(chunk) > CHAPTER_TARGET * 2:
            sub, at = [], 0
            while at < len(chunk):
                cut = chunk.find("\n\n", at + CHAPTER_TARGET)
                if cut < 0 or cut >= len(chunk) - 200:
                    cut = len(chunk)
                sub.append(chunk[at:cut].strip())
                at = cut + 2
            for k, piece in enumerate(sub, 1):
                if piece:
                    parts.append((f"{title} · {k}" if len(sub) > 1 else title, piece, 2, 0))
        else:
            parts.append((title, chunk, 1, 0))
    return parts


def _build(parts: list[tuple], cap: int = MAX_DOC_CHARS) -> tuple[str, list[dict]]:
    """把 [(title, text, level, page[, sections])] 拼成 doc_text，同时算绝对字符偏移。

    sections 是章内小节（随正文连续阅读，目录里作为二级导航）：
        [{"title": str, "level": 2, "offset": 相对本章正文起点的字符偏移}]
    """
    buf: list[str] = []
    chapters: list[dict] = []
    pos = 0
    for i, part in enumerate(parts):
        title = str(part[0] or "").strip() or f"第 {i + 1} 节"
        text = str(part[1] or "").strip()
        level = int(part[2] or 1)
        page = int(part[3] or 0)
        sections = part[4] if len(part) > 4 and part[4] else []
        if not text:
            continue
        if buf:
            buf.append("\n\n")
            pos += 2
        if pos >= cap:
            break
        text = text[:cap - pos]
        start = pos
        buf.append(text)
        pos += len(text)
        ch = {"id": f"ch{i + 1}", "title": title[:80], "level": level,
              "start": start, "end": pos, "page": page, "chars": len(text)}
        clean_secs = []
        for s in sections:
            try:
                at = int(s.get("offset") or 0)
            except (TypeError, ValueError):
                continue
            name = str(s.get("title") or "").strip()[:80]
            if not name or at < 0 or at >= len(text):
                continue
            if clean_secs and at <= clean_secs[-1]["offset"]:
                continue
            clean_secs.append({"title": name,
                               "level": min(3, max(level + 1, int(s.get("level") or 2))),
                               "offset": at})
        if clean_secs:
            ch["sections"] = clean_secs
        chapters.append(ch)
    return "".join(buf), chapters


# ── EPUB 结构化解析 ───────────────────────────────────────
#  1) 目录（EPUB3 nav / EPUB2 ncx）按锚点切章，保留层级；
#  2) 章内 h1-h6 作为小节，随正文连续阅读，目录里做二级导航；
#  3) 没有目录条目的分片（Calibre 常见的 split_001…）并回上一章；
#  4) 去掉正文开头重复的章节名，标题里的 <br> 归成空格。

_MARK_RE = re.compile(r"\ue000([ah])(\d+)\ue001")
_TAG_OPEN_RE = re.compile(r"(?is)<([a-zA-Z][\w:-]*)([^>]*)>")
_HEAD_BLOCK_RE = re.compile(r"(?is)<h([1-6])[^>]*>(.*?)</h\1\s*>")
_PUNCT_RE = re.compile(
    r"[\s\u3000·・:：、,，.。!！?？\-—－_/\\|()（）\[\]【】{}｛｝'\"“”‘’《》〈〉]+")


def _norm_cmp(s: str) -> str:
    return _PUNCT_RE.sub("", s or "")


def _html_blocks(raw: str) -> tuple[str, dict[str, int], list[dict]]:
    """HTML → (正文, {元素 id: 正文偏移}, [{level,title,offset}])。

    一次扫描同时记正文、跳转锚点和标题位置；正文仍是纯文本，
    标注偏移、FTS、导出都不受影响。
    """
    s = re.sub(r"(?is)<(script|style|svg|head)[^>]*>.*?</\1>", " ", raw or "")
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    heads: list[dict] = []
    for m in _HEAD_BLOCK_RE.finditer(s):
        heads.append({"level": int(m.group(1)),
                      "title": re.sub(r"\s+", " ", _html_to_text(m.group(2))).strip()})
    hidx = 0
    order: list[str] = []
    anchors: dict[str, int] = {}
    def tag_repl(m):
        nonlocal hidx
        tag = m.group(0)
        name = m.group(1).lower()
        out = tag
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"} and hidx < len(heads):
            out = "\ue000h%d\ue001" % hidx + out
            hidx += 1
        im = re.search(r"\bid\s*=\s*[\"']([^\"']+)", tag)
        if im:
            key = im.group(1)
            if key not in anchors:
                anchors[key] = len(order)
                order.append(key)
            out += "\ue000a%d\ue001" % anchors[key]
        return out
    s = _TAG_OPEN_RE.sub(tag_repl, s)
    text = _html_to_text(s)
    removed = 0
    head_pos: dict[int, int] = {}
    anchor_pos: dict[str, int] = {}
    for m in _MARK_RE.finditer(text):
        clean = m.start() - removed
        if m.group(1) == "a":
            anchor_pos[order[int(m.group(2))]] = clean
        else:
            head_pos[int(m.group(2))] = clean
        removed += len(m.group(0))
    clean_text = _MARK_RE.sub("", text)
    # 标记挡住了 strip()，开头可能还留着空行；在这里一次性归零，偏移同步平移
    lead = len(clean_text) - len(clean_text.lstrip())
    if lead:
        clean_text = clean_text[lead:]
        head_pos = {k: max(0, v - lead) for k, v in head_pos.items()}
        anchor_pos = {k: max(0, v - lead) for k, v in anchor_pos.items()}
    out_heads = [{**h, "offset": head_pos[i]}
                 for i, h in enumerate(heads) if i in head_pos]
    return clean_text, anchor_pos, out_heads


def _strip_leading_title(text: str, title: str) -> tuple[str, int]:
    """正文开头若重复章节名（有些 EPUB 用 <br> 把书名拆两行），去掉这几行。"""
    target = _norm_cmp(title)
    if not target:
        return text, 0
    lines = text.split("\n")
    acc = ""
    for i in range(min(3, len(lines))):
        line = lines[i]
        if i == 0 and len(_norm_cmp(line)) > 60:
            return text, 0
        acc += line
        norm = _norm_cmp(acc)
        if not norm:
            continue
        if norm == target or (len(norm) >= 6 and
                              (norm.startswith(target) or target.startswith(norm))):
            cut = len("\n".join(lines[:i + 1]))
            rest = text[cut:].lstrip("\n")
            return rest, len(text) - len(rest)
    return text, 0


def _nav_html(raw: str) -> list[dict]:
    m = re.search(r"(?is)<nav\b[^>]*epub:type\s*=\s*[\"']toc[\"'][^>]*>(.*?)</nav\s*>", raw)
    seg = m.group(1) if m else ""
    if not seg:
        m = re.search(r"(?is)<nav\b[^>]*>(.*?)</nav\s*>", raw)
        seg = m.group(1) if m else raw
    out: list[dict] = []
    depth = 0
    for m in re.finditer(
            r"(?is)(<ol\b[^>]*>)|(</ol\s*>)|"
            r"(<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a\s*>)", seg):
        if m.group(1):
            depth += 1
        elif m.group(2):
            depth = max(0, depth - 1)
        else:
            title = re.sub(r"\s+", " ", _html_to_text(m.group(5))).strip()
            if title:
                out.append({"href": m.group(4), "title": title, "level": max(1, depth)})
    return out


def _nav_ncx(raw: str) -> list[dict]:
    low = raw.lower()
    seg = raw[low.find("<navmap"):] if "<navmap" in low else raw
    out: list[dict] = []
    depth, last = 0, ""
    for m in re.finditer(
            r"(?is)(<navpoint\b[^>]*>)|(</navpoint\s*>)|"
            r"(<text[^>]*>(.*?)</text\s*>)|"
            r"(<content\b[^>]*?src\s*=\s*[\"']([^\"']+)[\"'])", seg):
        if m.group(1):
            depth += 1
            last = ""
        elif m.group(2):
            depth = max(0, depth - 1)
        elif m.group(4) is not None:
            last = re.sub(r"\s+", " ", _html_to_text(m.group(4))).strip()
        elif m.group(6):
            if last:
                out.append({"href": m.group(6), "title": last, "level": max(1, depth)})
            last = ""
    return out


def _epub_nav(z, names: set[str], base: str, manifest: dict) -> list[dict]:
    nav_href = ncx_href = ""
    for href, props in manifest.values():
        if "nav" in (props or "").lower() and not nav_href:
            nav_href = href
        if "ncx" in href.lower() and not ncx_href:
            ncx_href = href
    for kind, cand in (("nav", nav_href), ("ncx", ncx_href)):
        if not cand:
            continue
        full = _epub_join(base, cand)
        if full not in names:
            full = cand
        if full not in names:
            continue
        raw = z.read(full).decode("utf-8", "replace")
        entries = [_e for _e in (_nav_html(raw) if kind == "nav" else _nav_ncx(raw))
                   if _e.get("title")]
        if not entries:
            continue
        for e in entries:
            href = e.pop("href", "")
            path, _, frag = href.partition("#")
            e["file"] = _epub_join(base, path) if path else ""
            e["frag"] = frag
        return entries
    return []


def _epub_join(base: str, href: str) -> str:
    href = (href or "").strip().replace("\\", "/")
    if not href:
        return ""
    return posixpath.normpath(base + href)


def _parse_epub(path: Path) -> tuple[str, list[dict], dict]:
    """EPUB：按目录锚点切章、按标题分节，正文不再重复章节名。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        opf_path = ""
        if "META-INF/container.xml" in names:
            raw = z.read("META-INF/container.xml").decode("utf-8", "replace")
            m = re.search(r'full-path\s*=\s*["\']([^"\']+)["\']', raw)
            if m:
                opf_path = m.group(1)
        if not opf_path or opf_path not in names:
            opf_path = next((n for n in names if n.lower().endswith(".opf")), "")
        if not opf_path:
            raise ValueError("EPUB 缺少 OPF 目录文件")

        opf = z.read(opf_path).decode("utf-8", "replace")
        base = posixpath.dirname(opf_path)
        base = base + "/" if base else ""

        manifest: dict[str, tuple[str, str]] = {}
        for tag in re.findall(r"(?is)<item\b[^>]*>", opf):
            iid = re.search(r'id\s*=\s*["\']([^"\']+)', tag)
            href = re.search(r'href\s*=\s*["\']([^"\']+)', tag)
            props = re.search(r'properties\s*=\s*["\']([^"\']+)', tag)
            if iid and href:
                manifest[iid.group(1)] = (href.group(1), props.group(1) if props else "")

        spine: list[str] = []
        for tag in re.findall(r"(?is)<itemref\b[^>]*>", opf):
            ref = re.search(r'idref\s*=\s*["\']([^"\']+)', tag)
            if ref:
                spine.append(ref.group(1))
        if not spine:
            spine = list(manifest.keys())

        title = ""
        mt = re.search(r"(?is)<dc:title[^>]*>(.*?)</dc:title>", opf)
        if mt:
            title = _html_to_text(mt.group(1))
        author = ""
        ma = re.search(r"(?is)<dc:creator[^>]*>(.*?)</dc:creator>", opf)
        if ma:
            author = _html_to_text(ma.group(1))

        nav = _epub_nav(z, names, base, manifest)
        by_file: dict[str, list[dict]] = {}
        for e in nav:
            if e["file"] in names:
                by_file.setdefault(e["file"], []).append(e)

        parts: list[dict] = []
        current: dict | None = None
        pending_level = 0

        def add_part(t: str, lv: int) -> dict:
            nonlocal pending_level
            lv = max(1, int(lv or 1))
            if pending_level:
                # 父级只有标题、没有正文（如「附錄」总标题）时，它的子项整体升一级
                if lv > pending_level:
                    lv = pending_level
                else:
                    pending_level = 0
            p = {"title": str(t or "").strip()[:80], "level": lv, "text": "", "sections": []}
            parts.append(p)
            return p

        def place(p: dict, piece: str, abs_start: int, heads: list[dict]) -> None:
            if not piece:
                return
            base_off = len(p["text"]) + (2 if p["text"] else 0)
            for h in heads:
                rel = h["offset"] - abs_start
                if rel < -2 or rel >= len(piece):
                    continue
                rel = max(0, rel)
                p["sections"].append({
                    "title": h["title"],
                    "level": min(3, max(p["level"] + 1, h["level"])),
                    "offset": base_off + rel,
                })
            if p["text"]:
                p["text"] += "\n\n"
            p["text"] += piece

        for n, iid in enumerate(spine, 1):
            href = manifest.get(iid, ("", ""))[0]
            if not href:
                continue
            full = _epub_join(base, href)
            if full not in names:
                full = href
            if full not in names:
                continue
            try:
                if z.getinfo(full).file_size > 20 * 1024 * 1024:
                    continue
            except KeyError:
                continue
            raw = z.read(full).decode("utf-8", "replace")
            text, anchors, heads = _html_blocks(raw)
            heads = [h for h in heads if h["title"]]

            entries: list[tuple[int, dict]] = []
            for e in by_file.get(full, []):
                if e["frag"]:
                    at = anchors.get(e["frag"])
                    if at is None:
                        # U4：目录锚点写成 name= 或纯 #frag 时，旧代码默认
                        # 落到 0；多个 0 又被去重成只剩一章，整章正文静默
                        # 消失。先按标题在正文里定位，找不到就跳过这项，
                        # 让整段文本走「按标题切章 / 并入当前章」。
                        at = text.find(e["title"]) if e["title"] else -1
                        if at < 0:
                            continue
                else:
                    at = 0
                entries.append((at, e))
            entries.sort(key=lambda x: x[0])
            uniq: list[tuple[int, dict]] = []
            seen_pairs: set[tuple[int, str]] = set()
            for at, e in entries:
                key = (at, e.get("title", ""))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                uniq.append((at, e))

            if not text.strip():
                # 图片页也保留目录项，至少让人知道「这里有一节」
                if uniq and re.search(r"(?is)<img\b", raw):
                    p = add_part(uniq[0][1]["title"], uniq[0][1]["level"])
                    p["text"] = "〔本節為圖版，暫未提取圖像〕"
                    current = p
                continue

            if uniq:
                lead = text[:uniq[0][0]]
                if lead.strip():
                    if current is None:
                        current = add_part("开篇", 1)
                    place(current, lead.strip(), len(lead) - len(lead.lstrip()), heads)
                for k, (at, e) in enumerate(uniq):
                    end = uniq[k + 1][0] if k + 1 < len(uniq) else len(text)
                    body, cut = _strip_leading_title(text[at:end], e["title"])
                    body2 = body.lstrip()
                    if not body2.strip():
                        # 只有标题、没有正文（如「附錄」总标题）：不建章，层级让给下一项
                        if text[at:end].strip():
                            pending_level = e["level"]
                        continue
                    p = add_part(e["title"], e["level"])
                    place(p, body2.rstrip(), at + cut + (len(body) - len(body2)), heads)
                    current = p
            else:
                if not nav:
                    # 整本书没有目录：按 h1（没有 h1 就用 h2）切章，h3 以下做小节
                    tops = [h for h in heads if h["level"] == 1] or \
                           [h for h in heads if h["level"] == 2]
                    first = tops[0]["offset"] if tops else len(text)
                    lead = text[:first]
                    if lead.strip():
                        if current is None:
                            current = add_part("开篇", 1)
                        place(current, lead.strip(), len(lead) - len(lead.lstrip()), heads)
                    for k, h in enumerate(tops):
                        end = tops[k + 1]["offset"] if k + 1 < len(tops) else len(text)
                        body, cut = _strip_leading_title(text[h["offset"]:end], h["title"])
                        body2 = body.lstrip()
                        if not body2.strip():
                            continue
                        p = add_part(h["title"], min(2, h["level"]))
                        place(p, body2.rstrip(), h["offset"] + cut + (len(body) - len(body2)), heads)
                        current = p
                    if sum(len(p["text"]) for p in parts) >= MAX_DOC_CHARS:
                        break
                    continue
                if current is None:
                    mt2 = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
                    label = re.sub(r"\s+", " ", _html_to_text(mt2.group(1))).strip() if mt2 else ""
                    # 封面/题献这类页面的 <title> 常常就是文件名（BK7080），别拿它当章名
                    if not label or label == Path(full).stem or re.fullmatch(r"[\w\-.]{1,24}", label):
                        label = "开篇" if not parts else f"第 {n} 节"
                    current = add_part(label, 1)
                place(current, text, 0, heads)

            if sum(len(p["text"]) for p in parts) >= MAX_DOC_CHARS:
                break

    out_parts = [(p["title"], p["text"], p["level"], 0, p["sections"]) for p in parts]
    doc, chapters = _build(out_parts)
    return doc, chapters, {"title": title, "author": author}


_OUTLINE_IDENT = re.compile(r"[A-Za-z]{1,3}\.[\w.]{4,}")


def _outline_title_ok(title: str) -> bool:
    """书签标题能不能给人看。

    InDesign / Word 导出的 PDF，书签名常常是排版软件内部的对象名：
    `_headingh.z9qqzdqhy9ye`、`_Ref159955435`、`2406 China Attitudes COVER_S`。
    它们直接进目录就是满屏乱码，得让位给正文首行。
    """
    t = (title or "").strip()
    if not t:
        return False
    if "_" in t:                      # _heading… / _Ref… / COVER_S
        return False
    if _OUTLINE_IDENT.fullmatch(t):   # h.z9qqzdqhy9ye / p.abc123
        return False
    if not re.search(r"[^\W\d_]", t, re.UNICODE):   # 只剩数字和标点
        return False
    return True


def _page_lead_title(pages: list[str], pg: int) -> str:
    """书签不可用时，拿该页正文首行当章名（页眉已在抽取层去掉）。"""
    if not (0 <= pg < len(pages)):
        return ""
    for line in (pages[pg] or "").split("\n"):
        s = line.strip()
        if 2 <= len(s) <= 60 and _outline_title_ok(s):
            return s
    return ""


def _parse_pdf(path: Path) -> tuple[str, list[dict], dict]:
    """PDF：按页读正文（版面已还原：分栏、页眉页脚、双语对照）；
    有书签大纲则按大纲分章，否则按标题/页组切节。"""
    import pdf_text

    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("PDF 已加密，需要密码才能读取")
        except Exception as exc:
            raise ValueError("PDF 已加密，需要密码才能读取") from exc
    meta = {"title": "", "author": ""}
    try:
        meta["title"] = str(reader.metadata.title or "") if reader.metadata else ""
        meta["author"] = str(reader.metadata.author or "") if reader.metadata else ""
    except Exception:
        pass

    # 每页保留独立文字：分章要按页跳转，拼成一整篇就没法定位了。
    pages, _used = pdf_text.extract_pdf_pages(reader, MAX_DOC_CHARS)
    # 乱码页 / 空页不写进 doc，但保留为空占位：大纲里的页码是原书页号，
    # 过滤掉空页会把后面的书签整体前移，章节全落到错的页上。空串 join 后
    # 被 strip 掉，纯扫描件仍不会产出非空 doc，也不会污染 FTS。
    pages = [(p.strip() if p and p.strip() and not pdf_text.looks_garbled(p) else "")
             for p in pages]

    outlines: list[tuple[int, str, int]] = []
    try:
        def walk(items, depth=1):
            for it in items or []:
                if isinstance(it, list):
                    walk(it, min(depth + 1, 3))
                else:
                    try:
                        pg = reader.get_destination_page_number(it)
                        outlines.append((pg, str(it.title or "").strip(), depth))
                    except Exception:
                        pass
        walk(reader.outline)
    except Exception:
        pass

    # 大纲页号可能倒退（附录书签排前面）、也可能同页挂多个书签。
    # 按页聚合后再切，切片区间就不会重叠或整章重复；首个书签之前
    # 的封面/目录页也不再被无声丢掉。
    by_page: dict[int, list[tuple[str, int]]] = {}
    for pg, title, level in outlines:
        try:
            pg = max(0, min(int(pg), max(0, len(pages) - 1)))
        except (TypeError, ValueError):
            continue
        by_page.setdefault(pg, []).append((title, level))
    clean: list[tuple[int, str, int]] = []
    for pg in sorted(by_page):
        items = by_page[pg]
        # 同一个书签里可能一半是内部对象名、一半是真标题，逐个过滤再拼
        good = [t.strip() for t, _ in items if _outline_title_ok(t)]
        title = " / ".join(good) or _page_lead_title(pages, pg) or f"第 {pg + 1} 页"
        clean.append((pg, title, min(l for _, l in items)))

    parts: list[tuple] = []
    if clean:
        first = clean[0][0]
        lead = "\n\n".join(pages[:first]).strip()
        if lead:
            parts.append(("开篇", lead, 1, 0))
        for i, (pg, title, level) in enumerate(clean):
            end = clean[i + 1][0] if i + 1 < len(clean) else len(pages)
            text = "\n\n".join(pages[pg:max(pg + 1, end)]).strip()
            if text:
                parts.append((title, text, min(3, max(1, level)), pg + 1))
    else:
        joined = "\n\n".join(pages).strip()
        if joined:
            parts = _split_by_headings(joined, "正文")
    doc, chapters = _build(parts)
    if not chapters and pages:
        doc, chapters = _build([("正文", "\n\n".join(pages).strip(), 1, 0)])
    return doc, chapters, meta


def _parse_plain(path: Path, suffix: str, fallback_text: str = "") -> tuple[str, list[dict], dict]:
    """文档类：先用自己的 HTML/文本清洗，再按标题分章。"""
    text = ""
    if suffix in {".html", ".htm", ".xhtml"}:
        text = _html_to_text(path.read_bytes().decode("utf-8", "replace"))
    elif suffix in ZIP_DOC_EXTS or suffix not in TEXT_EXTS:
        # docx/pptx/xlsx 等是 zip 包，不能按纯文本读；交给应用自带的解析器
        try:
            fn = _CTX.get("extract_file_text")
            text = fn(path, suffix)[0] if fn else fallback_text
        except Exception:
            text = fallback_text
        text = text or fallback_text
    else:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_bytes().decode("gb18030", "replace")
        text = text or fallback_text
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    parts = _split_by_headings(text[:MAX_DOC_CHARS], "正文")
    doc, chapters = _build(parts)
    return doc, chapters, {}


def parse_material(path: Path, suffix: str, *, fallback_text: str = "") -> tuple[str, list[dict], dict]:
    """统一入口。返回 (doc_text, chapters, meta)。永不抛到路由外。"""
    suffix = (suffix or path.suffix or "").lower()
    if suffix and not suffix.startswith("."):
        # 上游把 "pdf" 传进来时要能认出来；以前会静默落到 plain 分支，
        # 结果抽出一篇空文档，还看不出是哪一环错了。
        suffix = "." + suffix
    if suffix == ".epub":
        return _parse_epub(path)
    if suffix == ".pdf":
        return _parse_pdf(path)
    return _parse_plain(path, suffix, fallback_text)


def classify(suffix: str, source_type: str = "file") -> tuple[str, str]:
    if suffix in AUDIO_EXTS:
        return "audio", suffix.lstrip(".").lower()
    if suffix in VIDEO_EXTS:
        return "video", suffix.lstrip(".").lower()
    if suffix in BOOK_EXTS:
        return "book", suffix.lstrip(".").lower()
    if source_type != "file":
        return "web", suffix.lstrip(".").lower() or "web"
    return "doc", suffix.lstrip(".").lower()


# ══════════════════════════════════════════════════════════
# FTS 索引
# ══════════════════════════════════════════════════════════

def reindex(conn, cid: int) -> int:
    """按书重建索引：章节正文 + 标注 + 笔记 + 书签 + 卡片元信息。"""
    ensure(conn)
    conn.execute("DELETE FROM reading_fts WHERE clip_id=?", (str(cid),))
    item = _item_row(conn, cid)
    clip = conn.execute("SELECT title,summary,excerpt,tags FROM clips WHERE id=?", (cid,)).fetchone()
    if not item or not clip:
        return 0
    doc = item["doc_text"] or ""
    n = 0

    def put(kind, ref_id, offset, title, body):
        nonlocal n
        if not (title or body):
            return
        conn.execute(
            """INSERT INTO reading_fts(kind,clip_id,ref_id,offset,title,body)
               VALUES (?,?,?,?,?,?)""",
            (kind, str(cid), str(ref_id), str(int(offset or 0)),
             str(title or "")[:300], str(body or "")[:60_000]))
        n += 1

    put("meta", 0, 0, clip["title"] or "",
        " ".join(x for x in [clip["summary"], clip["excerpt"], clip["tags"]] if x))
    for ch in _j(item["chapters"], []):
        put("chapter", ch.get("id", ""), ch.get("start", 0),
            ch.get("title", ""), doc[int(ch.get("start", 0)):int(ch.get("end", 0))])
    for a in conn.execute(
            "SELECT * FROM clip_annotations WHERE clip_id=? ORDER BY start_offset", (cid,)):
        put("annotation", a["id"], a["start_offset"],
            "标注 · " + (a["quote"] or "")[:60],
            (a["quote"] or "") + "\n" + (a["note"] or ""))
    for r in conn.execute("SELECT * FROM reading_notes WHERE clip_id=?", (cid,)):
        put("note", r["id"], 0, r["title"] or "笔记",
            (r["source_quote"] or "") + "\n" + (r["content_md"] or "") + "\n" + (r["tags"] or ""))
    for b in conn.execute("SELECT * FROM reading_bookmarks WHERE clip_id=?", (cid,)):
        put("bookmark", b["id"], b["offset"], b["label"] or "书签", b["note"] or "")
    return n


# ══════════════════════════════════════════════════════════
# 视图辅助
# ══════════════════════════════════════════════════════════

def _item_dict(conn, row, with_text=False) -> dict:
    d = dict(row)
    d["chapters"] = _j(d.get("chapters"), [])
    d["meta"] = _j(d.get("meta"), {})
    if not with_text:
        d.pop("doc_text", None)          # 列表不回传整本书，省几 MB
    clip = conn.execute("""SELECT id,title,summary,excerpt,tags,file_name,file_size,
                                  mime_type,source_type,board_id,tier,grade,created_at
                           FROM clips WHERE id=?""", (d["clip_id"],)).fetchone()
    d["clip"] = dict(clip) if clip else None
    if d["clip"]:
        d["clip"]["tags"] = _j(d["clip"].get("tags"), [])
    ann = conn.execute(
        "SELECT COUNT(*) n FROM clip_annotations WHERE clip_id=?", (d["clip_id"],)).fetchone()
    notes = conn.execute(
        "SELECT COUNT(*) n FROM reading_notes WHERE clip_id=?", (d["clip_id"],)).fetchone()
    marks = conn.execute(
        "SELECT COUNT(*) n FROM reading_bookmarks WHERE clip_id=?", (d["clip_id"],)).fetchone()
    links = conn.execute(
        "SELECT COUNT(*) n FROM reading_links WHERE clip_id=?", (d["clip_id"],)).fetchone()
    d["counts"] = {"annotations": ann["n"], "notes": notes["n"],
                   "bookmarks": marks["n"], "links": links["n"]}
    return d


def _shelf_dict(row, counts=None) -> dict:
    d = dict(row)
    d["count"] = (counts or {}).get(d["id"], 0)
    return d


def _shelf_counts(conn) -> dict:
    return {r["shelf_id"]: r["n"] for r in conn.execute(
        """SELECT shelf_id, COUNT(*) n FROM reading_items
           WHERE shelf_id IS NOT NULL GROUP BY shelf_id""")}


# ══════════════════════════════════════════════════════════
# GET
# ══════════════════════════════════════════════════════════

def api_overview(conn, qs):
    ensure(conn)
    shelves = conn.execute("SELECT * FROM reading_shelves ORDER BY sort,id").fetchall()
    counts = _shelf_counts(conn)
    stat = conn.execute(
        """SELECT COUNT(*) total,
                  COALESCE(SUM(CASE WHEN status='reading' THEN 1 ELSE 0 END),0) reading,
                  COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END),0) done,
                  COALESCE(SUM(CASE WHEN progress>=100 THEN 1 ELSE 0 END),0) finished,
                  COALESCE(SUM(read_seconds),0) seconds,
                  COALESCE(SUM(char_count),0) chars
           FROM reading_items""").fetchone()
    recent = conn.execute(
        """SELECT ri.* FROM reading_items ri
           ORDER BY COALESCE(NULLIF(ri.last_read_at,''), ri.added_at) DESC LIMIT 6""").fetchall()
    sessions = conn.execute(
        """SELECT day, SUM(seconds) seconds, SUM(chars) chars
           FROM reading_sessions GROUP BY day ORDER BY day DESC LIMIT 120""").fetchall()
    ann = conn.execute(
        """SELECT COUNT(*) n FROM clip_annotations a
           JOIN reading_items ri ON ri.clip_id=a.clip_id""").fetchone()["n"]
    notes = conn.execute("SELECT COUNT(*) n FROM reading_notes").fetchone()["n"]
    links = conn.execute("SELECT COUNT(*) n FROM reading_links").fetchone()["n"]
    return {"ok": True,
            "stats": {**dict(stat), "annotations": ann, "notes": notes, "links": links},
            "shelves": [_shelf_dict(r, counts) for r in shelves],
            "recent": [_item_dict(conn, r) for r in recent],
            "sessions": [dict(r) for r in sessions],
            "ai": _ai_status()}


def api_items(conn, qs):
    ensure(conn)
    shelf = (qs.get("shelf") or [""])[0].strip()
    kind = (qs.get("kind") or [""])[0].strip()
    status = (qs.get("status") or [""])[0].strip()
    q = (qs.get("q") or [""])[0].strip()
    try:
        limit = min(200, max(1, int((qs.get("limit") or ["60"])[0])))
    except ValueError:
        limit = 60
    where, args = ["1=1"], []
    if shelf == "none":
        where.append("ri.shelf_id IS NULL")
    elif shelf:
        where.append("ri.shelf_id=?"); args.append(shelf)
    if kind in ITEM_KINDS:
        where.append("ri.kind=?"); args.append(kind)
    if status in {"unread", "reading", "done", "paused"}:
        where.append("ri.status=?"); args.append(status)
    if q:
        where.append("(c.title LIKE ? ESCAPE '\\' OR c.tags LIKE ? ESCAPE '\\' "
                     "OR c.summary LIKE ? ESCAPE '\\')")
        args.extend([_like(q)] * 3)
    rows = conn.execute(
        f"""SELECT ri.* FROM reading_items ri JOIN clips c ON c.id=ri.clip_id
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(NULLIF(ri.last_read_at,''), ri.added_at) DESC LIMIT ?""",
        (*args, limit)).fetchall()
    return {"ok": True, "items": [_item_dict(conn, r) for r in rows],
            "total": len(rows), "shelves": [_shelf_dict(r, _shelf_counts(conn))
                                            for r in conn.execute(
                                                "SELECT * FROM reading_shelves ORDER BY sort,id")]}


def api_shelves(conn, qs):
    ensure(conn)
    counts = _shelf_counts(conn)
    rows = conn.execute("SELECT * FROM reading_shelves ORDER BY sort,id").fetchall()
    return {"ok": True, "shelves": [_shelf_dict(r, counts) for r in rows],
            "unshelved": conn.execute(
                "SELECT COUNT(*) n FROM reading_items WHERE shelf_id IS NULL").fetchone()["n"]}


def api_book(conn, qs, cid):
    """打开一本书：正文、目录、标注、书签、笔记、关联、进度一次给全。"""
    ensure(conn)
    cid = _int_id(cid)
    item = _item_row(conn, cid)
    if not item:
        return _err("这份资料不在阅读中心")
    doc = ensure_text(conn, cid)
    conn.commit()                     # GET 路由不会自动提交，正文自愈要落盘
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not clip:
        return _err("原件记录不存在")
    annotations = conn.execute(
        "SELECT * FROM clip_annotations WHERE clip_id=? ORDER BY start_offset,id", (cid,)).fetchall()
    bookmarks = conn.execute(
        "SELECT * FROM reading_bookmarks WHERE clip_id=? ORDER BY offset,id", (cid,)).fetchall()
    notes = conn.execute(
        "SELECT * FROM reading_notes WHERE clip_id=? ORDER BY id DESC", (cid,)).fetchall()
    links = conn.execute(
        """SELECT l.*, c.title AS dst_clip_title
           FROM reading_links l LEFT JOIN clips c
             ON l.dst_kind='clip' AND CAST(l.dst_id AS INTEGER)=c.id
           WHERE l.clip_id=? ORDER BY l.id DESC""", (cid,)).fetchall()
    backlinks = conn.execute(
        """SELECT * FROM reading_links
           WHERE dst_kind='clip' AND dst_id=? ORDER BY id DESC""", (str(cid),)).fetchall()
    chapters = _j(item["chapters"], [])
    cur_ch = ""
    for ch in chapters:
        if ch.get("start", 0) <= (item["position"] or 0) < ch.get("end", 0):
            cur_ch = ch.get("id", "")
            break
    return {"ok": True,
            "item": _item_dict(conn, item, with_text=False),
            "doc_text": doc,
            "chapters": chapters,
            "clip": {k: clip[k] for k in clip.keys()
                     if k not in {"content_text", "file_path", "body_images"}},
            "annotations": [dict(a) for a in annotations],
            "bookmarks": [dict(b) for b in bookmarks],
            "notes": [dict(n) for n in notes],
            "links": [dict(l) for l in links],
            "backlinks": [dict(l) for l in backlinks],
            "current_chapter": cur_ch or (chapters[0]["id"] if chapters else ""),
            "boards": [dict(b) for b in conn.execute("SELECT id,name,kind FROM boards ORDER BY sort,id")],
            "claims": [dict(c) for c in conn.execute(
                "SELECT code,statement,domain FROM claims WHERE status='active' ORDER BY id DESC LIMIT 300")],
            "ai": _ai_status()}


def api_notes(conn, qs):
    ensure(conn)
    q = (qs.get("q") or [""])[0].strip()
    kind = (qs.get("kind") or [""])[0].strip()
    where, args = ["1=1"], []
    if kind in NOTE_KINDS:
        where.append("n.kind=?"); args.append(kind)
    if q:
        where.append("(n.title LIKE ? ESCAPE '\\' OR n.content_md LIKE ? ESCAPE '\\' "
                     "OR n.source_quote LIKE ? ESCAPE '\\')")
        args.extend([_like(q)] * 3)
    rows = conn.execute(
        f"""SELECT n.*, c.title AS book_title FROM reading_notes n
            JOIN clips c ON c.id=n.clip_id WHERE {' AND '.join(where)}
            ORDER BY n.id DESC LIMIT 300""", args).fetchall()
    return {"ok": True, "notes": [dict(r) for r in rows]}


def api_links(conn, qs):
    ensure(conn)
    cid = (qs.get("clip_id") or [""])[0].strip()
    dst_kind = (qs.get("dst_kind") or [""])[0].strip()
    dst_id = (qs.get("dst_id") or [""])[0].strip()
    if dst_kind and dst_id:
        rows = conn.execute(
            "SELECT * FROM reading_links WHERE dst_kind=? AND dst_id=? ORDER BY id DESC",
            (dst_kind, dst_id)).fetchall()
    elif cid:
        rows = conn.execute(
            "SELECT * FROM reading_links WHERE clip_id=? ORDER BY id DESC", (cid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reading_links ORDER BY id DESC LIMIT 300").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for side in ("src", "dst"):
            if d[f"{side}_kind"] == "clip":
                hit = conn.execute("SELECT title FROM clips WHERE id=?",
                                   (d[f"{side}_id"],)).fetchone()
                d[f"{side}_title"] = hit["title"] if hit else ""
            elif d[f"{side}_kind"] == "claim":
                hit = conn.execute("SELECT statement FROM claims WHERE code=?",
                                   (d[f"{side}_id"],)).fetchone()
                d[f"{side}_title"] = hit["statement"] if hit else ""
            elif d[f"{side}_kind"] == "board":
                hit = conn.execute("SELECT name FROM boards WHERE id=?",
                                   (d[f"{side}_id"],)).fetchone()
                d[f"{side}_title"] = hit["name"] if hit else ""
            elif d[f"{side}_kind"] == "annotation":
                hit = conn.execute("SELECT quote FROM clip_annotations WHERE id=?",
                                   (d[f"{side}_id"],)).fetchone()
                d[f"{side}_title"] = (hit["quote"] if hit else "")[:80]
            elif d[f"{side}_kind"] == "note":
                hit = conn.execute("SELECT title FROM reading_notes WHERE id=?",
                                   (d[f"{side}_id"],)).fetchone()
                d[f"{side}_title"] = hit["title"] if hit else ""
            else:
                d[f"{side}_title"] = ""
        out.append(d)
    return {"ok": True, "links": out}


def api_search(conn, qs):
    """正文 + 批注 + 笔记 + 书签 的统一全文检索（带跳转坐标）。"""
    ensure(conn)
    q = (qs.get("q") or [""])[0].strip()
    scope = (qs.get("scope") or ["all"])[0].strip()
    shelf = (qs.get("shelf") or [""])[0].strip()
    try:
        limit = min(120, max(1, int((qs.get("limit") or ["60"])[0])))
    except ValueError:
        limit = 60
    if not q:
        return {"ok": True, "q": "", "total": 0, "results": []}

    terms = [t for t in re.split(r"\s+", q) if t][:6]
    allowed = ({scope} if scope in {"chapter", "annotation", "note", "bookmark", "meta"} else None)
    shelf_clause, shelf_args = "", []
    if shelf:
        shelf_clause = " AND ri.shelf_id=?" if shelf != "none" else " AND ri.shelf_id IS NULL"
        if shelf != "none":
            shelf_args = [shelf]
    hits: list[dict] = []
    seen = set()

    long_ok = all(len(t) >= 3 for t in terms)
    engine = "like"
    if long_ok:
        expr = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
        try:
            rows = conn.execute(
                """SELECT f.kind,f.clip_id,f.ref_id,f.offset,f.title,f.body,
                          c.title AS book_title, ri.chapters
                   FROM reading_fts f
                   JOIN clips c ON c.id=CAST(f.clip_id AS INTEGER)
                   JOIN reading_items ri ON ri.clip_id=CAST(f.clip_id AS INTEGER)
                   WHERE reading_fts MATCH ?""" + shelf_clause + " LIMIT 400",
                (expr, *shelf_args)).fetchall()
            for r in rows:
                if allowed and r["kind"] not in allowed:
                    continue
                key = (r["kind"], r["clip_id"], r["ref_id"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(_hit(conn, r, terms))
            if rows:
                engine = "fts5-trigram"
        except Exception:
            hits = []

    if not hits:
        # 短词（<3 字，trigram 无三元组）或 FTS 异常：直接扫源表，
        # 且按「章」而不是按「整本」给跳转坐标，结果形状与 FTS 一致。
        low_q = q.lower()
        item_sql = ("SELECT ri.*, c.title AS clip_title FROM reading_items ri "
                    "JOIN clips c ON c.id=ri.clip_id WHERE 1=1" + shelf_clause)
        for item in conn.execute(item_sql, shelf_args).fetchall():
            if allowed and "chapter" not in allowed:
                break
            doc = item["doc_text"] or ""
            for ch in _j(item["chapters"], []):
                seg = doc[int(ch.get("start", 0)):int(ch.get("end", 0))]
                if low_q not in seg.lower():
                    continue
                key = ("chapter", item["clip_id"], ch.get("id", ""))
                if key in seen:
                    continue
                seen.add(key)
                hits.append(_hit(conn, {
                    "kind": "chapter", "clip_id": item["clip_id"],
                    "ref_id": ch.get("id", ""), "offset": ch.get("start", 0),
                    "title": ch.get("title", ""), "body": seg,
                    "book_title": item["clip_title"], "chapters": item["chapters"],
                }, terms))
        # 标注 / 笔记 / 书签：列名全部带表别名，避免与 clips.title 撞成歧义
        for spec in (("annotation", "clip_annotations",
                      "COALESCE(t.quote,'') || ' ' || COALESCE(t.note,'')", "t.start_offset"),
                     ("note", "reading_notes",
                      "COALESCE(t.title,'') || ' ' || COALESCE(t.source_quote,'') || ' ' || COALESCE(t.content_md,'')", "0"),
                     ("bookmark", "reading_bookmarks",
                      "COALESCE(t.label,'') || ' ' || COALESCE(t.note,'')", "t.offset")):
            kind, table, body, off = spec
            if allowed and kind not in allowed:
                continue
            try:
                rows = conn.execute(
                    f"""SELECT '{kind}' AS kind, t.clip_id, CAST(t.id AS TEXT) AS ref_id,
                               {off} AS offset, c.title AS title, {body} AS body,
                               c.title AS book_title, ri.chapters AS chapters
                        FROM {table} t JOIN clips c ON c.id=t.clip_id
                        JOIN reading_items ri ON ri.clip_id=t.clip_id
                        WHERE ({body}) LIKE ? ESCAPE '\\'{shelf_clause} LIMIT 200""",
                    (_like(q), *shelf_args)).fetchall()
            except Exception:
                continue
            for r in rows:
                key = (kind, r["clip_id"], r["ref_id"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(_hit(conn, r, terms))

    hits.sort(key=lambda h: (-h["score"], h["clip_id"], h["offset"]))
    return {"ok": True, "q": q, "scope": scope, "total": len(hits),
            "results": hits[:limit], "engine": engine}


def _hit(conn, r, terms) -> dict:
    """把一行命中整理成可跳转的检索结果。"""
    body = str(r["body"] or "")
    flat = re.sub(r"\s+", " ", body)
    low = flat.lower()
    pos = min((p for p in [low.find(t.lower()) for t in terms] if p >= 0), default=-1)
    start = max(0, pos - 46) if pos >= 0 else 0
    snippet = ("…" if start else "") + flat[start:start + 150] + ("…" if start + 150 < len(flat) else "")
    score = sum(min(low.count(t.lower()), 8) for t in terms)
    cid = int(r["clip_id"])
    chapters = _j(r["chapters"], []) if "chapters" in r.keys() else []
    off = int(float(r["offset"] or 0))
    ch_id, ch_title = "", ""
    for ch in chapters:
        if int(ch.get("start", 0)) <= off < int(ch.get("end", 0)):
            ch_id, ch_title = ch.get("id", ""), ch.get("title", "")
            break
    if not ch_id and chapters:
        ch_id, ch_title = chapters[0].get("id", ""), chapters[0].get("title", "")
    return {"kind": r["kind"], "clip_id": cid, "book_title": r["book_title"] or "",
            "ref_id": str(r["ref_id"] or ""), "offset": off,
            "chapter_id": ch_id, "chapter_title": ch_title,
            "title": str(r["title"] or "")[:120], "snippet": snippet, "score": score}


def api_stats(conn, qs):
    ensure(conn)
    days = conn.execute(
        """SELECT day, SUM(seconds) seconds, SUM(chars) chars, COUNT(DISTINCT clip_id) books
           FROM reading_sessions GROUP BY day ORDER BY day DESC LIMIT 180""").fetchall()
    by_kind = conn.execute(
        "SELECT kind, COUNT(*) n FROM reading_items GROUP BY kind").fetchall()
    by_status = conn.execute(
        "SELECT status, COUNT(*) n FROM reading_items GROUP BY status").fetchall()
    top = conn.execute(
        """SELECT ri.clip_id, c.title, ri.read_seconds, ri.progress
           FROM reading_items ri JOIN clips c ON c.id=ri.clip_id
           ORDER BY ri.read_seconds DESC LIMIT 10""").fetchall()
    return {"ok": True, "days": [dict(r) for r in days],
            "by_kind": [dict(r) for r in by_kind],
            "by_status": [dict(r) for r in by_status],
            "top": [dict(r) for r in top]}


def api_export(conn, qs, cid):
    ensure(conn)
    cid = _int_id(cid)
    item = _item_row(conn, cid)
    if not item:
        return _err("这份资料不在阅读中心")
    clip = conn.execute("SELECT title FROM clips WHERE id=?", (cid,)).fetchone()
    out = [f"# {clip['title'] if clip else '未命名'}", "",
           f"> 导出自观澜 · 阅读中心 · {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    chapters = _j(item["chapters"], [])
    if chapters:
        out += ["## 目录", ""] + [f"- {c['title']}" for c in chapters] + [""]
    anns = conn.execute(
        "SELECT * FROM clip_annotations WHERE clip_id=? ORDER BY start_offset", (cid,)).fetchall()
    if anns:
        out += ["## 高亮与批注", ""]
        for a in anns:
            out.append(f"### [{a['color']}] {a['quote'][:120]}")
            if a["note"]:
                out += ["", a["note"]]
            out.append("")
    notes = conn.execute("SELECT * FROM reading_notes WHERE clip_id=? ORDER BY id", (cid,)).fetchall()
    if notes:
        out += ["## 笔记", ""]
        for n in notes:
            out += [f"### {n['title'] or '无标题'}", ""]
            if n["source_quote"]:
                out += ["> " + n["source_quote"].replace("\n", "\n> "), ""]
            out += [n["content_md"], ""]
    marks = conn.execute("SELECT * FROM reading_bookmarks WHERE clip_id=? ORDER BY offset", (cid,)).fetchall()
    if marks:
        out += ["## 书签", ""] + [f"- {m['label'] or '书签'} @ {int(m['offset'])}"
                                  + (f" · {m['note']}" if m["note"] else "") for m in marks] + [""]
    links = conn.execute("SELECT * FROM reading_links WHERE clip_id=? ORDER BY id", (cid,)).fetchall()
    if links:
        out += ["## 关联", ""] + [f"- {l['src_kind']}#{l['src_id']} → {l['dst_kind']}#{l['dst_id']} ({l['relation']})"
                                  for l in links] + [""]
    return {"ok": True, "markdown": "\n".join(out), "filename":
            re.sub(r"[\\/:*?\"<>|]", "_", clip["title"] if clip else "阅读笔记")[:60] + ".md"}


# ══════════════════════════════════════════════════════════
# POST
# ══════════════════════════════════════════════════════════

def api_item_add(conn, qs, body):
    """把一条已有收存 / 刚上传的文件纳入阅读中心，并做结构化解析。"""
    ensure(conn)
    try:
        cid = int(body.get("clip_id"))
    except (TypeError, ValueError):
        return _err("clip_id 无效")
    if not _clip_exists(conn, cid):
        return _err("这条收存不存在")
    if _item_row(conn, cid):
        return {"ok": True, "duplicate": True, "item": _item_dict(conn, _item_row(conn, cid))}

    clip = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    suffix = Path(clip["file_name"] or "").suffix.lower()[:12]
    kind, fmt = classify(suffix, clip["source_type"] or "file")
    if body.get("kind") in ITEM_KINDS:
        kind = body["kind"]

    doc, chapters, meta = "", [], {}
    parse_message = ""
    stored = clip["file_path"] or ""
    if stored and Path(stored).name == stored:
        path = _CTX["UPLOAD_DIR"] / stored
        if path.is_file():
            try:
                doc, chapters, meta = parse_material(
                    path, suffix, fallback_text=clip["content_text"] or "")
            except Exception as exc:
                parse_message = f"结构化解析失败，已保留原件（{type(exc).__name__}）"
        else:
            parse_message = "原件不在本机，只登记了元信息"
    elif clip["content_text"]:
        doc0 = re.sub(r"\r\n?", "\n", clip["content_text"])[:MAX_DOC_CHARS]
        doc, chapters = _build(_split_by_headings(doc0, "正文"))
    if not doc and not parse_message:
        parse_message = ("这份资料没有文字层；音视频可在「文字稿」里粘贴内容，"
                         "之后同样能做高亮、书签与检索")

    shelf_id = body.get("shelf_id")
    try:
        shelf_id = int(shelf_id) if shelf_id not in (None, "", "none") else None
    except (TypeError, ValueError):
        shelf_id = None
    if shelf_id and not conn.execute("SELECT 1 FROM reading_shelves WHERE id=?", (shelf_id,)).fetchone():
        shelf_id = None

    meta_out = {"author": meta.get("author", ""), "publisher": "", "language": "",
                "original_title": meta.get("title", "")}
    if isinstance(_j(body.get("meta"), {}), dict):
        meta_out.update(_j(body.get("meta"), {}))
    status = "unread" if not body.get("status") else str(body["status"])
    if status not in {"unread", "reading", "done", "paused"}:
        status = "unread"
    position_unit = "second" if kind in {"audio", "video"} else "char"

    conn.execute(
        """INSERT INTO reading_items
           (clip_id,kind,format,shelf_id,doc_text,chapters,char_count,status,
            position_unit,added_at,meta)
           VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'),?)""",
        (cid, kind, fmt, shelf_id, doc, json.dumps(chapters, ensure_ascii=False),
         len(doc), status, position_unit, json.dumps(meta_out, ensure_ascii=False)))
    _write_clip_text(conn, cid, doc)
    if meta.get("title") and not (clip["title"] or "").strip():
        conn.execute("UPDATE clips SET title=? WHERE id=?", (meta["title"], cid))
    if meta.get("author"):
        conn.execute("""UPDATE clips SET tags=? WHERE id=?""",
                     (json.dumps(_clean_tags([*_j(clip["tags"], []), meta["author"]]),
                                 ensure_ascii=False), cid))
    reindex(conn, cid)
    conn.commit()
    return {"ok": True, "duplicate": False, "parse_message": parse_message,
            "item": _item_dict(conn, _item_row(conn, cid)),
            "chapters": len(chapters), "chars": len(doc)}


def api_item_update(conn, qs, body, cid):
    ensure(conn)
    cid = _int_id(cid)
    if not _item_row(conn, cid):
        return _err("这份资料不在阅读中心")
    sets, args = [], []
    for key, col in (("status", "status"), ("shelf_id", "shelf_id"),
                     ("progress", "progress"), ("duration", "duration")):
        if key in body:
            val = body[key]
            if key == "status" and val not in {"unread", "reading", "done", "paused"}:
                continue
            if key in {"shelf_id"}:
                val = None if val in ("", "none", None) else val
            if key in {"progress", "duration"}:
                # 必须落库前就转成数字：REAL 列存 TEXT 时，SQLite 的类型亲和性
                # 会保留原字符串，之后 api_progress 的 float() 直接抛 ValueError，
                # 这一本书就再也存不进进度了。
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
                val = max(0.0, val)
                if key == "progress":
                    val = min(100.0, val)
            sets.append(f"{col}=?"); args.append(val)
    if "meta" in body and isinstance(body["meta"], dict):
        old = _j(_item_row(conn, cid)["meta"], {})
        old.update(body["meta"])
        sets.append("meta=?"); args.append(json.dumps(old, ensure_ascii=False))
    if "title" in body:
        conn.execute("UPDATE clips SET title=?, updated_at=datetime('now','localtime') WHERE id=?",
                     (str(body["title"]).strip()[:180] or "未命名", cid))
        reindex(conn, cid)     # 书名进了 FTS 的 meta 行，改名不重建索引就搜不到新名
    if "tags" in body:
        conn.execute("UPDATE clips SET tags=? WHERE id=?",
                     (json.dumps(_clean_tags(body["tags"]), ensure_ascii=False), cid))
        reindex(conn, cid)
    if sets:
        conn.execute(f"UPDATE reading_items SET {', '.join(sets)} WHERE clip_id=?",
                     (*args, cid))
    conn.commit()
    return {"ok": True, "item": _item_dict(conn, _item_row(conn, cid))}


def api_item_remove(conn, qs, body, cid):
    """移出阅读中心：只删阅读态，原件 / 收存卡片 / 知识卡都留着。"""
    ensure(conn)
    cid = _int_id(cid)
    if not _item_row(conn, cid):
        return _err("这份资料不在阅读中心")
    for table in ("reading_bookmarks", "reading_notes", "reading_links", "reading_sessions"):
        conn.execute(f"DELETE FROM {table} WHERE clip_id=?", (cid,))
    conn.execute("DELETE FROM reading_fts WHERE clip_id=?", (str(cid),))
    conn.execute("DELETE FROM reading_items WHERE clip_id=?", (cid,))
    conn.commit()
    return {"ok": True}


def api_transcript(conn, qs, body, cid):
    """给音视频 / 无文字层的书补文字稿，重建目录与索引。"""
    ensure(conn)
    cid = _int_id(cid)
    item = _item_row(conn, cid)
    if not item:
        return _err("这份资料不在阅读中心")
    text = re.sub(r"\r\n?", "\n", str(body.get("text") or ""))[:MAX_DOC_CHARS].strip()
    doc, chapters = _build(_split_by_headings(text, "文字稿")) if text else ("", [])
    conn.execute("""UPDATE reading_items SET doc_text=?, chapters=?, char_count=?,
                    position=0, progress=0 WHERE clip_id=?""",
                 (doc, json.dumps(chapters, ensure_ascii=False), len(doc), cid))
    _write_clip_text(conn, cid, doc, trusted=True)
    reindex(conn, cid)
    conn.commit()
    return {"ok": True, "chapters": len(chapters), "chars": len(doc)}


def api_reparse(conn, qs, body, cid):
    """按当前解析器重建目录与正文（原件不在本机的条目无法重解析）。

    正文重排后字符偏移会变，进度按百分比折算，不把人打回第一页；
    高亮与批注按原句重新锚定，书签偏移可能略有漂移。
    """
    ensure(conn)
    cid = _int_id(cid)
    item = _item_row(conn, cid)
    if not item:
        return _err("这份资料不在阅读中心")
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not clip:
        return _err("原件记录不存在")
    stored = clip["file_path"] or ""
    path = _CTX["UPLOAD_DIR"] / stored if stored and Path(stored).name == stored else None
    if not path or not path.is_file():
        return _err("原件不在本机，无法重新解析")
    suffix = Path(clip["file_name"] or path.name).suffix.lower()[:12]
    try:
        doc, chapters, meta = parse_material(path, suffix, fallback_text="")
    except Exception as exc:
        return _err(f"解析失败：{type(exc).__name__}")
    if not doc or not _doc_has_real_text(doc):
        return _err("没有解析出文字层")
    pct = float(item["progress"] or 0)
    pos = int(len(doc) * min(100.0, max(0.0, pct)) / 100)
    conn.execute("""UPDATE reading_items SET doc_text=?, chapters=?, char_count=?,
                    position=?, progress=? WHERE clip_id=?""",
                 (doc, json.dumps(chapters, ensure_ascii=False), len(doc), pos, pct, cid))
    _write_clip_text(conn, cid, doc)
    reindex(conn, cid)
    conn.commit()
    secs = sum(len(c.get("sections") or []) for c in chapters)
    return {"ok": True, "chapters": len(chapters), "sections": secs, "chars": len(doc),
            "position": pos}


def api_shelf_create(conn, qs, body):
    ensure(conn)
    name = str(body.get("name") or "").strip()[:24]
    if not name:
        return _err("书架名不能为空")
    if conn.execute("SELECT 1 FROM reading_shelves WHERE name=?", (name,)).fetchone():
        return _err("已经有同名书架")
    try:
        board_id = int(body.get("board_id")) if body.get("board_id") not in (None, "", "none") else None
    except (TypeError, ValueError):
        board_id = None
    try:
        sort = int(body.get("sort") or 0)
    except (TypeError, ValueError):
        sort = 0
    cur = conn.execute("INSERT INTO reading_shelves(name,board_id,sort) VALUES (?,?,?)",
                       (name, board_id, sort))
    conn.commit()
    row = conn.execute("SELECT * FROM reading_shelves WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "shelf": _shelf_dict(row)}


def api_shelf_delete(conn, qs, body, sid):
    ensure(conn)
    sid = _int_id(sid, "shelf_id")
    conn.execute("UPDATE reading_items SET shelf_id=NULL WHERE shelf_id=?", (sid,))
    conn.execute("DELETE FROM reading_shelves WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


def api_progress(conn, qs, body, cid):
    ensure(conn)
    cid = _int_id(cid)
    item = _item_row(conn, cid)
    if not item:
        return _err("这份资料不在阅读中心")
    try:
        position = max(0, float(body.get("position") or 0))
        seconds = max(0, min(7200, int(body.get("seconds") or 0)))
        chars = max(0, int(body.get("chars") or 0))
        duration = max(0, float(body.get("duration") or item["duration"] or 0))
    except (TypeError, ValueError):
        return _err("进度数据无效")
    if item["position_unit"] == "second":
        # 音视频：分母只能是时长。时长未知时不猜百分比——音视频的 char_count
        # 恒为 0，拿它当分母等于「1 秒 = 100%」，一播就显示读完。
        total = duration
    else:
        total = max(1, item["char_count"])
    pct = min(100.0, round(position / total * 100, 2)) if total else 0
    if body.get("progress") is not None:
        try:
            pct = min(100.0, max(0.0, float(body["progress"])))
        except (TypeError, ValueError):
            pass
    if pct >= 99.5:
        status = "done"
    elif pct > 0:
        status = "reading"
    elif item["status"] == "done":
        # 回到开头：读完了又从头读，得把「读完」撤掉，否则 0% 的书仍算读完
        status = "reading" if position > 0 else "unread"
    else:
        status = item["status"]
    if str(body.get("status") or "") in {"unread", "reading", "done", "paused"}:
        status = body["status"]
    # 播放器拿到元数据时只上报 duration，其余字段都不给。此时 position 会是 0，
    # 上面的公式会把进度算成 0%、把「读完」撤成「未读」——只更新时长却丢进度。
    # 这种载荷一律沿用库里已有的位置/进度/状态，只让 duration 生效。
    only_duration = (
        body.get("position") is None and body.get("progress") is None
        and body.get("seconds") is None and body.get("chars") is None
        and body.get("status") is None and body.get("duration") is not None
    )
    if only_duration:
        position = float(item["position"] or 0)
        pct = float(item["progress"] or 0)
        status = item["status"]
    conn.execute(
        """UPDATE reading_items SET position=?, progress=?, status=?, duration=?,
           read_seconds=read_seconds+?, last_read_at=datetime('now','localtime')
           WHERE clip_id=?""",
        (position, pct, status, duration, seconds, cid))
    if seconds or chars:
        day = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            """INSERT INTO reading_sessions(day,clip_id,seconds,chars) VALUES (?,?,?,?)
               ON CONFLICT(day,clip_id) DO UPDATE SET
                 seconds=seconds+excluded.seconds, chars=chars+excluded.chars""",
            (day, cid, seconds, chars))
    conn.commit()
    return {"ok": True, "progress": pct, "status": status}


def api_bookmark_create(conn, qs, body, cid):
    ensure(conn)
    cid = _int_id(cid)
    if not _item_row(conn, cid):
        return _err("这份资料不在阅读中心")
    try:
        offset = max(0, float(body.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    time_sec = body.get("time_sec")
    label = str(body.get("label") or "").strip()[:80] or "书签"
    note = str(body.get("note") or "").strip()[:2000]
    chapter_id = str(body.get("chapter_id") or "")
    cur = conn.execute(
        """INSERT INTO reading_bookmarks(clip_id,offset,time_sec,chapter_id,label,note)
           VALUES (?,?,?,?,?,?)""", (cid, offset, time_sec, chapter_id, label, note))
    reindex(conn, cid)
    conn.commit()
    row = conn.execute("SELECT * FROM reading_bookmarks WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "bookmark": dict(row)}


def api_bookmark_delete(conn, qs, body, bid):
    ensure(conn)
    bid = _int_id(bid, "bookmark_id")
    row = conn.execute("SELECT clip_id FROM reading_bookmarks WHERE id=?", (bid,)).fetchone()
    conn.execute("DELETE FROM reading_bookmarks WHERE id=?", (bid,))
    # reading_links 没有外键，删了书签得把指向它的关联一起收掉
    conn.execute(
        """DELETE FROM reading_links WHERE (src_kind='bookmark' AND src_id=?)
           OR (dst_kind='bookmark' AND dst_id=?)""",
        (str(bid), str(bid)))
    if row:
        reindex(conn, row["clip_id"])
    conn.commit()
    return {"ok": True}


def api_note_create(conn, qs, body):
    ensure(conn)
    try:
        cid = int(body.get("clip_id"))
    except (TypeError, ValueError):
        return _err("clip_id 无效")
    if not _item_row(conn, cid):
        return _err("这份资料不在阅读中心")
    kind = str(body.get("kind") or "note")
    if kind not in NOTE_KINDS:
        kind = "note"
    cur = conn.execute(
        """INSERT INTO reading_notes
           (clip_id,title,content_md,chapter_id,source_annotation_id,source_quote,kind,tags)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cid, str(body.get("title") or "").strip()[:160],
         str(body.get("content_md") or "")[:60_000],
         str(body.get("chapter_id") or ""), body.get("source_annotation_id"),
         str(body.get("source_quote") or "")[:8_000], kind,
         json.dumps(_clean_tags(body.get("tags")), ensure_ascii=False)))
    reindex(conn, cid)
    conn.commit()
    row = conn.execute("SELECT * FROM reading_notes WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "note": dict(row)}


def api_note_update(conn, qs, body, nid):
    ensure(conn)
    nid = _int_id(nid, "note_id")
    row = conn.execute("SELECT * FROM reading_notes WHERE id=?", (nid,)).fetchone()
    if not row:
        return _err("这条笔记不存在")
    # 显式传 null 要按「清空」处理，不能 str(None) 写成字面量 "None"
    title = str(body.get("title") or "")[:160] if "title" in body else row["title"]
    content = str(body.get("content_md") or "")[:60_000] if "content_md" in body else row["content_md"]
    tags = json.dumps(_clean_tags(body.get("tags") or []), ensure_ascii=False) if "tags" in body else row["tags"]
    conn.execute("""UPDATE reading_notes SET title=?,content_md=?,tags=?,
                    updated_at=datetime('now','localtime') WHERE id=?""",
                 (title, content, tags, nid))
    reindex(conn, row["clip_id"])
    conn.commit()
    return {"ok": True, "note": dict(conn.execute(
        "SELECT * FROM reading_notes WHERE id=?", (nid,)).fetchone())}


def api_note_delete(conn, qs, body, nid):
    ensure(conn)
    nid = _int_id(nid, "note_id")
    row = conn.execute("SELECT clip_id FROM reading_notes WHERE id=?", (nid,)).fetchone()
    conn.execute("DELETE FROM reading_notes WHERE id=?", (nid,))
    # reading_links 没有外键，删了笔记得把指向它的关联一起收掉
    conn.execute(
        """DELETE FROM reading_links WHERE (src_kind='note' AND src_id=?)
           OR (dst_kind='note' AND dst_id=?)""",
        (str(nid), str(nid)))
    if row:
        reindex(conn, row["clip_id"])
    conn.commit()
    return {"ok": True}


def api_link_create(conn, qs, body):
    ensure(conn)
    src_kind, dst_kind = str(body.get("src_kind") or ""), str(body.get("dst_kind") or "")
    src_id, dst_id = str(body.get("src_id") or ""), str(body.get("dst_id") or "")
    relation = str(body.get("relation") or "refs")
    if src_kind not in LINK_KINDS or dst_kind not in LINK_KINDS:
        return _err("引用类型不支持")
    if not src_id or not dst_id:
        return _err("引用两端不能为空")
    if relation not in RELATIONS:
        relation = "refs"
    # 目标必须真的存在，避免知识网络里挂空指针
    if dst_kind == "clip" and not _clip_exists(conn, dst_id):
        return _err("关联的收存不存在")
    if dst_kind == "claim" and not conn.execute(
            "SELECT 1 FROM claims WHERE code=?", (dst_id,)).fetchone():
        return _err("关联的正典命题不存在")
    if dst_kind == "board" and not conn.execute(
            "SELECT 1 FROM boards WHERE id=?", (dst_id,)).fetchone():
        return _err("关联的板块不存在")
    # 其余可指向的对象也要验存在，否则关联面板里会出现空标题的死链
    for kind, table, col in (("annotation", "clip_annotations", "id"),
                             ("bookmark", "reading_bookmarks", "id"),
                             ("note", "reading_notes", "id"),
                             ("excerpt", "reading_notes", "id")):
        if kind == dst_kind and not conn.execute(
                f"SELECT 1 FROM {table} WHERE {col}=?", (dst_id,)).fetchone():
            return _err(f"关联的{kind}不存在")
    try:
        cid = int(body.get("clip_id")) if body.get("clip_id") else None
    except (TypeError, ValueError):
        cid = None
    if cid and not _item_row(conn, cid):
        cid = None
    conn.execute(
        """INSERT OR IGNORE INTO reading_links
           (src_kind,src_id,dst_kind,dst_id,relation,clip_id,note)
           VALUES (?,?,?,?,?,?,?)""",
        (src_kind, src_id, dst_kind, dst_id, relation, cid,
         str(body.get("note") or "")[:500]))
    # 反向边：从知识库一侧也能回溯到这条标注 / 笔记
    inverse = {"refs": "refs", "cites": "cites", "derived_from": "refs",
               "belongs_to": "refs", "supports": "refs"}[relation]
    conn.execute(
        """INSERT OR IGNORE INTO reading_links
           (src_kind,src_id,dst_kind,dst_id,relation,clip_id,note)
           VALUES (?,?,?,?,?,?,?)""",
        (dst_kind, dst_id, src_kind, src_id, inverse, cid,
         str(body.get("note") or "")[:500]))
    conn.commit()
    return {"ok": True}


def api_link_delete(conn, qs, body, lid):
    ensure(conn)
    lid = _int_id(lid, "link_id")
    row = conn.execute("SELECT * FROM reading_links WHERE id=?", (lid,)).fetchone()
    if not row:
        return _err("这条关联不存在")
    conn.execute("DELETE FROM reading_links WHERE id=?", (lid,))
    conn.execute(
        """DELETE FROM reading_links WHERE src_kind=? AND src_id=?
           AND dst_kind=? AND dst_id=?""",
        (row["dst_kind"], row["dst_id"], row["src_kind"], row["src_id"]))
    conn.commit()
    return {"ok": True}


def api_archive(conn, qs, body):
    """汇入知识库：批注 / 摘录 / 笔记 → 一条新的收存卡片，并留下双向引用。"""
    ensure(conn)
    try:
        cid = int(body.get("clip_id"))
    except (TypeError, ValueError):
        return _err("clip_id 无效")
    if not _item_row(conn, cid):
        return _err("这份资料不在阅读中心")
    source_kind = str(body.get("source_kind") or "note")
    quote, note_text, title, src_id = "", "", "", ""

    if source_kind == "annotation":
        a = conn.execute("SELECT * FROM clip_annotations WHERE id=?",
                         (body.get("source_id"),)).fetchone()
        if not a or a["clip_id"] != cid:
            return _err("这条标注不存在")
        quote, note_text = a["quote"], a["note"]
        title = str(body.get("title") or f"摘录 · {quote[:24]}")
        src_id = str(a["id"])
    elif source_kind == "note":
        n = conn.execute("SELECT * FROM reading_notes WHERE id=?",
                         (body.get("source_id"),)).fetchone()
        if not n or n["clip_id"] != cid:
            return _err("这条笔记不存在")
        quote, note_text = n["source_quote"], n["content_md"]
        title = str(body.get("title") or n["title"] or "阅读笔记")
        src_id = str(n["id"])
    elif source_kind == "excerpt":
        quote = str(body.get("quote") or "").strip()[:8_000]
        note_text = str(body.get("content_md") or "").strip()[:60_000]
        if not quote and not note_text:
            return _err("摘录内容不能为空")
        title = str(body.get("title") or f"摘录 · {quote[:24] or '阅读摘录'}")
        cur = conn.execute(
            """INSERT INTO reading_notes
               (clip_id,title,content_md,chapter_id,source_quote,kind,tags)
               VALUES (?,?,?,?,?,'excerpt',?)""",
            (cid, title, note_text, str(body.get("chapter_id") or ""), quote,
             json.dumps(_clean_tags(body.get("tags")), ensure_ascii=False)))
        src_id = str(cur.lastrowid)
        source_kind = "note"
    else:
        return _err("不支持的归档来源")

    book = conn.execute("SELECT title FROM clips WHERE id=?", (cid,)).fetchone()
    body_text = (f"来源：《{book['title'] if book else '阅读资料'}》\n\n"
                 f"> {quote}\n\n{note_text}").strip()
    digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    tags = _clean_tags([*_clean_tags(body.get("tags")), "阅读摘录", "阅读中心"])
    try:
        board_id = int(body.get("board_id")) if body.get("board_id") not in (None, "", "none") else None
    except (TypeError, ValueError):
        board_id = None
    if not board_id:
        item = _item_row(conn, cid)
        if item and item["shelf_id"]:
            shelf = conn.execute("SELECT board_id FROM reading_shelves WHERE id=?",
                                 (item["shelf_id"],)).fetchone()
            board_id = shelf["board_id"] if shelf else None

    dup = conn.execute("SELECT id FROM clips WHERE content_hash=?",
                       ("reading:" + digest,)).fetchone()
    if dup:
        return {"ok": True, "duplicate": True, "clip_id": dup["id"]}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO clips
           (title,url,channel,board_id,tags,excerpt,content_text,fetch_status,
            content_hash,source_type,grade,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,'stored',?, 'text','ref',?,?)""",
        (title[:180], "", "阅读摘录", board_id,
         json.dumps(tags, ensure_ascii=False), quote[:600], body_text[:60_000],
         "reading:" + digest, now, now))
    new_id = cur.lastrowid

    conn.execute(
        """INSERT OR IGNORE INTO reading_links
           (src_kind,src_id,dst_kind,dst_id,relation,clip_id,note)
           VALUES (?,?,?,?,?,?,?)""",
        (source_kind, src_id, "clip", str(new_id), "derived_from", cid, "汇入知识库"))
    conn.execute(
        """INSERT OR IGNORE INTO reading_links
           (src_kind,src_id,dst_kind,dst_id,relation,clip_id,note)
           VALUES (?,?,?,?,?,?,?)""",
        ("clip", str(new_id), source_kind, src_id, "refs", cid, "来自阅读中心"))
    if source_kind == "note":
        conn.execute("UPDATE reading_notes SET archived_clip_id=? WHERE id=?", (new_id, int(src_id)))
    reindex(conn, cid)
    conn.commit()
    return {"ok": True, "duplicate": False, "clip_id": new_id, "title": title,
            "board_id": board_id, "links": 2}


def api_reindex(conn, qs, body):
    ensure(conn)
    cid = body.get("clip_id")
    if cid:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return _err("clip_id 无效")
        if not _item_row(conn, cid):
            return _err("这份资料不在阅读中心")
        n = reindex(conn, cid)
        conn.commit()
        return {"ok": True, "indexed": n}
    total = 0
    for row in conn.execute("SELECT clip_id FROM reading_items").fetchall():
        total += reindex(conn, row["clip_id"])
    conn.commit()
    return {"ok": True, "indexed": total}


# ══════════════════════════════════════════════════════════
# AI 伴读：走 WorkBuddy 账号云端额度（server.ai_chat / WB_DEFAULT）
# ══════════════════════════════════════════════════════════

AI_ACTIONS = {
    "chat": ("针对这段阅读内容回答问题。先给结论，再给依据；依据只取自给定正文，"
             "正文里没有的就明说没有，不要补造。用中文。"),
    "summary": ("总结本章。输出：一句话主旨 + 3–6 条要点 + 一处值得追问的地方。"
                "只依据给定章节正文，不引入外部知识。用中文。"),
    "explain": ("解释选中的这段话：字面意思、上下文里的作用、可能的言外之意，分三段写。"
                "不确定的地方标注为推测。用中文。"),
    "outline": ("根据给定目录与各章开头，整理全书提纲与论证脉络，用有序列表。"
                "只依据给定内容。用中文。"),
    "translate": ("把选中文字译成通顺中文；若已是中文，改写成更平实的表达并保留原意。"
                  "只输出译文，不加解释。"),
    "extract": ("从给定内容中提炼可沉淀进知识库的事实、概念与论断，"
                "每条一行，格式为「- 类型｜陈述｜可回溯位置」。不要编造。用中文。"),
    "questions": ("基于给定内容提出 5 个值得继续追的问题，按由浅入深排列，"
                  "每个问题后面附一句「为什么值得问」。用中文。"),
    "polish": ("把用户给的阅读笔记整理成结构清晰的 Markdown：补齐小标题、"
               "去掉重复、保留原意与原话，不新增事实。只输出 Markdown。"),
}


def _ai_status() -> dict:
    fn = _CTX.get("ai_config")
    try:
        cfg = fn() if fn else {}
    except Exception:
        cfg = {}
    return {"ready": bool(cfg.get("base_url") and cfg.get("key") and cfg.get("model")
                          and not cfg.get("config_error")),
            "model": cfg.get("model") or "", "style": cfg.get("style") or "",
            "quota": "WorkBuddy 账号云端额度" if cfg.get("style") == "wb" else "自定义端点",
            "config_error": cfg.get("config_error") or ""}


def api_ai_reading(conn, qs, body):
    ensure(conn)
    # 统一实现位于独立模块：证据/检索/缓存与现有资料表解耦。
    import reading_ai
    return reading_ai.handle(conn, body, ai_chat=_CTX["ai_chat"], ai_config=_ai_config_compat,
                             ai_usage=_CTX.get("ai_usage"))


def _ai_config_compat():
    cfg = _CTX["ai_config"]() if _CTX.get("ai_config") else {}
    return {**cfg, "channel": "WorkBuddy AI" if cfg.get("style") == "wb" else "自定义 AI 端点"}


def _legacy_api_ai_reading(conn, qs, body):
    ensure(conn)
    if not _ai_status()["ready"]:
        return _err("WorkBuddy AI 未就绪：" + (_ai_status()["config_error"] or "额度通道未配置"))
    action = str(body.get("action") or "chat")
    if action not in AI_ACTIONS:
        action = "chat"
    try:
        cid = int(body.get("clip_id"))
    except (TypeError, ValueError):
        return _err("clip_id 无效")
    item = _item_row(conn, cid)
    if not item:
        return _err("这份资料不在阅读中心")
    clip = conn.execute("SELECT title,summary FROM clips WHERE id=?", (cid,)).fetchone()
    doc = item["doc_text"] or ""
    chapters = _j(item["chapters"], [])

    chapter_text, chapter_title = "", ""
    ch_id = str(body.get("chapter_id") or "")
    for ch in chapters:
        if ch.get("id") == ch_id or (not ch_id and chapter_text == ""):
            chapter_text = doc[int(ch.get("start", 0)):int(ch.get("end", 0))]
            chapter_title = ch.get("title", "")
            if ch_id:
                break
    if ch_id and not chapter_text and chapters:
        chapter_text = doc[int(chapters[0].get("start", 0)):int(chapters[0].get("end", 0))]
        chapter_title = chapters[0].get("title", "")

    toc = "\n".join(f"- {c.get('title', '')}（{c.get('chars', 0)} 字）" for c in chapters[:80])
    selection = str(body.get("selection") or "").strip()[:6_000]
    question = str(body.get("question") or "").strip()[:2_000]
    note_md = str(body.get("content_md") or "").strip()[:8_000]
    focus = chapter_text[:9_000] if action in {"summary", "outline", "extract", "questions"} else chapter_text[:6_000]

    user = [
        f"书名：{clip['title'] if clip else '未命名'}",
        f"当前章节：{chapter_title or '未指定'}",
        f"全书目录：\n{toc}" if toc else "全书目录：（未解析出章节）",
        f"\n【本章正文】\n{focus or '（本章没有可读取的文字）'}",
    ]
    if selection:
        user.append(f"\n【选中文字】\n{selection}")
    if note_md:
        user.append(f"\n【我的笔记】\n{note_md}")
    if question:
        user.append(f"\n【我的问题】\n{question}")
    else:
        user.append("\n请按系统指令完成任务。")

    messages = [{"role": "system", "content": AI_ACTIONS[action]},
                {"role": "user", "content": "\n".join(user)}]
    try:
        text = _CTX["ai_chat"](messages, max_tokens=2600)
    except Exception as exc:
        name = type(exc).__name__
        return _err(f"WorkBuddy AI 调用失败（{name}）：{str(exc)[:220]}")
    if not (text or "").strip():
        return _err("WorkBuddy AI 没有返回内容")
    return {"ok": True, "text": text.strip(), "action": action,
            "model": _ai_status()["model"], "quota": _ai_status()["quota"],
            "context": {"book": clip["title"] if clip else "", "chapter": chapter_title,
                        "chapter_chars": len(focus), "selection_chars": len(selection)}}


GET_ROUTES = {
    "/api/reading/overview": api_overview,
    "/api/reading/items": api_items,
    "/api/reading/shelves": api_shelves,
    "/api/reading/book/{cid}": api_book,
    "/api/reading/notes": api_notes,
    "/api/reading/links": api_links,
    "/api/reading/search": api_search,
    "/api/reading/stats": api_stats,
    "/api/reading/export/{cid}": api_export,
}

POST_ROUTES = {
    "/api/reading/items": api_item_add,
    "/api/reading/items/{cid}": api_item_update,
    "/api/reading/items/{cid}/remove": api_item_remove,
    "/api/reading/items/{cid}/transcript": api_transcript,
    "/api/reading/items/{cid}/reparse": api_reparse,
    "/api/reading/shelves": api_shelf_create,
    "/api/reading/shelves/{sid}/delete": api_shelf_delete,
    "/api/reading/progress/{cid}": api_progress,
    "/api/reading/bookmarks/{cid}": api_bookmark_create,
    "/api/reading/bookmarks/{bid}/delete": api_bookmark_delete,
    "/api/reading/notes": api_note_create,
    "/api/reading/notes/{nid}": api_note_update,
    "/api/reading/notes/{nid}/delete": api_note_delete,
    "/api/reading/links": api_link_create,
    "/api/reading/links/{lid}/delete": api_link_delete,
    "/api/reading/archive": api_archive,
    "/api/reading/reindex": api_reindex,
    "/api/ai/reading": api_ai_reading,
}
