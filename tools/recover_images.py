#!/usr/bin/env python
"""观澜 · 恢复丢失的图片文件（不改动数据库）

    python tools/recover_images.py             恢复
    python tools/recover_images.py --dry-run    只报告，不写文件

背景：data/notes 与 data/body 里的位图文件因早前的数据目录迁移丢失，
但数据库 clip_images / clips.body_images 的引用还在，于是前端每个
<img> 都 404，卡片显示「图片暂不可见」。

恢复顺序（都不动数据库，用原文件名写回，引用即刻重新生效）：
  1) 本地副本：在 peer-sync / backup / 旧 data 备份里按文件名找，直接复制。
  2) 原文重抓：对仍有缺失且有 url 的收存，抓 HTML 里的图片，
     带 Referer 下载，然后按两种指纹匹配原文件名：
       * clip_images  —— 下载内容长度 == 数据库 size
       * body_images  —— b{cid}-{pos:02d}-{sha256(raw[:4096])[:8]}{ext}
  3) 报告仍缺失的记录；这些由服务端的 clip_images_of() 过滤掉，
     前端不会再拿到 404 的图片，也就不再出现占位框。

只新增文件，不删除、不 UPDATE，可重复运行。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import storage                                   # noqa: E402

DATA = storage.DATA_DIR
NOTES = DATA / "notes"
BODY = DATA / "body"
DB = DATA / "guanlan.db"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

LOCAL_SEARCH = [
    DATA / "peer-sync",
    ROOT / "backup",
    ROOT / "backups",
    ROOT / "data.bak-moved-20261111",
]


def sniff_ext(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ""


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "zh-CN,zh;q=0.9"})
    with opener().open(req, timeout=20) as r:
        return r.read(3_000_000).decode("utf-8", "replace")


def image_urls(html: str) -> list[str]:
    out: list[str] = []
    for u in re.findall(r'(?:data-src|src)="(https?://[^"]+)"', html):
        u = u.replace("&amp;", "&")
        if not re.search(r"(mmbiz\.qpic|qlogo|\.(png|jpe?g|webp|gif)(\?|$)|wx_fmt)", u, re.I):
            continue
        if u not in out:
            out.append(u)
    return out


def download(url: str, referer: str) -> bytes | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "image/webp,image/png,image/jpeg,image/gif,*/*;q=0.5",
        "Referer": referer,
    })
    try:
        with opener().open(req, timeout=15) as r:
            ctype = (r.headers.get_content_type() or "").split("/")[0]
            if ctype != "image":
                return None
            return r.read(8 * 1024 * 1024 + 1)
    except Exception:
        return None


def collect_targets() -> dict:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    clip_imgs = []
    for r in cur.execute("SELECT id,clip_id,file,name,size FROM clip_images ORDER BY id"):
        if not (NOTES / str(r["file"])).is_file():
            clip_imgs.append({"id": r["id"], "cid": r["clip_id"], "file": str(r["file"]),
                              "size": int(r["size"] or 0), "name": str(r["name"] or "")})
    body_imgs = []
    urls = {}
    for r in cur.execute("SELECT id,title,url,body_images FROM clips"):
        urls[int(r["id"])] = str(r["url"] or "")
        try:
            arr = json.loads(r["body_images"] or "[]")
        except Exception:
            arr = []
        for i, it in enumerate(arr):
            if not isinstance(it, dict):
                continue
            f = str(it.get("f") or "")
            if f and not (BODY / f).is_file():
                body_imgs.append({"cid": int(r["id"]), "pos": i, "file": f,
                                  "hash": f.split("-")[-1].split(".")[0] if "-" in f else "",
                                  "ext": Path(f).suffix.lower()})
    con.close()
    return {"clip": clip_imgs, "body": body_imgs, "urls": urls}


def local_restore(targets: dict, dry: bool) -> int:
    """按文件名在 peer-sync / 备份目录里找副本。"""
    index: dict[str, Path] = {}
    for root in LOCAL_SEARCH:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.name not in index:
                index[p.name] = p
    n = 0
    for t in targets["clip"]:
        src = index.get(t["file"])
        if src:
            print(f"  [本地] clip_images → {t['file']}  ←  {src}")
            if not dry:
                NOTES.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, NOTES / t["file"])
            n += 1
    for t in targets["body"]:
        src = index.get(t["file"])
        if src:
            print(f"  [本地] body_images → {t['file']}  ←  {src}")
            if not dry:
                BODY.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, BODY / t["file"])
            n += 1
    return n


def remote_restore(targets: dict, dry: bool) -> int:
    """从原文重新下载，按 size / 内容哈希匹配原文件名。"""
    by_cid: dict[int, dict] = {}
    for t in targets["clip"]:
        by_cid.setdefault(t["cid"], {"clip": [], "body": []})["clip"].append(t)
    for t in targets["body"]:
        by_cid.setdefault(t["cid"], {"clip": [], "body": []})["body"].append(t)

    n = 0
    for cid, group in by_cid.items():
        url = targets["urls"].get(cid, "")
        if not url.startswith(("http://", "https://")):
            print(f"  clip {cid}: 无原文链接，跳过网络恢复")
            continue
        try:
            html = fetch_html(url)
            urls = image_urls(html)
            print(f"  clip {cid}: 原文找到 {len(urls)} 个图片地址")
        except Exception as e:
            print(f"  clip {cid}: 抓原文失败 {e!r}")
            continue
        if dry:
            continue
        NOTES.mkdir(parents=True, exist_ok=True)
        BODY.mkdir(parents=True, exist_ok=True)
        for u in urls:
            raw = download(u, url)
            if not raw or not sniff_ext(raw):
                continue
            ext = sniff_ext(raw)
            # 1) 主收存图：按数据库 size 精确匹配
            for t in list(group["clip"]):
                if t["size"] and len(raw) == t["size"]:
                    (NOTES / t["file"]).write_bytes(raw)
                    print(f"  [网络] clip_images → {t['file']} ({len(raw)}B, «{t['name']}»)")
                    group["clip"].remove(t)
                    n += 1
                    break
            # 2) 正文配图：按 b{cid}-{pos:02d}-{hash}{ext} 复现文件名
            h = hashlib.sha256(raw[:4096]).hexdigest()[:8]
            for t in list(group["body"]):
                if t["hash"] == h and t["ext"] in (ext, ".jpeg" if ext == ".jpg" else ext):
                    (BODY / t["file"]).write_bytes(raw)
                    print(f"  [网络] body_images → {t['file']} ({len(raw)}B, hash={h})")
                    group["body"].remove(t)
                    n += 1
                    break
            if not group["clip"] and not group["body"]:
                break
    return n


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    print(f"=== 观澜图片恢复 {'(dry-run)' if dry else ''} ===")
    print(f"数据目录 {DATA}")
    targets = collect_targets()
    print(f"待恢复：clip_images {len(targets['clip'])} 张 / body_images {len(targets['body'])} 张")
    print("[1] 本地副本")
    n1 = local_restore(targets, dry)
    print(f"  本地恢复 {n1} 张")
    # 重新扫描，只把仍缺失的交给网络恢复
    targets = collect_targets()
    print("[2] 原文重抓")
    n2 = 0 if dry else remote_restore(targets, dry)
    print(f"  网络恢复 {n2} 张")

    left = collect_targets()
    print(f"=== 结果：恢复 {n1 + n2} 张，仍缺失 "
          f"clip_images {len(left['clip'])} / body_images {len(left['body'])} ===")
    for t in left["clip"]:
        print(f"  仍缺 clip_images id={t['id']} «{t['name']}» {t['file']}")
    for t in left["body"]:
        print(f"  仍缺 body_images clip={t['cid']} {t['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
