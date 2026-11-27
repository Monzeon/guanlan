#!/usr/bin/env python
"""阅读 AI 隔离 QA：独立数据目录 + 本地模型桩，绝不调用真实 AI。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server
import reading
import reading_ai


def fake_ai(messages, max_tokens=4000):
    text = messages[-1]["content"]
    if "章节：" in text and "【" not in text:
        return "本章主旨为测试章节摘要。"
    if "根据各章摘要" in messages[0]["content"]:
        return "1. 全书脉络：从城市发展到第二章的潮汐政策。"
    return "这是隔离测试回答，依据真实资料片段。[E1]"


def main():
    os.environ["GL_AI_DISABLE"] = "1"
    server.ai_chat = fake_ai
    reading_ai.CACHE.clear()
    # 用已装配的 reading._CTX 替换调用，配置仅用于显示，不能访问云端。
    reading._CTX["ai_chat"] = fake_ai
    reading._CTX["ai_config"] = lambda: {
        "base_url": "offline.test", "key": "offline", "model": "offline-model",
        "style": "wb", "config_error": ""}
    reading._CTX["ai_usage"] = lambda: {"prompt_tokens": 120, "completion_tokens": 18, "total_tokens": 138}
    server.storage.ensure_dirs()
    server.init_db()
    conn = server.db()
    conn.execute("INSERT INTO clips(id,title,source_type,content_text,fetch_status) VALUES(1,'隔离测试书','text',?,'stored')",
                 ("",))
    first = "城市发展与公共交通的关系需要结合具体证据分析。" * 420
    second = "人口流动与空间布局是另一项重要因素。" * 420
    doc = ("第一章开头。" + first + "第一章中段。" + second + "第一章结尾。")[:12000] + "第二章出现独特词：潮汐政策。"
    chapters = [{"id":"a","title":"第一章","level":1,"start":0,"end":12000,"chars":12000},
                {"id":"b","title":"第二章","level":1,"start":12000,"end":len(doc),"chars":len(doc)-12000}]
    conn.execute("INSERT INTO reading_items(clip_id,kind,doc_text,chapters,char_count) VALUES(1,'book',?,?,?)",
                 (doc,json.dumps(chapters,ensure_ascii=False),len(doc)))
    conn.execute("INSERT INTO reading_notes(clip_id,title,content_md,chapter_id,kind) VALUES(1,'我的原稿','原始观点：不能覆盖这句话。','a','note')")
    reading.reindex(conn,1)
    conn.commit(); conn.close()
    print("隔离测试书 ID=1；模型为 offline-model；GL_HOME=" + os.environ.get("GL_HOME", ""),flush=True)
    sys.argv = ["server.py","--host","127.0.0.1","--port","8899"]
    return server.main()


if __name__ == "__main__":
    raise SystemExit(main())
