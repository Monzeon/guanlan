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
import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import random
import mimetypes
import os
import secrets
import shutil
import subprocess
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cloud_sync import (
    DEFAULT_CLOUD_ENDPOINT,
    DEFAULT_CLOUD_KEY,
    CloudSync,
    cloud_config_load,
    cloud_config_save,
)
from peer_sync import PeerSync
import pdf_text
import storage                                  # 数据与缓存的落盘根目录，统一在 storage.py
import app_paths                                # 打包后资源解压在 sys._MEIPASS

ROOT = app_paths.RES_DIR
DB_PATH = storage.DATA_DIR / "guanlan.db"
STATIC = ROOT / "static"
SCHEMA = ROOT / "schema.sql"
READING_SCHEMA = ROOT / "reading_schema.sql"   # 阅读中心独立大板块的表结构
APP_LOG = storage.DATA_DIR / "app.log"
_LOGGER: logging.Logger | None = None


def app_logger() -> logging.Logger:
    """结构化日志：内部异常写轮转文件，不再把堆栈直接吐给浏览器。"""
    global _LOGGER
    if _LOGGER is None:
        logger = logging.getLogger("guanlan")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            try:
                APP_LOG.parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    APP_LOG, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
            except OSError:
                handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(threadName)s %(message)s"))
            logger.addHandler(handler)
        _LOGGER = logger
    return _LOGGER


def internal_error_ref(context: str, exc: BaseException | None = None) -> str:
    ref = uuid.uuid4().hex
    try:
        if exc is not None:
            app_logger().error("[%s] %s", ref, context,
                               exc_info=(type(exc), exc, exc.__traceback__))
        else:
            app_logger().error("[%s] %s", ref, context)
    except Exception:  # noqa: BLE001 - 日志失败不能盖掉原请求
        pass
    return ref


def internal_error_payload(exc: BaseException, context: str) -> dict:
    return {"error": "内部错误", "ref": internal_error_ref(context, exc)}

# PDF 阅读器随应用走，不要求用户另装依赖。只把 vendor 放进来，不修改全局 Python。
VENDOR = ROOT / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

# ── 手机访问：同一局域网 + 一个六位访问码 ──────────────────
# 桌面走 127.0.0.1 不受影响；只有从别的设备连进来才要码。
# 技术评审后：默认关闭；访问码只通过 POST 提交；登录成功换发随机会话令牌。
LAN_FILE = storage.DATA_DIR / "lan.json"
LAN_SESSION_FILE = storage.DATA_DIR / "lan-sessions.json"
LAN_RATE_FILE = storage.DATA_DIR / "lan-rate.json"
LAN_KEY = ""
LAN_MODE = False          # 是否绑到 0.0.0.0（要不要让别的设备连得进来）
LAN_OPEN = False          # 开关：关着的时候，非本机请求一律 403
LAN_PORT = 8790
LAN_IP = ""
LAN_TRIES: dict[str, list[float]] = {}
LAN_SESSION_TTL = 31536000
LAN_SESSION_LOCK = threading.Lock()
LAN_SESSIONS: dict[str, float] = {}
LAN_SESSIONS_LOADED = False
LAN_FAIL_TIMES: list[float] = []
LAN_FAIL_BLOCKED_UNTIL = 0.0
LAN_RATE_LOADED = False
CURRENT_REQUEST = threading.local()

# 内联展示 HTML 原件时的沙箱策略：文档被丢进唯一源，脚本/表单/弹窗/顶层跳转
# 全部禁用，外链资源一律不加载，只放开内联样式与 data: 图片字体。这样
# 「打开 HTML 直接读」不会变成同源存储型 XSS（历史白名单排除 html 的原因）。
HTML_SANDBOX_CSP = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

# ── 多端实时同步 ─────────────────────────────────────
# 所有成功的写请求推进一个单调版本，SSE 连接只负责把这个版本推给别的客户端。
# 用毫秒时间戳起步，服务重启后版本也不会倒退到旧客户端认过的值。
SYNC_COND = threading.Condition()
SYNC_VERSION = int(time.time() * 1000)
SYNC_SOURCE = ""
SYNC_CLIENT_COUNT = 0
SYNC_CLIENT_LIMIT = 16

# 慢 AI 写入的短时幂等：同一客户端、同一路径、同一请求体在上一轮未结束时短路。
SLOW_WRITE_PATHS = {
    "/api/ask", "/api/insights/run", "/api/recap",
    "/api/ai/analyze", "/api/ai/digest", "/api/ai/instruct",
}
SLOW_REQUEST_TTL = 260
AI_INFLIGHT_LOCK = threading.Lock()
AI_INFLIGHT: dict[str, float] = {}


def is_slow_post_path(path: str) -> bool:
    return (path in SLOW_WRITE_PATHS or path.startswith("/api/ai/")
            or path.endswith("/extract"))


def slow_request_key(path: str, qs: dict, body: dict, client: str) -> str:
    material = json.dumps(
        {"path": path, "qs": qs, "body": body, "client": client},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def slow_request_enter(key: str) -> bool:
    now = time.time()
    with AI_INFLIGHT_LOCK:
        for old, deadline in list(AI_INFLIGHT.items()):
            if deadline <= now:
                AI_INFLIGHT.pop(old, None)
        if key in AI_INFLIGHT:
            return False
        AI_INFLIGHT[key] = now + SLOW_REQUEST_TTL
        return True


def slow_request_leave(key: str) -> None:
    with AI_INFLIGHT_LOCK:
        AI_INFLIGHT.pop(key, None)


def sync_version() -> int:
    with SYNC_COND:
        return SYNC_VERSION


def sync_notify(source: str = "") -> int:
    """写操作成功后叫醒所有 SSE 连接；source 用来让发起端跳过自己的回声。"""
    global SYNC_VERSION, SYNC_SOURCE
    with SYNC_COND:
        SYNC_VERSION = max(SYNC_VERSION + 1, int(time.time() * 1000))
        SYNC_SOURCE = source[:120]
        SYNC_COND.notify_all()
        version = SYNC_VERSION
    cloud_sync_kick()
    return version


def _safe_json_read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _safe_json_write(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def lan_settings() -> dict:
    """读 data/lan.json：访问码 + 开关 + 是否已被用户显式选择。"""
    data = _safe_json_read(LAN_FILE, {})
    return data if isinstance(data, dict) else {}


def lan_save(**patch) -> dict:
    data = lan_settings()
    data.update(patch)
    _safe_json_write(LAN_FILE, data)
    return data


def lan_normalize_settings() -> dict:
    """一次性把旧版「缺省开启」迁移成安全默认；显式选择过的值不动。"""
    data = lan_settings()
    changed = False
    if "enabled" not in data:
        data["enabled"] = False
        changed = True
    if "explicit_choice" not in data:
        data["explicit_choice"] = False
        changed = True
    if not bool(data.get("explicit_choice")) and bool(data.get("enabled")):
        data["enabled"] = False
        changed = True
    return lan_save(**data) if changed else data


def lan_key() -> str:
    """访问码：六位，第一次生成后固定存在 data/lan.json，重装重启都不变。"""
    data = lan_settings()
    key = str(data.get("key") or "").strip().upper()
    if key:
        return key
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # 去掉了 0/O/1/I
    key = "".join(random.choice(alphabet) for _ in range(6))
    lan_save(key=key)
    return key


def _lan_sessions_load_locked() -> None:
    global LAN_SESSIONS, LAN_SESSIONS_LOADED
    if LAN_SESSIONS_LOADED:
        return
    raw = _safe_json_read(LAN_SESSION_FILE, {})
    items = raw.get("sessions", {}) if isinstance(raw, dict) else {}
    now = time.time()
    loaded: dict[str, float] = {}
    if isinstance(items, dict):
        for digest, expiry in items.items():
            try:
                value = float(expiry)
            except (TypeError, ValueError):
                continue
            if value > now:
                loaded[str(digest)] = value
    LAN_SESSIONS = loaded
    LAN_SESSIONS_LOADED = True


def _lan_session_prune_locked(now: float) -> None:
    expired = [k for k, expiry in LAN_SESSIONS.items() if expiry <= now]
    if not expired:
        return
    for key in expired:
        LAN_SESSIONS.pop(key, None)
    _safe_json_write(LAN_SESSION_FILE, {"sessions": LAN_SESSIONS})


def _lan_session_create() -> str:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with LAN_SESSION_LOCK:
        _lan_sessions_load_locked()
        _lan_session_prune_locked(now)
        LAN_SESSIONS[digest] = now + LAN_SESSION_TTL
        _safe_json_write(LAN_SESSION_FILE, {"sessions": LAN_SESSIONS})
    return token


def _lan_session_valid(token: str) -> bool:
    if not token:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with LAN_SESSION_LOCK:
        _lan_sessions_load_locked()
        expiry = LAN_SESSIONS.get(digest)
        if expiry and expiry > now:
            return True
        if digest in LAN_SESSIONS:
            LAN_SESSIONS.pop(digest, None)
            _safe_json_write(LAN_SESSION_FILE, {"sessions": LAN_SESSIONS})
        return False


def _lan_rate_load_locked() -> None:
    global LAN_FAIL_TIMES, LAN_FAIL_BLOCKED_UNTIL, LAN_RATE_LOADED
    if LAN_RATE_LOADED:
        return
    raw = _safe_json_read(LAN_RATE_FILE, {})
    times: list[float] = []
    blocked = 0.0
    if isinstance(raw, dict):
        for value in raw.get("times", []) or []:
            try:
                times.append(float(value))
            except (TypeError, ValueError):
                continue
        try:
            blocked = float(raw.get("blocked_until") or 0)
        except (TypeError, ValueError):
            blocked = 0.0
    LAN_FAIL_TIMES = times
    LAN_FAIL_BLOCKED_UNTIL = blocked
    LAN_RATE_LOADED = True


def _lan_rate_prune_locked(now: float) -> None:
    global LAN_FAIL_TIMES, LAN_FAIL_BLOCKED_UNTIL
    LAN_FAIL_TIMES = [t for t in LAN_FAIL_TIMES if now - t < 3600]
    if LAN_FAIL_BLOCKED_UNTIL < now:
        LAN_FAIL_BLOCKED_UNTIL = 0.0


def _lan_rate_blocked(now: float | None = None) -> int:
    now = now or time.time()
    with LAN_SESSION_LOCK:
        _lan_rate_load_locked()
        _lan_rate_prune_locked(now)
        return int(max(0, LAN_FAIL_BLOCKED_UNTIL - now))


def _lan_rate_record_failure() -> int:
    global LAN_FAIL_BLOCKED_UNTIL
    now = time.time()
    with LAN_SESSION_LOCK:
        _lan_rate_load_locked()
        _lan_rate_prune_locked(now)
        LAN_FAIL_TIMES.append(now)
        if len(LAN_FAIL_TIMES) >= 5:
            delay = min(3600, 2 ** min(len(LAN_FAIL_TIMES) - 4, 12))
            LAN_FAIL_BLOCKED_UNTIL = max(LAN_FAIL_BLOCKED_UNTIL, now + delay)
        _safe_json_write(LAN_RATE_FILE, {
            "times": LAN_FAIL_TIMES,
            "blocked_until": LAN_FAIL_BLOCKED_UNTIL,
        })
        return int(max(0, LAN_FAIL_BLOCKED_UNTIL - now))


def _lan_rate_reset() -> None:
    global LAN_FAIL_TIMES, LAN_FAIL_BLOCKED_UNTIL
    with LAN_SESSION_LOCK:
        _lan_rate_load_locked()
        LAN_FAIL_TIMES = []
        LAN_FAIL_BLOCKED_UNTIL = 0.0
        _safe_json_write(LAN_RATE_FILE, {
            "times": LAN_FAIL_TIMES,
            "blocked_until": LAN_FAIL_BLOCKED_UNTIL,
        })


LAN_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f5f3ee">
<title>观澜 · 访问码</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px; background: #f5f3ee;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC",
                 "PingFang SC", "Microsoft YaHei", sans-serif; color: #1f2328;
  }
  form { width: 100%; max-width: 320px; text-align: center; }
  .mark { width: 56px; height: 56px; margin: 0 auto 18px; border-radius: 14px;
          background: #0e1a2e; display: flex; align-items: center; justify-content: center; }
  h1 { margin: 0 0 6px; font-size: 21px; font-weight: 600; letter-spacing: .08em; }
  p { margin: 0 0 24px; font-size: 13px; line-height: 1.7; color: #8b8f96; }
  input {
    width: 100%; padding: 15px 18px; text-align: center;
    font-size: 26px; font-weight: 600; letter-spacing: .24em; text-indent: .24em;
    text-transform: uppercase; color: #1f2328;
    border: 1px solid #dedad2; border-radius: 12px; background: #fffefb;
    outline: none; -webkit-appearance: none;
  }
  input:focus { border-color: #c9973b; box-shadow: 0 0 0 3px rgba(201,151,59,.14); }
  button {
    width: 100%; margin-top: 14px; padding: 14px; border: none; border-radius: 12px;
    background: #0e1a2e; color: #f3ecdd; font-size: 15px; font-weight: 500;
    letter-spacing: .06em; cursor: pointer;
  }
  button:active { transform: translateY(1px); }
  .hint { margin-top: 18px; font-size: 11.5px; color: #a8acb3; line-height: 1.7; }
  .err { margin-top: 14px; font-size: 12px; color: #a33; line-height: 1.6; }
</style></head>
<body>
<form method="post" action="/api/lan/login">
  <div class="mark">
    <svg width="30" height="30" viewBox="0 0 34 34" aria-hidden="true">
      <path d="M5 21c3.2-6.4 6.4-6.4 9.6 0s6.4 6.4 9.6 0 4.8-4.8 4.8 0"
        stroke="#c9973b" stroke-width="2.6" fill="none" stroke-linecap="round"/>
      <circle cx="24.5" cy="9" r="2.8" fill="#efcb84"/>
    </svg>
  </div>
  <h1>观澜</h1>
  <p>同一局域网内，输入电脑上显示的<br>六位访问码</p>
  <input name="k" maxlength="6" autocapitalize="characters" autocomplete="off"
         autocorrect="off" spellcheck="false" inputmode="latin" placeholder="······">
  <button type="submit">进去</button>
  <!--ERROR-->
  <div class="hint">码在电脑端「设置 · 手机访问」里</div>
</form>
</body></html>"""


LAN_OFF_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f5f3ee">
<title>观澜 · 手机访问没开</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         padding: 24px; background: #f5f3ee; color: #1f2328; text-align: center;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC",
                      "PingFang SC", "Microsoft YaHei", sans-serif; }
  .box { max-width: 300px; }
  h1 { font-size: 17px; font-weight: 600; margin: 0 0 10px; letter-spacing: .04em; }
  p { font-size: 13px; line-height: 1.85; color: #8b8f96; margin: 0; }
  code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px;
         background: #ebe7de; padding: 1px 5px; border-radius: 4px; color: #474b52; }
</style></head>
<body><div class="box">
  <h1>手机访问现在是关着的</h1>
  <p>到电脑上打开「设置 · 手机访问」把它打开，<br>再回这里刷新一次。</p>
</div></body></html>"""


def lan_ip() -> str:
    """本机在局域网里的地址。UDP connect 不发包，只是让系统选一张网卡。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


TIER_CEILING = {"T0": 0.95, "T1": 0.85, "T2": 0.70, "T3": 0.50, "T4": 0.25}

# 材料级：和信源分级是两条正交的轴。
#   tier  回答「谁说的、能信到什么程度」——决定引用时怎么标。
#   grade 回答「它属于库里的哪一层、能不能当依据、活多久」——决定待遇。
GRADES = {"canon": "权威", "ref": "参考", "daily": "资讯"}
GRADE_NOTE = {
    "canon": "法规、公报、报告、论文、一手文件：可以当依据",
    "ref": "深度报道、专业分析、书摘：可引用，算分析",
    "daily": "每日新闻、快讯、推送：只作时效背景",
}
GRADE_UNSET_LABEL = "未定"

# 正文批注的五种语义色；颜色不是装饰，侧栏会用同一套词（阅读中心与收存共用）。
ANNOTATION_COLORS = {
    "amber": "重点",
    "sage": "论据",
    "blue": "疑问",
    "rose": "反驳",
    "violet": "联想",
}

# 域名命中即判权威：官方、统计、央行、国际组织、期刊与论文库
AUTHORITY_HOSTS = (
    "gov.cn", "gov.hk", "gov.mo", "stats.gov.cn", "pbc.gov.cn", "mof.gov.cn",
    "ndrc.gov.cn", "mofcom.gov.cn", "customs.gov.cn", "nhc.gov.cn", "moe.gov.cn",
    "nbs.gov.cn", "csrc.gov.cn", "safe.gov.cn", "nfra.gov.cn",
    "who.int", "imf.org", "worldbank.org", "oecd.org", "un.org", "unctad.org",
    "adb.org", "bis.org", "federalreserve.gov", "ecb.europa.eu", "eurostat.ec.europa.eu",
    "arxiv.org", "ssrn.com", "nber.org", "nature.com", "science.org", "thelancet.com",
    "nejm.org", "cnki.net", "wanfangdata.com.cn", "doi.org", "springer.com",
)
# 你自己传进来的这些，默认算「参考」：是可留档的材料，但够不上权威
GRADE_FILE_EXT = {".pdf", ".doc", ".docx", ".txt", ".md", ".markdown", ".tex",
                  ".epub", ".csv", ".xlsx", ".xls", ".ppt", ".pptx"}

DOMAINS = {
    "economy": "经济", "population": "人口", "history": "历史",
    "society": "社会", "culture": "文化", "world": "世界", "cross": "跨域",
}
SOURCE_KINDS = {"web", "pdf", "report", "book", "data"}

# ── 数据库 ───────────────────────────────────────────

# 读共享 / 快照替换排他：普通请求（包括 AI 长请求）不再互相排队四分钟；
# 只有 PeerSync / CloudSync 替换整库时通过 `with lock` 取排他窗口。
class LibraryGate:
    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._shared: dict[int, int] = {}
        self._shared_total = 0
        self._exclusive_owner: int | None = None
        self._exclusive_depth = 0
        self._waiting_writers = 0

    def acquire(self, shared: bool = False, blocking: bool = True, timeout: float = -1) -> bool:
        ident = threading.get_ident()
        deadline = None if timeout is None or timeout < 0 else time.time() + timeout
        with self._cond:
            if not shared and self._exclusive_owner == ident:
                self._exclusive_depth += 1
                return True
            if shared and self._exclusive_owner is None and self._shared.get(ident, 0) > 0:
                # 同线程嵌套读连接不能因为后来排队的写者而自锁。
                self._shared[ident] += 1
                self._shared_total += 1
                return True
            if shared and self._exclusive_owner == ident:
                self._shared[ident] = self._shared.get(ident, 0) + 1
                self._shared_total += 1
                return True
            if not shared:
                self._waiting_writers += 1
            try:
                while (self._exclusive_owner is not None
                       or (not shared and self._shared_total > 0)
                       or (shared and self._waiting_writers > 0)):
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline - time.time()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                if shared:
                    self._shared[ident] = self._shared.get(ident, 0) + 1
                    self._shared_total += 1
                else:
                    self._exclusive_owner = ident
                    self._exclusive_depth = 1
                return True
            finally:
                if not shared:
                    self._waiting_writers = max(0, self._waiting_writers - 1)
                    # 写者超时/异常退出后，等着的读者不该继续睡到自己的超时
                    self._cond.notify_all()

    def release(self, shared: bool = False) -> None:
        ident = threading.get_ident()
        with self._cond:
            if shared:
                depth = self._shared.get(ident, 0)
                if depth <= 0:
                    raise RuntimeError("LibraryGate: 共享锁释放次数不匹配")
                if depth == 1:
                    self._shared.pop(ident, None)
                else:
                    self._shared[ident] = depth - 1
                self._shared_total = max(0, self._shared_total - 1)
                if self._shared_total == 0:
                    self._cond.notify_all()
                return
            if self._exclusive_owner != ident:
                raise RuntimeError("LibraryGate: 排他锁释放者不匹配")
            self._exclusive_depth -= 1
            if self._exclusive_depth <= 0:
                self._exclusive_owner = None
                self._exclusive_depth = 0
                self._cond.notify_all()

    def __enter__(self):
        self.acquire(shared=False)
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release(shared=False)

    def locked(self) -> bool:
        return self._exclusive_owner is not None or self._shared_total > 0


LIBRARY_LOCK = LibraryGate()
PEER: PeerSync | None = None
CLOUD: CloudSync | None = None


class _LockedConnection(sqlite3.Connection):
    """在连接层吸收「数据库瞬时不可写」，而不是让每个 API 各自兜底。

    data/app.log 里曾有连续几十条 `attempt to write a readonly database`：
    GET 详情写 viewed_at、POST 进度写 progress 都因此整条 500，用户侧
    表现为进度条静默冻结。单条 SQL 重放是安全的 —— 失败的那条没有生效，
    也不会重复任何业务副作用；真·只读库三次后照样把原始错误抛出去。
    """

    _RETRYABLE = ("readonly", "locked", "busy")

    def execute(self, sql, parameters=(), /):
        for attempt in range(3):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if attempt >= 2 or not any(k in msg for k in self._RETRYABLE):
                    raise
                time.sleep(0.12 * (attempt + 1))

    def close(self):
        try:
            return super().close()
        finally:
            if not getattr(self, "_gl_lock_released", False):
                self._gl_lock_released = True
                LIBRARY_LOCK.release(shared=True)


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 不在共享锁上无限等待：长请求（AI 最长 240s、全表导出）叠加写者优先，
    # 会让一个排队写者把之后所有读请求一起冻住；超时后交给统一 500 处理。
    if not LIBRARY_LOCK.acquire(shared=True, timeout=30):
        raise RuntimeError("数据库繁忙（排队超过 30 秒），请稍后重试")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30, factory=_LockedConnection)
        conn._gl_lock_released = False
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn
    except Exception:
        LIBRARY_LOCK.release(shared=True)
        raise


def peer() -> PeerSync:
    """延迟创建：Android 端会先改 ROOT/DB_PATH/目录，再触发这里。"""
    global PEER
    if PEER is None:
        data = DB_PATH.parent
        PEER = PeerSync(
            ROOT, DB_PATH, data,
            {
                "uploads": UPLOAD_DIR,
                "body": BODY_DIR,
                "notes": NOTES_DIR,
            },
            LIBRARY_LOCK,
        )
    return PEER


def cloud_peer() -> CloudSync:
    """云同步客户端：配置在 data/cloud.json，默认关闭、需显式选择后才启用。"""
    global CLOUD
    if CLOUD is None:
        data = DB_PATH.parent
        CLOUD = CloudSync(
            ROOT, DB_PATH, data,
            {
                "uploads": UPLOAD_DIR,
                "body": BODY_DIR,
                "notes": NOTES_DIR,
            },
            LIBRARY_LOCK,
            cloud_config_load(data, peer().token),
        )
    return CLOUD


def start_cloud_sync_background() -> None:
    """云同步兜底循环；页面操作会触发更及时的同步，这里是保底节奏。"""
    try:
        cloud_peer().start_auto_sync_if_enabled()
    except Exception as e:  # noqa: BLE001
        app_logger().error("后台云同步没起来", exc_info=(type(e), e, e.__traceback__))


CLOUD_KICK_LOCK = threading.Lock()
CLOUD_KICK_TIMER: threading.Timer | None = None


def cloud_sync_kick(delay: float = 1.2) -> None:
    """本地写成功后稍等片刻再推云，合并连续输入产生的多次写。"""
    global CLOUD_KICK_TIMER

    def run():
        try:
            cp = cloud_peer()
            if cp.config.get("enabled", False):
                cp.sync_once()
        except Exception:  # noqa: BLE001
            pass

    with CLOUD_KICK_LOCK:
        if CLOUD_KICK_TIMER is not None:
            CLOUD_KICK_TIMER.cancel()
        CLOUD_KICK_TIMER = threading.Timer(delay, run)
        CLOUD_KICK_TIMER.daemon = True
        CLOUD_KICK_TIMER.start()


# 全新库的默认板块；已有库不会被动加，删光了也不会自己长回来。
DEFAULT_BOARDS = ("政治", "经济", "文化", "社会")

# 板块的两种性格：普通抽屉 vs 资料库（原件、原始数据、权威原文的存放处）
BOARD_KINDS = {"": "普通", "archive": "资料库"}

BOARD_MIGRATIONS = {
    "kind": "TEXT NOT NULL DEFAULT ''",
}

CLIP_MIGRATIONS = {
    "board_id": "INTEGER",
    "tier": "TEXT DEFAULT ''",
    "grade": "TEXT NOT NULL DEFAULT ''",
    "grade_hint": "TEXT DEFAULT ''",
    "grade_hint_reason": "TEXT DEFAULT ''",
    "summary": "TEXT DEFAULT ''",
    "content_text": "TEXT DEFAULT ''",
    "body_images": "TEXT DEFAULT ''",          # 正文配图 JSON：[{f,n,c,at,w,h}]
    "fetch_status": "TEXT NOT NULL DEFAULT 'idle'",
    "fetch_error": "TEXT DEFAULT ''",
    "archived": "INTEGER NOT NULL DEFAULT 0",
    "content_hash": "TEXT DEFAULT ''",
    "source_type": "TEXT NOT NULL DEFAULT 'web'",
    "file_name": "TEXT DEFAULT ''",
    "file_path": "TEXT DEFAULT ''",
    "mime_type": "TEXT DEFAULT ''",
    "file_size": "INTEGER NOT NULL DEFAULT 0",
    "viewed_at": "TEXT DEFAULT ''",
    "updated_at": "TEXT DEFAULT ''",
}


def migrate_clips(conn: sqlite3.Connection) -> None:
    """旧库只加列、不改旧值；新库执行 schema 后这里是空操作。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clips'").fetchone()
    if not exists:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)").fetchall()}
    for name, definition in CLIP_MIGRATIONS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {definition}")
    conn.execute("UPDATE clips SET updated_at=created_at WHERE updated_at IS NULL OR updated_at=''")


def migrate_boards(conn: sqlite3.Connection) -> None:
    """旧库只加列、不改旧值；老板块一律是普通板块，资料库由你自己指定。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='boards'").fetchone()
    if not exists:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(boards)").fetchall()}
    for name, definition in BOARD_MIGRATIONS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE boards ADD COLUMN {name} {definition}")


GRADE_SCHEMA_VERSION = 3          # user_version 打标记：材料级回填只跑一次


def backfill_grades(conn: sqlite3.Connection) -> None:
    """老库一次性回填材料级：先按信源分级推，剩下的按域名猜。

    用 PRAGMA user_version 记一次，跑过就不再跑——否则你手动清成「未定」的
    条目，下次启动又会被推回去，手动判断就白做了。
    """
    if (conn.execute("PRAGMA user_version").fetchone()[0] or 0) >= GRADE_SCHEMA_VERSION:
        return
    conn.execute("""
        UPDATE clips SET grade = CASE
            WHEN source_type='file' AND tier IN ('T0','T1') THEN 'canon'
            WHEN source_type='file' THEN 'ref'
            WHEN tier IN ('T0','T1') THEN 'canon'
            WHEN tier = 'T2' THEN 'ref'
            WHEN tier IN ('T3','T4') THEN 'daily'
            ELSE '' END
        WHERE grade = ''""")
    for row in conn.execute(
            "SELECT id,url,source_type,file_name,tier FROM clips WHERE grade=''").fetchall():
        guessed = guess_grade(row["url"] or "", row["source_type"] or "",
                              row["file_name"] or "", row["tier"] or "")
        if guessed:
            conn.execute("UPDATE clips SET grade=? WHERE id=?", (guessed, row["id"]))
    conn.execute(f"PRAGMA user_version = {GRADE_SCHEMA_VERSION}")


def init_db() -> None:
    conn = db()
    try:
        # 新索引依赖新增列，因此旧库必须先迁移再执行完整 schema。
        migrate_clips(conn)
        # 只有全新的库才铺默认板块：你自己删光了，重启也不该再长回来。
        fresh = not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='boards'").fetchone()
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        if READING_SCHEMA.is_file():
            conn.executescript(READING_SCHEMA.read_text(encoding="utf-8"))
        migrate_clips(conn)
        migrate_boards(conn)
        if fresh:
            for i, name in enumerate(DEFAULT_BOARDS):
                conn.execute("INSERT OR IGNORE INTO boards(name, sort) VALUES (?,?)", (name, i))
        conn.execute("UPDATE clips SET archived=0 WHERE archived<>0")   # 归档已取消，全部放回
        # 阅读中心的结构化正文自己持久化（reading_items.doc_text），启动后回填，
        # 否则 clip_annotations 的字符偏移会在重启后全部失锚。
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='reading_items'").fetchone():
            conn.execute("""UPDATE clips SET content_text =
                              (SELECT ri.doc_text FROM reading_items ri WHERE ri.clip_id=clips.id)
                            WHERE source_type='file'
                              AND EXISTS (SELECT 1 FROM reading_items ri2
                                          WHERE ri2.clip_id=clips.id)""")
        # 这里以前有一句 `UPDATE clips SET content_text='' WHERE source_type='file'`
        # （「文件件不留抽取文本」，11-03 的旧设计）。产品早就改成保存正文了，
        # 这句却留了下来：每重启一次就把所有 PDF/EPUB 抽好的正文清空一次，
        # 而且只清 content_text、不动 fetch_status —— 于是库里全是
        # 「stored 但 0 字」，界面一边说「正文已入库」一边说「暂未提取到文字」。
        # 抽取失败的诚实状态由下面的自愈统一收拾：
        # 没有正文就不是 stored，宁可显示「没抓到」并给重试入口。
        # 大 PDF 走后台线程抽取，期间状态是 'extracting'。线程是 daemon，
        # 服务一重启它就没了；这个状态若不收口，前端会一直显示「正在提取正文…」
        # 且把「提取正文」按钮藏掉 —— 条目再也抽不出正文，连重试入口都没有。
        # 所以重启时一并自愈：没正文的回 failed 给重试入口，已有正文的回 stored。
        conn.execute("""UPDATE clips
                           SET fetch_status='failed',
                               fetch_error=CASE
                                   WHEN fetch_status='extracting'
                                       THEN '上次后台提取未完成（服务重启或中断），请重新提取正文'
                                   WHEN trim(coalesce(fetch_error,''))=''
                                       THEN '正文为空，需要重新提取'
                                   ELSE fetch_error END
                         WHERE source_type='file'
                           AND fetch_status IN ('stored','extracting')
                           AND trim(coalesce(content_text,''))=''""")
        conn.execute("""UPDATE clips SET fetch_status='stored', fetch_error=''
                         WHERE source_type='file' AND fetch_status='extracting'
                           AND trim(coalesce(content_text,''))<>''""")
        backfill_grades(conn)                                                     # 老库补一次材料级
        prune_note_images(conn)                                                   # 清掉没挂上的随手记图片
        prune_body_images(conn)                                                   # 清掉没归属的正文配图
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")     # 真拿一次写锁，比读一下更能说明问题
            conn.rollback()
        except sqlite3.OperationalError as e:
            print(f"[db] 警告：数据库当前不可写（{e}）。"
                  f"关掉占用它的程序，或检查 data 目录权限。", flush=True)
    finally:
        conn.close()


def next_code(conn: sqlite3.Connection, prefix: str, table: str) -> str:
    """生成 C-2026-0001 式编号；计数器写入会串行化并发请求。"""
    if table not in {"claims", "predictions", "decisions", "sources"}:
        raise ValueError("未知编号表")
    year = datetime.now().year
    rows = conn.execute(
        f"SELECT code FROM {table} WHERE code LIKE ?", (f"{prefix}-{year}-%",)).fetchall()
    seqs = []
    for row in rows:
        try:
            seqs.append(int(row["code"].rsplit("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    candidate = max(seqs, default=0) + 1
    conn.execute(
        """INSERT INTO code_counters (prefix,year,seq) VALUES (?,?,?)
           ON CONFLICT(prefix,year) DO UPDATE SET seq=MAX(code_counters.seq+1,excluded.seq)""",
        (prefix, year, candidate))
    seq = conn.execute(
        "SELECT seq FROM code_counters WHERE prefix=? AND year=?", (prefix, year)).fetchone()["seq"]
    return f"{prefix}-{year}-{seq:04d}"


def json_value(raw, fallback):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return fallback if value is None else value
    except (json.JSONDecodeError, TypeError):
        return fallback


def clean_strings(values, limit=40) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，]", values.strip("[]\"'"))
    if not isinstance(values, (list, tuple, set)):
        return []
    out = []
    for value in values:
        text = str(value).strip()
        if text and text not in out:
            out.append(text[:80])
        if len(out) >= limit:
            break
    return out


def row_to_claim(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["entities"] = json_value(d["entities"], [])
    d["links"] = json_value(d["links"], {})
    d["tags"] = json_value(d["tags"], [])
    d["ceiling"] = TIER_CEILING.get(d["tier"], 0.25)
    d["domain_label"] = DOMAINS.get(d["domain"], d["domain"])
    return d


# ── 校验 ─────────────────────────────────────────────

class ValidationError(ValueError):
    pass


def validate_claim(data: dict, conn: sqlite3.Connection) -> dict:
    kind = data.get("kind", "claim")
    if kind not in {"claim", "question"}:
        raise ValidationError("kind 必须是 claim 或 question")
    question = str(data.get("question") or "").strip()
    st = str(data.get("statement") or question).strip()
    if len(st) < (4 if kind == "question" else 6):
        raise ValidationError("statement 太短；命题必须可判定真伪，问题必须可裁决")
    if kind == "claim" and re.search(r"可能|或许|据说|大概", st):
        raise ValidationError("statement 含模糊词（可能/或许/据说/大概）。这是观点，请改为待验证命题（kind=question）")

    tier = data.get("tier")
    if tier not in TIER_CEILING:
        raise ValidationError(f"tier 必须是 {'/'.join(TIER_CEILING)}")

    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ValidationError("confidence 必须是 0-1 的数字")
    if not math.isfinite(conf) or conf < 0:
        raise ValidationError("confidence 必须是有限的非负数字")
    ceiling = TIER_CEILING[tier]
    if conf > ceiling + 1e-9:
        raise ValidationError(f"I5 违规：{tier} 的置信度上限是 {ceiling}，给了 {conf}")

    if kind == "claim" and tier in ("T3", "T4"):
        raise ValidationError("I7 违规：T3/T4 不能作为证据 claim。博主观点应录为待验证命题（kind=question, question=...）")

    as_of = str(data.get("as_of") or "").strip()
    observed = str(data.get("observed_at") or "").strip()
    if not re.match(r"^\d{4}(-\d{2}-\d{2})?$", as_of):
        raise ValidationError("as_of 必填，格式 YYYY-MM-DD（历史可用 YYYY）")
    if len(as_of) == 10:
        try:
            datetime.strptime(as_of, "%Y-%m-%d")
        except ValueError:
            raise ValidationError("as_of 不是有效日期")
    try:
        datetime.strptime(observed, "%Y-%m-%d")
    except ValueError:
        raise ValidationError("observed_at 必填，且必须是有效的 YYYY-MM-DD 日期")

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
        "question": question,
        "resolution": str(data.get("resolution") or "").strip(),
        "domain": data.get("domain") if data.get("domain") in DOMAINS else "economy",
        "entities": json.dumps(clean_strings(data.get("entities")), ensure_ascii=False),
        "source_id": source_id,
        "tier": tier,
        "confidence": conf,
        "as_of": as_of,
        "observed_at": observed,
        "falsifier": falsifier,
        "status": "draft" if kind == "question" else "active",
        "links": json.dumps(data.get("links") if isinstance(data.get("links"), dict) else {}, ensure_ascii=False),
        "tags": json.dumps(clean_strings(data.get("tags")), ensure_ascii=False),
        "notes": str(data.get("notes") or ""),
    }


# ── 矛盾检测（结构化规则，非语义）────────────────────

CONFLICT_CACHE = {"key": None, "at": 0.0, "value": None}


def detect_conflicts(conn: sqlite3.Connection) -> dict:
    # 总览和侧栏常同时请求；短缓存避免同一批数据连续做两次 O(n²) 配对。
    fingerprint = conn.execute(
        "SELECT COUNT(*) n,COALESCE(MAX(id),0) mid,COALESCE(MAX(updated_at),'') mu FROM claims").fetchone()
    key = (fingerprint["n"], fingerprint["mid"], fingerprint["mu"])
    if CONFLICT_CACHE["key"] == key and time.monotonic() - CONFLICT_CACHE["at"] < 2:
        return CONFLICT_CACHE["value"]
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

    result = {
        "conflicts": conflicts, "inverted": inverted, "asymmetric": asymmetric,
        "entity_links": links,
        "honesty": "结构化检测只抓『形式打架』：同实体+同年+数值不同。措辞不同但意思相反的语义冲突，交给 AI 反方论证。",
    }
    CONFLICT_CACHE.update({"key": key, "at": time.monotonic(), "value": result})
    return result


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


# ── AI 接入（OpenAI 兼容 + WorkBuddy 云数据面，零依赖）──

# WorkBuddy 云通道默认值：publishableKey 只标识应用、无权限含义，
# 计费走账号云端额度（云服务规范允许进源码）。GL_AI_* 环境变量可覆盖。
WB_DEFAULT = {
    "base_url": "https://atlas-cognition-13448.app.workbuddy.host",
    "key": "wbpk_5wJMxwVyfLllRVlfD6rHRD_EhydyhsWQWKq0lA9ZBkLx2gcULYEFZyA",
    "model": "deepseek-v4.1-flash",
}


def ai_config() -> dict:
    """GL_AI_* 优先；显式配置不完整时绝不悄悄切换到其他服务商。"""
    if os.environ.get("GL_AI_DISABLE") == "1":
        return {"base_url": "", "model": "", "key": "", "style": "disabled",
                "config_error": "AI 已通过 GL_AI_DISABLE=1 停用"}
    env_base = (os.environ.get("GL_AI_BASE_URL") or "").rstrip("/")
    env_key = os.environ.get("GL_AI_KEY") or ""
    env_model = os.environ.get("GL_AI_MODEL") or ""
    if env_base or env_key or env_model:
        missing = [name for name, value in (
            ("GL_AI_BASE_URL", env_base), ("GL_AI_MODEL", env_model), ("GL_AI_KEY", env_key)) if not value]
        return {"base_url": env_base, "model": env_model, "key": env_key,
                "style": "openai",
                "config_error": "缺少 " + ", ".join(missing) if missing else ""}
    return {**WB_DEFAULT, "style": "wb", "config_error": ""}


AI_LAST_USAGE: dict = {}


def _read_sse(resp) -> tuple[str, str]:
    """聚合 WorkBuddy 数据面的 SSE 流（只收流式，本层包装成同步）。
    usage 只保留数值型 token 统计，不保存正文、提示词或密钥。"""
    parts, reasoning = [], []
    global AI_LAST_USAGE
    AI_LAST_USAGE = {}
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            AI_LAST_USAGE = {str(k): int(v) for k, v in usage.items()
                             if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                             and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if chunk.get("error"):
            err = chunk["error"]
            raise RuntimeError(f"流内错误 [{err.get('code') or 'error'}]: {err.get('message','')}")
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = (choices[0] or {}).get("delta") or {}
        if delta.get("refusal"):
            raise RuntimeError(f"模型拒绝: {delta['refusal']}")
        if delta.get("content"):
            parts.append(delta["content"])
        if delta.get("reasoning_content"):
            reasoning.append(delta["reasoning_content"])
    text = "".join(parts) or "".join(reasoning)
    if not text.strip():
        raise RuntimeError("流结束但无内容（max_tokens 过小或上游无响应）")
    return text.strip(), "".join(reasoning)


class AIRefusal(RuntimeError):
    """上游内容审核拒答：模型不是答错了，是被平台拦在门外。"""


REFUSAL_HINTS = ("敏感内容", "无法响应", "无法提供", "无法协助", "不能提供", "不便提供",
                 "无法回答", "抱歉，我无法", "抱歉，我不能", "内容审核",
                 "content policy", "cannot assist", "can't assist", "unable to help",
                 "i can't help with", "i cannot help with")


REFUSAL_MESSAGE = ("上游内容审核拒答了：这段正文触发了平台的敏感内容过滤，模型一个字都没答。"
                   "不是你操作错了，也不是应用坏了。两条路：换一个模型端点（设置里改），"
                   "或者只让它分析其中一段。")


def looks_refusal(text: str) -> bool:
    """模型回的是不是一句「抱歉」而不是内容。拒答都很短，而且不是 JSON。"""
    body = (text or "").strip()
    if not body or body[0] in "{[":
        return False
    if len(body) > 600:              # 长文不按拒答处理，免得误伤正常输出
        return False
    lowered = body.lower()
    return any(hint.lower() in lowered for hint in REFUSAL_HINTS)


def ai_with_excerpt(system: str, build_user, text: str, max_tokens: int,
                    ladder=(1600, 800, 400)) -> tuple[str, int]:
    """整篇被审核拦下来时，退成节选再试。返回 (回答, 实际用上的字数)。

    平台的内容过滤是按“这一坨里有多少敏感内容”判的：整篇 5000 字会被拒，
    开头 400 字通常能过。所以不是绕，是换成对方能处理的分量——结果会标明
    “只覆盖开头”，不冒充全篇。
    """
    try:
        return ai_chat([{"role": "system", "content": system},
                        {"role": "user", "content": build_user(text, 0)}], max_tokens), len(text)
    except AIRefusal:
        pass
    for limit in ladder:
        if len(text) <= limit:
            continue
        try:
            raw = ai_chat([{"role": "system", "content": system},
                           {"role": "user", "content": build_user(text[:limit], limit)}], max_tokens)
            return raw, limit
        except AIRefusal:
            continue
    raise AIRefusal(REFUSAL_MESSAGE)


def _ai_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """AI 请求的出口：默认强制直连，不继承任何本机代理。

    裸 ``urlopen`` 会读 HTTP_PROXY / HTTPS_PROXY 以及 Windows 系统代理注册表；
    本机隧道（WorkBuddy 会话代理、Clash/v2ray 10809）上游抖动时，会在
    CONNECT 阶段抛 ``Tunnel connection failed: 502 Bad Gateway``，
    而 WorkBuddy 云端数据面直连可达。确实需要代理时显式设 GL_AI_PROXY。
    """
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ai_chat(messages: list[dict], max_tokens: int = 4000) -> str:
    cfg = ai_config()
    if cfg.get("config_error"):
        raise RuntimeError("AI 配置不完整：" + cfg["config_error"])
    if not (cfg["base_url"] and cfg["key"] and cfg["model"]):
        raise RuntimeError("AI 未配置，请先设置 GL_AI_BASE_URL / GL_AI_MODEL / GL_AI_KEY")
    # 协议要求 messages[0] 是 system（WorkBuddy 数据面强制）
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": "You are a helpful assistant."}, *messages]

    wb = cfg["style"] == "wb"
    url = cfg["base_url"] + ("/.cloud/llm/chat/completions" if wb else "/chat/completions")
    payload = {
        "model": cfg["model"], "messages": messages,
        "temperature": 0.2, "max_tokens": max_tokens,
    }
    if wb:
        payload.update({"stream": True, "stream_options": {"include_usage": True}})
    headers = {"Content-Type": "application/json"}
    if wb:
        headers.update({"Accept": "text/event-stream", "x-wb-webapp-access-key": cfg["key"]})
    else:
        headers["Authorization"] = f"Bearer {cfg['key']}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    proxy = (os.environ.get("GL_AI_PROXY") or "").strip()

    def _call(opener):
        with opener.open(req, timeout=240 if wb else 180) as resp:
            if wb:
                text, _ = _read_sse(resp)
                if looks_refusal(text):
                    raise AIRefusal(REFUSAL_MESSAGE)
                return text
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("error"):
            raise RuntimeError("AI 上游错误：" + str(result["error"].get("message") or "未知错误")[:300])
        text = (result["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise RuntimeError("AI 返回空内容，请检查模型或 max_tokens 配置")
        if looks_refusal(text):
            raise AIRefusal(REFUSAL_MESSAGE)
        return text

    try:
        try:
            return _call(_ai_opener(proxy))
        except urllib.error.HTTPError:
            raise                      # 4xx/5xx 是业务错误，不重试
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            if not proxy:
                raise
            # 代理隧道故障（典型：Tunnel connection failed: 502 Bad Gateway）
            # 自动回落直连一次，不让本机代理波动直接把失败甩给界面。
            return _call(_ai_opener(""))
    except urllib.error.HTTPError as e:
        # 不回显上游响应体，避免把账户或请求信息写入界面日志。
        hint = {400: "请求或模型参数不被接受", 401: "密钥鉴权失败",
                403: "端点、密钥或模型权限不匹配", 404: "接口路径或模型不存在",
                429: "限流或额度耗尽"}.get(e.code, "上游服务错误")
        raise RuntimeError(f"AI 调用失败 HTTP {e.code}：{hint}") from e
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        reason = getattr(e, "reason", e)
        hint = ""
        if "Tunnel connection failed" in str(reason):
            hint = "（本机代理隧道故障；AI 通道默认直连，仍失败请检查网络或设置 GL_AI_PROXY）"
        raise RuntimeError(f"AI 网络连接失败：{reason}{hint}") from e
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise RuntimeError("AI 返回格式不兼容，请确认端点支持 OpenAI chat/completions") from e


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
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse  # noqa: E402

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


def route_arg(pattern: str, path: str) -> str | None:
    """参数只匹配一个路径段，避免 /clips/{id} 抢先吞掉 /clips/{id}/fetch。"""
    expected, actual = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(expected) != len(actual):
        return None
    value = None
    for left, right in zip(expected, actual):
        if left.startswith("{") and left.endswith("}"):
            if not right or value is not None:
                return None
            value = unquote(right)
        elif left != right:
            return None
    return value


@route_get("/api/overview")
def api_overview(conn, qs):
    stats = {
        "clips": conn.execute("SELECT COUNT(*) n FROM clips").fetchone()["n"],
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
        "ai_ready": bool(ai_config()["base_url"] and ai_config()["key"]
                         and ai_config()["model"] and not ai_config().get("config_error")),
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


@route_get("/api/claims/search")
def api_search(conn, qs):
    """正典命题全文检索。

    原来挂在 /api/search 上，但下面还有一个同名路由也挂 /api/search，
    路由表是普通 dict，后注册的会把先注册的悄悄盖掉——这个接口从此
    没人能调到。/api/search 现在归剪藏全文检索（前端在用），命题检索
    单独走 /api/claims/search。
    """
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
    allowed = {"status", "confidence", "notes", "falsifier", "links", "tags",
               "resolution", "statement", "kind"}
    new_kind = body.get("kind", r["kind"])
    if new_kind not in {"claim", "question"}:
        return {"error": "kind 必须是 claim 或 question"}
    if new_kind == "claim" and r["tier"] in {"T3", "T4"}:
        return {"error": "T3/T4 不能直接裁决为证据 claim，请先核实来源并调整层级"}
    if new_kind == "claim" and re.search(r"可能|或许|据说|大概", body.get("statement", r["statement"])):
        return {"error": "命题仍含模糊词，不能裁决为 claim"}
    if "status" in body and body["status"] not in {"active", "draft", "superseded", "contradicted", "retired"}:
        return {"error": "status 无效"}
    if "confidence" in body:
        try:
            confidence = float(body["confidence"])
        except (TypeError, ValueError):
            return {"error": "confidence 必须是数字"}
        if not math.isfinite(confidence) or not (0 <= confidence <= TIER_CEILING[r["tier"]]):
            return {"error": f"confidence 必须在 0 与 {r['tier']} 上限 {TIER_CEILING[r['tier']]} 之间"}
    if "statement" in body and len(str(body["statement"] or "").strip()) < 6:
        return {"error": "statement 至少 6 个字"}
    sets, args = [], []
    for k, v in (body or {}).items():
        if k in allowed:
            if k == "tags":
                v = json.dumps(clean_strings(v), ensure_ascii=False)
            elif k == "links":
                v = json.dumps(v if isinstance(v, dict) else {}, ensure_ascii=False)
            elif k == "confidence":
                v = float(v)
            elif k == "statement":
                v = str(v).strip()
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
    report = {"claims": 0, "clips": 0, "duplicates": 0, "errors": [],
              "observations": 0, "revised": 0}
    conn.execute("BEGIN IMMEDIATE")

    for index, item in enumerate(body.get("claims") or []):
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
            report["errors"].append(f"claims[{index}]: {e}")
        except sqlite3.IntegrityError as e:
            report["errors"].append(f"claims[{index}] 数据库约束: {e}")

    for index, item in enumerate(body.get("clips") or []):
        try:
            # 导入以快速、可重复为准；网页正文可在列表中按需重试。
            item = dict(item)
            item.setdefault("fetch", False)
            result = insert_clip_record(conn, item)
            if result.get("duplicate"):
                report["duplicates"] += 1
            else:
                report["clips"] += 1
        except (ValidationError, sqlite3.IntegrityError, TypeError) as e:
            report["errors"].append(f"clips[{index}]: {e}")

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
                    existed = conn.execute(
                        "SELECT 1 FROM observations WHERE code=? AND period=?", (code, period)).fetchone()
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO observations (code,period,value,unit,vintage) VALUES (?,?,?,?,?)",
                        (code, period, value, ser.get("unit", ""), vintage))
                    if cur.rowcount:
                        report["observations"] += 1
                        if existed:
                            report["revised"] += 1
                except sqlite3.IntegrityError as e:
                    report["errors"].append(f"series {code} {period}: {e}")
    conn.commit()
    report["ok"] = True
    report["partial"] = bool(report["errors"])
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
    points = [{"p": p, "v": _num(v["value"]),
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
        value = float(str(s).replace(",", "").strip())
        return value if math.isfinite(value) else None
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
    try:
        datetime.strptime(resolve_by, "%Y-%m-%d")
    except ValueError:
        return {"error": "到期日必填，且必须是有效的 YYYY-MM-DD。没有到期日的预测是感想"}
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


@route_post("/api/decisions/{code}/review")
def api_decision_review(conn, qs, body, code):
    """决策闭环后半段：事后填结果与复盘。"""
    r = conn.execute("SELECT * FROM decisions WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": f"找不到 {code}"}
    outcome = str(body.get("outcome") or "").strip()
    if len(outcome) < 2:
        return {"error": "结果必填——没有结果的决策日志只是日记"}
    now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("UPDATE decisions SET outcome=?, reviewed_at=? WHERE code=?",
                 (outcome, now, code))
    conn.commit()
    return {"ok": True}


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
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"error": "日期必填，且必须是有效的 YYYY-MM-DD"}
    stakes = body.get("stakes", "medium")
    if stakes not in {"low", "medium", "high"}:
        return {"error": "stakes 必须是 low/medium/high"}
    code = next_code(conn, "D", "decisions")
    conn.execute(
        """INSERT INTO decisions (code,decision,date,stakes,reversible,depends_on,chosen_because)
           VALUES (?,?,?,?,?,?,?)""",
        (code, dec, date, stakes,
         1 if body.get("reversible", True) else 0,
         json.dumps(clean_strings(body.get("depends_on")), ensure_ascii=False),
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


# ── 集藏：轻量信息剪藏 ───────────────────────────────

def clip_row(r: sqlite3.Row, full: bool = False) -> dict:
    d = dict(r)
    d["tags"] = clean_strings(json_value(d.get("tags"), []))
    d["archived"] = bool(d.get("archived", 0))
    d["domain"] = _domain_of(d.get("url") or "")
    d["has_content"] = bool((d.get("content_text") or "").strip())
    d["content_chars"] = len(d.get("content_text") or "")
    # 正文配图同理：文件不在了就别把断图地址发给前端
    _body_dir = storage.DATA_DIR / "body"
    d["body_images"] = [
        it for it in json_value(d.get("body_images"), [])
        if isinstance(it, dict) and str(it.get("f") or "")
        and (_body_dir / str(it["f"])).is_file()
    ]
    # '' 表示还没判；前端拿到 label 直接显示，不用自己去查表
    d["grade"] = str(d.get("grade") or "")
    d["grade_label"] = GRADES.get(d["grade"], "")
    # 主收存图由列表 / 详情接口再按条补齐；这里先给前端一个稳定形状
    d["preview_images"] = []
    d["preview_image_count"] = 0
    d.pop("file_path", None)  # 本机存储路径不进入浏览器响应
    # 正文照常回传：文件件的正文就是要给人看的
    return d


def clip_images_of(conn: sqlite3.Connection, cid: int, limit: int = 4) -> tuple[list[dict], int]:
    """一条收存的主图：返回可直接 <img src> 的地址和总数。

    只返回磁盘上确实存在的文件。数据目录迁移/清理后，数据库引用可能还在
    而位图文件已经不在了——把这种悬空引用发给前端，每个 <img> 都会 404，
    卡片就变成一排「图片暂不可见」。过滤掉它们，前端干脆不渲染图片区，
    total 也按实际存在的张数算，"+N" 角标不会凭空多出来。
    """
    try:
        rows = conn.execute(
            "SELECT * FROM clip_images WHERE clip_id=? ORDER BY id ASC",
            (cid,)).fetchall()
    except sqlite3.OperationalError:
        return [], 0                         # 老库在 schema 执行前的容错
    items = []
    for r in rows:
        name = str(r["file"] or "")
        if not name or not (NOTES_DIR / name).is_file():
            continue
        items.append({
            "file": name,
            "name": str(r["name"] or r["file"] or "图片"),
            "size": int(r["size"] or 0),
            "url": "/api/notes/img/" + quote(name, safe=""),
        })
    return items[:max(1, int(limit))], len(items)


def clip_with_images(conn: sqlite3.Connection, d: dict, limit: int = 4) -> dict:
    imgs, total = clip_images_of(conn, int(d.get("id") or 0), limit)
    d["preview_images"] = imgs
    d["preview_image_count"] = total
    return d


def _domain_of(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except ValueError:
        return ""


def guess_grade(url: str = "", source_type: str = "", file_name: str = "",
                tier: str = "") -> str:
    """保守的自动定级：只在有明确依据时才给结论。

    宁可漏判也不误判——把一个自媒体文章自动抬成「权威」，比不判更糟。
    你自己上传的文件是「特意留下的东西」，基准算参考；带 T0/T1 的才算权威。
    """
    t = str(tier or "").strip().upper()
    if source_type == "file":
        suffix = Path(file_name or url or "").suffix.lower()
        if suffix not in GRADE_FILE_EXT:
            return ""
        return "canon" if t in {"T0", "T1"} else "ref"
    if t in {"T0", "T1"}:
        return "canon"
    if t == "T2":
        return "ref"
    if t in {"T3", "T4"}:
        return "daily"
    host = _domain_of(url).lower().rstrip(".")
    if not host:
        return ""
    for known in AUTHORITY_HOSTS:
        if host == known or host.endswith("." + known):
            return "canon"
    return ""


def normalize_url(url: str) -> str:
    """移除片段和常见跟踪参数，保留真实查询条件，供去重使用。"""
    url = str(url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("链接必须是完整的 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise ValidationError("链接不应包含账号或密码")
    tracking = {"fbclid", "gclid", "igshid", "spm", "ref", "ref_src"}
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in tracking]
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        raise ValidationError("链接端口无效")
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", "",
                       urlencode(query, doseq=True), ""))


def validate_fetch_url(url: str) -> None:
    """默认不访问本机/内网/非常规端口，避免网页剪藏变成内网探测入口。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("仅支持有效的 HTTP(S) 网页链接")
    if os.environ.get("GL_ALLOW_PRIVATE_FETCH") == "1":
        return
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValidationError("链接端口无效")
    if os.environ.get("GL_ALLOW_ODD_PORTS") != "1" and port not in {80, 443, 8080, 8443}:
        raise ValidationError("为安全起见，默认不抓取非常规端口（可用 GL_ALLOW_ODD_PORTS=1 放开）")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValidationError("为安全起见，默认不抓取本机或局域网地址")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as e:
        raise ValidationError("域名无法解析：" + host) from e
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValidationError("为安全起见，默认不抓取本机、内网或保留地址")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        validate_fetch_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def system_proxy() -> str:
    """读 Windows 的「系统代理」——浏览器从哪条路出去，抓取就走哪条。

    不读 HTTP_PROXY：那个变量经常是别的程序（终端、Agent、构建工具）塞进来的，
    指着的隧道往往只放行它们自己那几条域名。歪脑、MarketWatch 这类站点
    在浏览器里打得开、在这里报「连接失败」，差的就是这一个开关。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            key.Close()
    except (OSError, ImportError, ValueError):
        return ""
    if not enabled or not server:
        return ""
    text = str(server).strip()
    if "=" in text:                                  # http=h:p;https=h:p 这种写法
        pairs = dict(part.strip().split("=", 1) for part in text.split(";") if "=" in part)
        text = pairs.get("https") or pairs.get("http") or ""
    if not text:
        return ""
    return text if text.startswith("http") else "http://" + text


LOCAL_PROXY_PORTS = (10809, 10808, 7890, 7891, 1080, 1087, 8889, 2080, 20171)
_PROXY_CACHE = {"at": 0.0, "value": ""}


def _proxy_egress_allowed(proxy: str) -> bool:
    """代理出口白名单：默认只信本机回环；远程代理须显式放开。"""
    if not proxy:
        return False
    if os.environ.get("GL_FETCH_ALLOW_REMOTE_PROXY") == "1":
        return True
    host = (urlparse(proxy).hostname or "").strip("[]").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def local_proxy() -> str:
    """找一个能用的本机代理：先看系统代理开关，没开就直接探端口。

    v2rayN / Clash 这类客户端常年挂着本地端口，但「系统代理」那个开关经常被
    切来切去（换 TUN 模式、开机没恢复）。只认系统开关，就会出现
    「浏览器打得开、这里连不上」；直接探端口更贴近实际。
    """
    now = time.time()
    if now - _PROXY_CACHE["at"] < 60:
        return _PROXY_CACHE["value"]
    found = system_proxy()
    if not found:
        for port in LOCAL_PROXY_PORTS:
            if _port_open(port):
                found = f"http://127.0.0.1:{port}"
                break
    if found and not _proxy_egress_allowed(found):
        found = ""
    _PROXY_CACHE["at"], _PROXY_CACHE["value"] = now, found
    return found


ROUTE_MEMO: dict[str, str] = {}      # 域名 → 上次走通的那条路（proxy / direct）


def url_routes(prefer: str = "") -> list[tuple[str, object]]:
    """出口顺序：默认先试本机代理，再直连；``prefer="direct"`` 就反过来。

    ``ProxyHandler({})`` 才是真的直连——不传的话 urllib 会读 HTTP_PROXY。
    """
    proxy = local_proxy()
    routes = []
    if proxy:
        routes.append(("proxy", urllib.request.build_opener(
            SafeRedirect, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))))
    routes.append(("direct", urllib.request.build_opener(
        SafeRedirect, urllib.request.ProxyHandler({}))))
    if prefer == "direct":
        routes.sort(key=lambda item: item[0] != "direct")
    return routes


def url_openers() -> list:
    return [opener for _name, opener in url_routes()]


def _response_peer_ip(resp) -> str:
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    candidates = (getattr(raw, "_sock", None), raw,
                  getattr(fp, "_sock", None), getattr(resp, "_sock", None))
    for sock in candidates:
        if sock is not None and hasattr(sock, "getpeername"):
            try:
                return str(sock.getpeername()[0])
            except OSError:
                return ""
    return ""


def _verify_connected_ip(resp, route_name: str) -> None:
    """直连路径校验真正连出去的 IP，堵住 DNS rebinding 的校验/连接 TOCTOU。"""
    if route_name != "direct" or os.environ.get("GL_ALLOW_PRIVATE_FETCH") == "1":
        return
    peer = _response_peer_ip(resp)
    if not peer:
        return
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise ValidationError("实际连接到了本机/内网地址，已拒绝这次抓取")


def _fetch_once(opener, url: str, limit: int, route_name: str = "") -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Dest": "document",
    })
    with opener.open(req, timeout=10) as resp:
        _verify_connected_ip(resp, route_name)
        content_type = resp.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise ValidationError(f"链接内容类型 {content_type} 不是可解析网页，请改用文件导入")
        raw = resp.read(limit + 1)
        charset = resp.headers.get_content_charset() or ""
        body_bytes = raw[:limit]
        try:
            text = body_bytes.decode(charset or "utf-8", "replace")
        except LookupError:
            text = body_bytes.decode("utf-8", "replace")
        return {"html": text, "raw": body_bytes, "charset": charset,
                "final_url": resp.geturl(), "truncated": len(raw) > limit}


def _is_timeout(err) -> bool:
    reason = getattr(err, "reason", err)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    text = str(reason).lower()
    return "timed out" in text or "timeout" in text


def _fetch_html(url: str, limit: int = 2_500_000) -> dict:
    """抓取网页：按出口链试，哪条通了就记住这个域名下次还走它。

    连接被掐断（梯子不稳）就再来一次；超时不重试——那个代价太大。
    401/403/451 只说明这条路被站点挡了，换条路可能就直接给了。
    """
    host = (urlparse(url).hostname or "").lower()
    last_error = None
    for name, opener in url_routes(ROUTE_MEMO.get(host, "")):
        for attempt in (0, 1):
            try:
                validate_fetch_url(url)
                page = _fetch_once(opener, url, limit, name)
                ROUTE_MEMO[host] = name
                return page
            except urllib.error.HTTPError as e:
                last_error = ValidationError(f"网页返回 HTTP {e.code}")
                if e.code in {401, 403, 451}:
                    break                        # 被站点挡了，换条路再试
                if e.code not in {429, 500, 502, 503, 504}:
                    raise last_error from e
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
                reason = getattr(e, "reason", e)
                last_error = ValidationError(f"网页连接失败：{reason}")
                if _is_timeout(e):
                    break
            except ValidationError:
                raise
            if attempt == 0:
                time.sleep(0.2)
    raise last_error or ValidationError("网页抓取失败")


def extract_main_text(html: str, cap: int = 60_000) -> str:
    """从 HTML 提主正文文本：去脚本/导航/样板，优先 article/main。"""
    import html as _html
    h = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    h = re.sub(r"<(script|style|noscript|svg|iframe|form|nav|header|footer|aside)[^>]*>.*?</\1>",
               " ", h, flags=re.DOTALL | re.IGNORECASE)
    m = (re.search(r"<article[^>]*>(.*?)</article>", h, re.DOTALL | re.IGNORECASE)
         or re.search(r"<main[^>]*>(.*?)</main>", h, re.DOTALL | re.IGNORECASE))
    if m and len(re.sub(r"<[^>]+>", " ", m.group(1))) > 300:
        h = m.group(1)
    h = re.sub(r"</(?:p|div|section|article|main|li|h[1-6]|tr|blockquote)>", "\n", h,
               flags=re.IGNORECASE)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", h)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:cap]


CJK_CHAR = re.compile(r"[\u3400-\u9fff]")
ARTICLE_SELECTORS = (
    "article", "main", "[itemprop='articleBody']", ".article-content", ".article-body",
    ".entry-content", ".post-content", ".rich_media_content", "#js_content",
    ".content", "#content",
)
BLOCK_TAGS = ("h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre", "figcaption")
JUNK_LINE = re.compile(
    r"^(登录|注册|退出|返回首页|返回顶部|上一篇|下一篇|相关阅读|推荐阅读|延伸阅读|热门推荐|"
    r"广告|广告位|版权声明|免责声明|扫码|扫一扫|关注我们|关注公众号|分享到|分享至|点击查看|"
    r"责任编辑\s*[:：]|编辑\s*[:：]|来源\s*[:：]|原标题\s*[:：]|本文来自|转载请)")
CLEAN_SYSTEM = """你在做网页正文的排版清洗。输入是从网页里抽出的正文，可能夹着导航、按钮文字、广告、推荐位、版权声明，或者被硬换行切断的句子。
规则：
1. 只做删除和拼接，不改写、不总结、不补充、不调整语序、不换词。
2. 删掉与正文无关的残留：导航、按钮、分享提示、广告、相关推荐、版权与免责声明、编辑署名、日期栏。
3. 把被硬换行切断的中文句子接回去；英文单词被断开的补一个空格。
4. 段落之间保留一个空行；不要输出 Markdown 标记，不要自己加标题或小标题。
5. 如果原本就干净，原样返回，不要润色。
只输出清洗后的正文。"""


COMMON_HAN = set("的一是在有和不了人这中大上个国我你们他她它说道要对会着里")
CJK_PUNCT = set("，。、；：！？“”‘’（）《》〈〉【】…—")


def _text_score(text: str) -> float:
    """判断一段解码结果像不像正常中文：常用字和中文标点是最硬的证据。"""
    if not text:
        return -1e9
    sample = text[:200_000]
    cjk = len(CJK_CHAR.findall(sample))
    common = sum(1 for ch in sample if ch in COMMON_HAN)
    punct = sum(1 for ch in sample if ch in CJK_PUNCT)
    repl = sample.count("\ufffd")
    ctrl = sum(1 for ch in sample[:20000] if ord(ch) < 32 and ch not in "\n\t\r")
    hi_latin = sum(1 for ch in sample[:20000] if 0xC0 <= ord(ch) <= 0xFF)
    return (cjk * 1.0 + common * 6 + punct * 4 + len(sample) * 0.001
            - repl * 40 - ctrl * 25 - hi_latin * 2)


def decode_page(raw: bytes, header_charset: str = "") -> tuple[str, str]:
    """把网页字节变成文字。按候选编码各解一次，挑“最像正常文字”的那个。"""
    if not raw:
        return "", ""
    candidates = []
    for enc in dict.fromkeys([header_charset, "utf-8", "gb18030", "big5"]):
        if not enc:
            continue
        try:
            candidates.append((enc, raw.decode(enc)))
        except (UnicodeDecodeError, LookupError):
            continue
    if candidates:
        scored = []
        for enc, text in candidates:
            if looks_mojibake(text):
                fixed, fix_enc = repair_mojibake(text)
                if fix_enc:                     # 修好了就按修好的打分
                    scored.append((_text_score(fixed), f"{enc}→{fix_enc}修复", fixed))
                    continue
            scored.append((_text_score(text), enc, text))
        _score, enc, text = max(scored, key=lambda item: item[0])
        return text, enc
    # 全部严格解码失败：交给 charset_normalizer，再退到宽松 utf-8
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None:
            return str(best), best.encoding or "unknown"
    except Exception:  # noqa: BLE001
        pass
    return raw.decode("utf-8", "replace"), "utf-8(替换)"


def looks_mojibake(text: str) -> bool:
    """UTF-8 被当 latin-1 读时会出现 “Ã¤Â¸Â” 这种成对字节；用它判定乱码。"""
    sample = text[:20000]
    if not sample:
        return False
    pair = sum(1 for a, b in zip(sample, sample[1:])
               if 0xC0 <= ord(a) <= 0xFF and 0x80 <= ord(b) <= 0xBF)
    if pair > max(3, len(sample) * 0.01):
        return True
    return "锟斤拷" in sample


def repair_mojibake(text: str) -> tuple[str, str]:
    """乱码试修复：把文字按 latin-1 还原成字节，再用 utf-8 / gbk 重解一次。"""
    best, best_enc, best_score = text, "", _text_score(text)
    for enc in ("utf-8", "gbk", "gb18030", "big5"):
        try:
            fixed = text.encode("latin-1").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            continue
        score = _text_score(fixed)
        if score > best_score * 1.05:
            best, best_enc, best_score = fixed, enc, score
    return best, best_enc


def _norm_block(text: str) -> str:
    t = text.replace("\u00a0", " ").replace("\u3000", " ")
    t = re.sub(r"[\u200b-\u200f\ufeff\u2060]", "", t)
    t = re.sub(r"\s+", " ", t)
    # 行内标签会在汉字之间留下空格，那不是真的断句
    left = r"[\u3400-\u9fff，。！？；：、）】》」』”’…]"
    right = r"[\u3400-\u9fff（【《「『“‘…]"
    t = re.sub(rf"(?<={left})\s+(?={right})", "", t)
    t = re.sub(r"\s+([，。！？；：、）】》」』”’])", r"\1", t)
    t = re.sub(r"([（【《「『“‘])\s+", r"\1", t)
    return t.strip()


# ── 结构化正文 ─────────────────────────────────────────────
# 现代站点把全文塞在 <script id="__NEXT_DATA__"> 或 JSON-LD 里：
# 段落本来就是干净的，配图带着图注和位置，比从 DOM 里刮准得多。

IMG_JUNK_HINTS = ("logo", "avatar", "icon", "sprite", "pixel", "tracking", "badge",
                  "button", "share", "spacer", "placeholder", "advert", "subscribe")


def _node_text(node: dict) -> str:
    """富文本节点：要么是纯字符串，要么拆成一串 nested 片段。"""
    val = node.get("text")
    if isinstance(val, str) and val.strip():
        return val.strip()
    tad = node.get("textAndDecorations")
    if isinstance(tad, dict):
        parts = [sub.get("text") for sub in (tad.get("nested") or [])
                 if isinstance(sub, dict) and isinstance(sub.get("text"), str)]
        if parts:
            joined = "".join(parts).strip()
            if joined:
                return joined
    for key in ("caption", "deck", "hed", "headline"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _node_image(node: dict, base: str) -> str:
    props = node.get("properties")
    url = str((props or {}).get("location") or "").strip() if isinstance(props, dict) else ""
    if not url:
        src = node.get("src")
        if isinstance(src, dict) and src.get("baseUrl") and src.get("path"):
            url = urljoin(str(src["baseUrl"]), str(src["path"]))
        elif isinstance(src, str):
            url = src
    if not url:
        url = str(node.get("url") or "").strip()
    if not url or url.startswith("data:"):
        return ""
    return urljoin(base or "", url)


def _find_body_nodes(obj, depth: int = 0):
    """在 __NEXT_DATA__ 里挖出 articleBody 那一串节点。"""
    if depth > 9:
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            if (key == "articleBody" and isinstance(val, list) and val
                    and any(isinstance(x, dict) and x.get("__typename") for x in val)):
                return val
            found = _find_body_nodes(val, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj[:80]:
            found = _find_body_nodes(item, depth + 1)
            if found:
                return found
    return None


def _next_data_blocks(html_text: str, base: str) -> list[dict]:
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                  html_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    nodes = _find_body_nodes(data)
    if not nodes:
        return []
    blocks: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = f"{node.get('type') or ''} {node.get('__typename') or ''}".lower()
        if "image" in kind:
            url = _node_image(node, base)
            if url:
                blocks.append({"k": "i", "u": url,
                               "n": str(node.get("caption") or "").strip(),
                               "c": str(node.get("credit") or "").strip()})
            continue
        if "video" in kind or "embed" in kind:
            continue
        piece = _node_text(node)
        if not piece:
            continue
        if "hed" in kind or "head" in kind:
            blocks.append({"k": "h", "t": piece})
        elif "quote" in kind:
            blocks.append({"k": "q", "t": piece})
        else:
            blocks.append({"k": "p", "t": piece})
    return blocks


def _jsonld_blocks(html_text: str, base: str) -> list[dict]:
    """JSON-LD 的 articleBody 只有纯文本，没有配图位置，够长就用它。"""
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html_text, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                items.extend(x for x in graph if isinstance(x, dict))
            body = item.get("articleBody")
            if isinstance(body, str) and len(body.strip()) >= 300:
                paras = [p.strip() for p in re.split(r"\n{2,}", body.strip()) if p.strip()]
                if len(paras) < 2:
                    paras = [p.strip() for p in body.split("\n") if p.strip()]
                if len(paras) >= 2:
                    return [{"k": "p", "t": p} for p in paras]
    return []


def blocks_to_text(blocks: list[dict]) -> tuple[str, list[dict]]:
    """把结构化节点压成正文 + 配图表（at = 插在第几段之后）。"""
    paras, images = [], []
    for b in blocks:
        if b["k"] == "i":
            images.append({"url": b["u"], "caption": b.get("n", ""),
                           "credit": b.get("c", ""), "at": len(paras)})
            continue
        piece = _norm_block(b.get("t") or "")
        if len(piece) < 2 or not re.search(r"[\w\u3400-\u9fff]", piece):
            continue
        paras.append(piece)
    return "\n\n".join(paras), images


def _img_src(el, base: str) -> str:
    for attr in ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "src"):
        val = (el.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            return urljoin(base or "", val)
    srcset = (el.get("srcset") or el.get("data-srcset") or "").strip()
    if srcset:
        biggest = srcset.split(",")[-1].strip().split(" ")[0]   # srcset 由小到大排列
        if biggest and not biggest.startswith("data:"):
            return urljoin(base or "", biggest)
    return ""


def _img_wanted(el) -> bool:
    for attr in ("width", "height"):
        val = (el.get(attr) or "").strip()
        if val.isdigit() and int(val) < 180:
            return False
    mark = f"{el.get('class') or ''} {el.get('id') or ''}".lower()
    return not any(hint in mark for hint in IMG_JUNK_HINTS)


IMG_SRC_JUNK = {"icon", "logo", "avatar", "sprite", "share", "qrcode", "btn", "button",
                "arrow", "pixel", "blank", "spacer", "placeholder", "loading", "emoticon",
                "rating", "star", "badge", "advert", "ad", "banner", "sound", "listen",
                "voice", "play", "eye", "view", "count", "read", "look", "font", "zoom",
                "close", "menu", "search", "nav"}


def _img_score(el, url: str) -> int:
    """给候选图打分：正文里的照片分高，页眉页脚的小图标分负——负分直接丢。"""
    low = url.lower().split("?")[0]
    if low.endswith(".svg"):
        return -1                      # 图标几乎都是 svg，照片不会是
    if set(re.split(r"[^a-z0-9]+", low)) & IMG_SRC_JUNK:
        return -1
    score = 0
    if el.find_parent("figure") is not None:
        score += 3
    width = int(el.get("width")) if (el.get("width") or "").isdigit() else 0
    height = int(el.get("height")) if (el.get("height") or "").isdigit() else 0
    if width >= 400 or height >= 300:
        score += 2
    elif width >= 220 or height >= 160:
        score += 1
    if el.find_parent("a") is not None:      # 包着链接的多半是按钮
        score -= 1
    return score


def _finish_article(body: str, images: list[dict], encoding: str, method: str) -> dict:
    """收尾：去空行、查乱码，返回正文 + 配图。"""
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    han = len(CJK_CHAR.findall(body))
    signal = sum(1 for ch in body if ch in COMMON_HAN) + 4 * sum(1 for ch in body if ch in CJK_PUNCT)
    if han >= 60 and signal < max(3, len(body) * 0.01):
        raise ValidationError("抓到的正文像编码错层的乱码，已放弃；可以点「打开原链接」手动复制")
    if body.count("?") > max(6, len(body) * 0.15) and not CJK_CHAR.search(body):
        raise ValidationError("抓到的正文全是问号，编码没解开；可以点「打开原链接」手动复制")
    if body.count("\ufffd") > max(2, len(body) * 0.002) or looks_mojibake(body[:8000]):
        raise ValidationError(f"抓到的正文疑似乱码（编码 {encoding}），已放弃；可以点「打开原链接」手动复制")
    return {"text": body[:60_000], "encoding": encoding,
            "images": images[:16], "method": method}


def _dom_article(text: str, base_url: str) -> tuple[str, list[dict]]:
    """DOM 抽法：挑主体容器，按块取字，顺手记下图的位置。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form",
                     "nav", "header", "footer", "aside", "button", "select", "input"]):
        tag.decompose()

    root = None
    for sel in ARTICLE_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(" ", strip=True)) >= 200:
            root = node
            break
    if root is None:
        # 文本密度：自身文字多、链接文字少的容器最像正文
        best, best_score = soup.body or soup, 0
        for node in soup.find_all(["div", "section", "article", "main"]):
            plain = node.get_text(" ", strip=True)
            if len(plain) < 200:
                continue
            links = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
            score = len(plain) - links * 1.6
            if score > best_score:
                best, best_score = node, score
        root = best

    lines, seen, images, kept = [], {}, [], {}   # images: [(分数, 顺序, 图)]
    for el in root.descendants:
        name = getattr(el, "name", None)
        if not name:
            continue
        if name == "img":
            # 图的位置以“前面已经有几段”记，段落清洗变了再按锚点找回
            if _img_wanted(el):
                picked = _img_src(el, base_url)
                score = _img_score(el, picked) if picked else -1
                if score >= 0:
                    item = {"url": picked, "caption": "", "credit": "",
                            "at": len(lines), "anchor": lines[-1] if lines else ""}
                    images.append((score, len(images), item))
                    kept[id(el)] = item
            continue
        if name == "figcaption":
            # 图注并进图片里，不再单独占一段
            fig = el.find_parent("figure")
            tag_img = fig.find("img") if fig is not None else None
            hit = kept.get(id(tag_img)) if tag_img is not None else None
            if hit is not None and not hit["caption"]:
                hit["caption"] = _norm_block(el.get_text(" ", strip=True))[:200]
                continue
        if name not in BLOCK_TAGS or el.find(BLOCK_TAGS):   # 有内层块，交给内层
            continue
        piece = _norm_block(el.get_text(" ", strip=True))
        if len(piece) < 2 or not re.search(r"[\w\u3400-\u9fff]", piece):
            continue
        if JUNK_LINE.match(piece):
            continue
        seen[piece] = seen.get(piece, 0) + 1
        if seen[piece] > 1:          # 同一句反复出现（页脚/重复模块），只留第一次
            continue
        lines.append(piece)

    body = "\n\n".join(lines)
    if not lines:                    # 一个块都没抽到，才退回整块文本
        body = _norm_block(root.get_text("\n", strip=True))
        body = re.sub(r"\n{2,}", "\n\n", body)
    # 候选太多就按分数挑，挑完仍按原文顺序排回去
    if len(images) > 12:
        best = sorted(images, key=lambda it: (-it[0], it[1]))[:12]
        images = sorted(best, key=lambda it: it[1])
    return body, [it[2] for it in images]


def _arc_document(html_text: str, base_url: str) -> str:
    """Arc XP 系站点（歪脑、华盛顿邮报这一脉）的正文接口。

    这类站点的 HTML 只有壳，正文要等前端跑起来再去问接口。可它把接口地址和
    参数明明白白写在了 ``Fusion.globalContentConfig`` 里——直接问那一个接口，
    一次就拿全段落和图，不必等浏览器把整个前端演一遍。
    """
    import html as _html

    m = re.search(r"Fusion\.globalContentConfig=(\{.*?\});", html_text, re.DOTALL)
    if not m:
        return ""
    try:
        conf = json.loads(m.group(1))
    except ValueError:
        return ""
    source = str(conf.get("source") or "").strip()
    query = conf.get("query")
    if not source or not isinstance(query, dict):
        return ""
    api = urljoin(base_url or "", f"/pf/api/v3/content/fetch/{source}")
    api += ("&" if "?" in api else "?") + "query=" + quote(
        json.dumps(query, ensure_ascii=False, separators=(",", ":")))
    raw = b""
    for opener in url_openers():
        for attempt in (0, 1):
            try:
                validate_fetch_url(api)
                req = urllib.request.Request(api, headers={
                    "User-Agent": BROWSER_UA, "Accept": "application/json",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                })
                with opener.open(req, timeout=12) as resp:
                    raw = resp.read(2_000_000)
                break
            except urllib.error.HTTPError:
                return ""
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError,
                    ValidationError) as e:
                if _is_timeout(e):
                    break
        if raw:
            break
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    parts = []
    promo = (data.get("promo_items") or {}).get("basic") or {}
    if isinstance(promo, dict) and promo.get("url"):
        cap = str(promo.get("caption") or "").strip()
        parts.append(f'<figure><img src="{_html.escape(str(promo["url"]), quote=True)}">'
                     f'<figcaption>{_html.escape(cap)}</figcaption></figure>')
    for el in (data.get("content_elements") or []):
        if not isinstance(el, dict):
            continue
        if str(el.get("type") or "") == "image":
            pic = str(el.get("url") or "").strip()
            cap = str(el.get("caption") or "").strip()
            if pic:
                parts.append(f'<figure><img src="{_html.escape(pic, quote=True)}">'
                             f'<figcaption>{_html.escape(cap)}</figcaption></figure>')
            continue
        content = el.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content)
    joined = "\n".join(parts)
    # 这类接口常把正文包在 <noscript> 里当降级内容；抽正文时 noscript 是要拆掉的，
    # 所以先把壳剥了、只留里面的话
    joined = re.sub(r"</?noscript[^>]*>", "", joined, flags=re.IGNORECASE)
    if len(re.sub(r"<[^>]+>", " ", joined)) < 200:
        return ""
    return f"<html><body><article>{joined}</article></body></html>"


def extract_article(raw: bytes, html_text: str, header_charset: str = "",
                    base_url: str = "") -> dict:
    """正文提取：先修编码，再试结构化数据，最后挑主体容器按块取文字 + 配图。"""
    text, encoding = decode_page(raw, header_charset) if raw else (html_text, header_charset or "utf-8")
    if not text.strip():
        raise ValidationError("网页内容是空的")

    blocks = _next_data_blocks(text, base_url) or _jsonld_blocks(text, base_url)
    if blocks:
        body, images = blocks_to_text(blocks)
        if len(body) >= 300 and body.count("\n\n") >= 1:
            return _finish_article(body, images, encoding, "structured")

    arc = _arc_document(text, base_url)
    if arc:
        body, images = _dom_article(arc, base_url)
        if len(body) >= 200:
            return _finish_article(body, images, encoding, "arc")

    body, images = _dom_article(text, base_url)
    return _finish_article(body, images, encoding, "dom")




BROWSER_CANDIDATES = (
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
)
RENDER_ON = os.environ.get("GL_RENDER", "1") != "0"


def find_browser() -> str:
    for path in BROWSER_CANDIDATES:
        if Path(path).is_file():
            return path
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return ""


# ── 无头浏览器：自己开一档，问它“正文长出来没有” ────────────
# --dump-dom 只能“到点交卷”：页面慢就交白卷，页面快也白等到点。
# 换成 DevTools 协议之后可以边等边问：body 里有字了就收，没有就再等。

def _ws_connect(ws_url: str, timeout: float = 20.0):
    """最小 WebSocket 握手：标准库没有 ws 客户端，CDP 又只认 ws。"""
    parsed = urlparse(ws_url)
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("WebSocket 握手被关闭")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    if b"101" not in head.split(b"\r\n")[0]:
        raise OSError("WebSocket 握手失败：" + head[:120].decode("latin-1", "replace"))
    return sock, rest


def _ws_send(sock, text: str) -> None:
    data = text.encode()
    head = bytearray([0x81])                     # FIN + text
    size = len(data)
    if size < 126:
        head.append(0x80 | size)
    elif size < 65536:
        head.append(0x80 | 126)
        head += struct.pack(">H", size)
    else:
        head.append(0x80 | 127)
        head += struct.pack(">Q", size)
    mask = os.urandom(4)
    head += mask
    sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_exact(sock, size: int, box: list) -> bytes:
    while len(box[0]) < size:
        chunk = sock.recv(65536)
        if not chunk:
            raise OSError("WebSocket 已关闭")
        box[0] += chunk
    out, box[0] = box[0][:size], box[0][size:]
    return out


def _ws_recv(sock, buf: bytes):
    """收一条完整消息（可能被拆成多帧）。"""
    box = [buf]
    chunks, fin, opcode = [], False, 0
    while not fin:
        b1, b2 = _ws_exact(sock, 2, box)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        size = b2 & 0x7F
        if size == 126:
            size = struct.unpack(">H", _ws_exact(sock, 2, box))[0]
        elif size == 127:
            size = struct.unpack(">Q", _ws_exact(sock, 8, box))[0]
        mask = _ws_exact(sock, 4, box) if masked else b""
        data = _ws_exact(sock, size, box)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        chunks.append(data)
    return opcode, b"".join(chunks), box[0]


class _CDP:
    """一条 CDP 连接：发一条命令，等到 id 对上为止，顺路把事件收进 self.events。"""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        self.sock, self.buf = _ws_connect(ws_url, timeout)
        self.next_id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self.next_id += 1
        mid = self.next_id
        _ws_send(self.sock, json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            self.sock.settimeout(max(0.2, end - time.time()))
            try:
                opcode, data, self.buf = _ws_recv(self.sock, self.buf)
            except socket.timeout:
                break
            if opcode == 0x8:
                raise OSError("DevTools 连接已断开")
            if opcode not in (0x1, 0x0):
                continue
            msg = json.loads(data)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"].get("message") or msg["error"]))
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(f"DevTools 没回 {method}")

    def evaluate(self, expression: str, timeout: float = 15.0):
        got = self.call("Runtime.evaluate",
                        {"expression": expression, "returnByValue": True,
                         "awaitPromise": True}, timeout=timeout)
        return (got.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _cdp_session(exe: str, url: str, wait_s: float, proxy: str = ""):
    """开一个调试端口，导航过去，等正文长出来再取 DOM。返回 (html, 用了几秒)。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    profile = storage.RENDER_DIR / f"gl-cdp-{os.getpid()}-{int(time.time() * 1000)}"
    cmd = [exe, "--headless=new", f"--remote-debugging-port={port}",
           f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--mute-audio", "--disable-dev-shm-usage",
           "--disable-background-networking", "--metrics-recording-only",
           "--disable-blink-features=AutomationControlled", f"--user-agent={BROWSER_UA}",
           "--lang=zh-CN,en-US", "--window-size=1440,1200", "--hide-scrollbars"]
    if proxy:
        cmd.append(f"--proxy-server={proxy}")
    cmd.append("about:blank")
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    proc = subprocess.Popen(cmd, **kwargs)
    started = time.time()
    cdp = None
    try:
        page = None
        for _ in range(60):
            if proc.poll() is not None:
                raise OSError("浏览器没起来")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    targets = json.loads(resp.read())
                page = next((t for t in targets if t.get("type") == "page"), None)
                if page:
                    break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.2)
        if not page:
            raise OSError("调试端口没开出来")
        cdp = _CDP(page["webSocketDebuggerUrl"])
        cdp.call("Page.enable", timeout=10)
        cdp.call("Runtime.enable", timeout=10)
        try:
            # 打不开的站点会让导航一直挂着；不等它，后面靠“有没有正文”判断
            cdp.call("Page.navigate", {"url": url}, timeout=5)
        except (TimeoutError, RuntimeError):
            pass

        end = started + wait_s
        last, stable = -1, 0
        # 用“段落字数”判正文到了没有：导航和一句 Loading 凑不出这么多，
        # 真文章一出来立刻就能过线；整页四千字以上则不必再等
        probe = ("(() => {const b = document.body; if (!b) return 0;"
                 " let s = 0; b.querySelectorAll('p').forEach(p => {"
                 " const t = p.innerText || ''; if (t.length > 40) s += t.length; });"
                 " const all = (b.innerText || '').length;"
                 " return Math.max(s, all >= 4000 ? 4000 : 0); })()")
        while time.time() < end:
            time.sleep(0.7)
            try:
                chars = cdp.evaluate(probe, timeout=8) or 0
            except (TimeoutError, OSError, RuntimeError, ValueError):
                break
            if chars >= 600:
                if abs(chars - last) < 60:
                    stable += 1
                    if stable >= 2 or chars >= 4000:
                        break
                else:
                    stable = 0
            last = chars
        html = cdp.evaluate(
            "document.documentElement ? document.documentElement.outerHTML : ''", timeout=20)
        return html or "", time.time() - started
    finally:
        if cdp is not None:
            cdp.close()
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(profile, ignore_errors=True)


def _cdp_failed(html: str) -> bool:
    """这一趟到底算不算打不开：报错页、没 DOM、或者整页只有二百来个字。"""
    if not html or len(html) < 1500:
        return True
    if _render_error_page(html):
        return True
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", text).strip()) < 200


def render_cdp(url: str, wait_s: float = 24.0) -> str:
    """浏览器自己判断“正文到了没有”，不等固定时钟——慢站快站都不吃亏。"""
    validate_fetch_url(url)
    exe = find_browser()
    if not exe:
        raise ValidationError("这个页面的正文由浏览器脚本渲染，本机没装 Chrome/Edge，抓不到")
    proxy = local_proxy()
    # 先直连，给 10 秒够它把正文吐出来；打不开（报错页 / 一片空白）再套代理重来
    html, _secs = _cdp_session(exe, url, min(wait_s, 10.0), "")
    if proxy and _cdp_failed(html):
        html, _secs = _cdp_session(exe, url, wait_s, proxy)
    if not html:
        raise OSError("调试渲染没拿到 DOM")
    return html


def _render_once(exe: str, url: str, flags: list[str], timeout_s: float,
                 profile: Path, proxy: str = "") -> str:
    """跑一次无头浏览器，超时就杀掉整棵进程树并放弃——绝不无限等下去。"""
    cmd = [exe, "--no-first-run", "--no-default-browser-check", "--disable-extensions",
           "--mute-audio", "--disable-dev-shm-usage", "--disable-background-networking",
           "--metrics-recording-only", "--disable-blink-features=AutomationControlled",
           f"--user-agent={BROWSER_UA}", "--lang=zh-CN,en-US",
           "--window-size=1440,1200", "--hide-scrollbars",
           f"--user-data-dir={profile}"]
    if proxy:                            # 浏览器也必须走同一条出口，否则它照样连不上
        cmd.append(f"--proxy-server={proxy}")
    cmd.extend([*flags, "--dump-dom", url])
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000          # CREATE_NO_WINDOW，别弹黑框
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        out, _err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return ""
    return out.decode("utf-8", "replace")


# Chrome 自己的报错页长这样：连都连不上，等再久也只是陪着等
RENDER_ERROR_HINTS = ("err_connection", "main-frame-error", "err_name_not_resolved",
                      "err_internet_disconnected", "err_tunnel_connection_failed",
                      "err_ssl_protocol_error", "err_timed_out", "err_address_unreachable")


def _render_error_page(html: str) -> bool:
    head = (html or "")[:8000].lower()
    return any(hint in head for hint in RENDER_ERROR_HINTS)


def _looks_skeleton(html: str) -> bool:
    """壳子还没有正文：只剩导航和一句 Loading。这种页面短等待没用，得等长的那条。"""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", text).strip()) < 800


def render_candidates(url: str, wait_ms: int = 6000, prefer_old: bool = False):
    """服务端拿不到正文时，用本机浏览器把 DOM 渲染出来再取。

    gov.cn、微信公众号这类页面正文由 JS 注入，静态 HTML 里根本没有；
    MarketWatch 这类上了 DataDome 的站点则是先发 401 挑战、等脚本跑完才放行；
    歪脑这类 Arc XP 站点更慢一层——壳子先到，正文要等脚本把内容接口拉回来。

    三条路按代价排：快的新无头 → 一定按时落盘的旧无头 → 等 32 秒的深渲染。
    逐个产出候选 HTML，让调用方抽一次正文再决定要不要试下一条——
    能不能抽出正文，比“拿到多少字节”更能说明这一趟到底成功没有。
    """
    validate_fetch_url(url)
    exe = find_browser()
    if not exe:
        raise ValidationError("这个页面的正文由浏览器脚本渲染，本机没装 Chrome/Edge，抓不到")
    root = storage.RENDER_DIR
    fast = ["--headless=new", f"--virtual-time-budget={wait_ms}"]
    deep = ["--headless=old", "--timeout=32000"]
    if prefer_old:                       # 已经知道对面是挑战页：直接上会等正文的那档
        plans = [("cdp", None, 0), ("new", fast, wait_ms / 1000 + 10), ("old-deep", deep, 44)]
    else:
        plans = [("new", fast, wait_ms / 1000 + 10), ("cdp", None, 0), ("old-deep", deep, 44)]

    queue = list(plans)
    while queue:
        tag, flags, budget = queue.pop(0)
        if tag == "cdp":
            try:
                html = render_cdp(url)
            except (OSError, ValidationError, TimeoutError):
                html = ""
        else:
            html = ""
            for attempt in (0, 1):
                # 第一次沿用老 profile（挑战 cookie 还在，往往秒过）；
                # 不行就换一个干净目录重来——上次超时留下的锁会把这一次也拖死
                profile = root / (tag if attempt == 0 else f"{tag}-fresh")
                if attempt:
                    shutil.rmtree(root / tag, ignore_errors=True)
                    shutil.rmtree(profile, ignore_errors=True)
                try:
                    html = _render_once(exe, url, flags, budget, profile, "")
                except OSError:
                    html = ""
                if len(html) >= 1500:
                    break
                shutil.rmtree(profile, ignore_errors=True)
        if not html:
            continue
        if _render_error_page(html) and tag != "old-deep":
            continue          # 这一档打不开而已，下一档可能行；最后一档才把话说明白
        yield tag, html
        if tag == "cdp" and _looks_skeleton(html):
            # 调试渲染都只拿到壳子，再回固定时钟那档赌一次长的
            queue = [p for p in queue if p[0] == "old-deep"]




BOT_WALL_HINTS = ("datadome", "cmsg", "cf-browser-verification", "just a moment",
                  "attention required", "enable js and disable any ad blocker",
                  "checking your browser", "please enable javascript", "captcha-delivery")


def looks_blocked(html: str) -> bool:
    """静态抓回来的是不是“请开 JS / 正在验证”的拦截页。"""
    head = (html or "")[:4000].lower()
    return any(hint in head for hint in BOT_WALL_HINTS)



def looks_like_body(text: str) -> bool:
    """判断抽出来的东西像不像正文：要有一定长度，也得有句子。

    英文页面的句号是「. 」，只数中文标点会把整篇 MarketWatch 判成“不像正文”。
    """
    if len(text) < 200:
        return False
    sentences = len(re.findall(r"[。！？!?…]", text))
    sentences += len(re.findall(r"[A-Za-z0-9\)\"'’”][.!?](?=\s|$)", text))
    return sentences >= 2


def split_for_clean(text: str, size: int = 3200, limit: int = 8):
    """按段落切块，返回 (要清洗的块, 超出上限的尾巴)。"""
    paras = [x for x in text.split("\n\n") if x.strip()]
    chunks, buf = [], ""
    for idx, para in enumerate(paras):
        if len(chunks) >= limit:
            head = [buf, *paras[idx:]] if buf else paras[idx:]
            return chunks, "\n\n".join(head).strip()
        if buf and len(buf) + len(para) + 2 > size:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks, ""


def _looks_cut(cleaned: str, original: str) -> bool:
    """原段落以句末标点收尾、清洗结果却没收尾，多半是被截断了。"""
    tail = "。！？”』）…"
    return original.rstrip().endswith(tuple(tail)) and not cleaned.rstrip().endswith(tuple(tail))


def ai_polish_article(text: str) -> tuple[str, bool]:
    """逐块清洗：只删残余、接断行；某块结果可疑就保留那块原样。"""
    if len(text) < 120:
        return text, False
    chunks, rest = split_for_clean(text, limit=8)
    if not chunks:
        return text, False
    cleaned_parts, used = [], False
    for chunk in chunks:
        piece = chunk
        if len(chunk) >= 80:
            try:
                got = ai_chat([
                    {"role": "system", "content": CLEAN_SYSTEM},
                    {"role": "user", "content": chunk},
                ], max_tokens=4000).strip()
            except RuntimeError:
                got = ""
            if got and len(got) >= len(chunk) * 0.6 and not _looks_cut(got, chunk):
                piece = got
                used = True
        cleaned_parts.append(piece)
    out = "\n\n".join(cleaned_parts + ([rest] if rest else []))
    return out.strip(), used


def _page_title(html_text: str) -> str:
    """从页面里取个像样的标题，顺手把「 - 站名」的尾巴切掉。"""
    import html as _html
    m = (re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"'](.*?)[\"']",
                   html_text, re.DOTALL | re.IGNORECASE)
         or re.search(r"<title[^>]*>(.*?)</title>", html_text, re.DOTALL | re.IGNORECASE))
    if not m:
        return ""
    title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
    title = re.sub(r"\s+[|–—]\s+[^|–—]{1,26}$", "", title)      # 站名后缀
    title = re.sub(r"\s+-\s+[^-]{1,26}$", "", title)
    return title.strip()[:200]


def _placeholder_title(title: str, url: str) -> bool:
    """标题只是域名（收存时没解析出来），这种情况才允许用网页标题覆盖。"""
    t = (title or "").strip().lower().rstrip("/")
    if not t:
        return True
    host = _domain_of(url).lower()
    bare = host[4:] if host.startswith("www.") else host
    return t in {host, bare, "www." + bare} or t == (url or "").strip().lower()


def fetch_url_content(url: str) -> dict:
    """抓网页标题与正文，返回可展示、可重试的失败原因。"""
    import html as _html
    try:
        result = _fetch_html(url)
    except (ValidationError, ValueError) as e:
        return {"title": "", "text": "", "ok": False, "error": str(e)}
    html = result["html"]
    out = {"title": "", "text": "", "ok": True, "error": "",
           "final_url": result["final_url"], "truncated": result["truncated"]}
    m = (re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"'](.*?)[\"']", html,
                   re.DOTALL | re.IGNORECASE)
         or re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE))
    if m:
        out["title"] = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()[:200]
    try:
        out["text"] = extract_article(result.get("raw") or b"", html,
                                      result.get("charset") or "",
                                      base_url=result.get("final_url") or url)["text"]
    except ValidationError as e:
        out["text"] = ""
        out["ok"] = False
        out["error"] = str(e)
    return out


def fetch_url_title(url: str) -> dict:
    """只取标题：正文抽取失败，不该把已经拿到的标题一起判死。

    新建网页剪藏只存标题与链接；空壳页 / 反爬挑战页的正文抽取经常失败，
    但那不影响 <title> / og:title 已经解析出来。
    """
    import html as _html
    try:
        result = _fetch_html(url)
    except (ValidationError, ValueError) as e:
        return {"title": "", "ok": False, "error": str(e)}
    html = result.get("html") or ""
    m = (re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"'](.*?)[\"']", html,
                   re.DOTALL | re.IGNORECASE)
         or re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE))
    title = ""
    if m:
        title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()[:200]
    return {"title": title, "ok": True, "error": ""}


@route_get("/api/clips")
def api_clips(conn, qs):
    tag = (qs.get("tag") or [""])[0].strip()
    channel = (qs.get("channel") or [""])[0].strip()
    term = (qs.get("q") or [""])[0].strip()
    sort = (qs.get("sort") or ["newest"])[0]
    board = (qs.get("board") or [""])[0].strip()
    tier = (qs.get("tier") or [""])[0].strip().upper()
    grade = (qs.get("grade") or [""])[0].strip().lower()
    hide_old = (qs.get("hide_old") or [""])[0] in {"1", "true", "yes"}
    try:
        limit = min(100, max(1, int((qs.get("limit") or ["60"])[0])))
        offset = max(0, int((qs.get("offset") or ["0"])[0]))
    except ValueError:
        raise ValidationError("limit/offset 必须是整数")
    where, args = ["1=1"], []
    if board == "none":
        where.append("board_id IS NULL")
    elif board.isdigit():
        where.append("board_id=?")
        args.append(int(board))
    if tier in {"NONE", "UNRATED"}:
        where.append("(tier IS NULL OR tier='')")
    elif tier in TIER_CEILING:
        where.append("tier=?")
        args.append(tier)
    if grade == "material":                      # 材料库 = 权威 + 参考
        where.append("grade IN ('canon','ref')")
    elif grade in {"unset", "none", "undecided"}:
        where.append("(grade IS NULL OR grade='')")
    elif grade in GRADES:
        where.append("grade=?")
        args.append(grade)
    if hide_old:                                 # 只看还新鲜的资讯
        where.append("NOT (grade='daily' AND created_at < datetime('now','localtime','-60 days'))")
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(clips.tags) j WHERE j.value=?)")
        args.append(tag)
    if channel:
        where.append("channel=?")
        args.append(channel)
    if term:
        like = f"%{term}%"
        annotation_like = ("EXISTS (SELECT 1 FROM clip_annotations a WHERE a.clip_id=clips.id "
                           "AND (a.quote LIKE ? OR a.note LIKE ?))")
        if len(term) >= 3:
            where.append(f"""(id IN (SELECT rowid FROM clips_fts WHERE clips_fts MATCH ?)
                           OR summary LIKE ? OR content_text LIKE ? OR {annotation_like})""")
            args.extend([f'"{term.replace(chr(34), chr(34) * 2)}"', like, like, like, like])
        else:
            where.append("(title LIKE ? OR excerpt LIKE ? OR tags LIKE ? OR summary LIKE ? "
                         f"OR content_text LIKE ? OR {annotation_like})")
            args.extend([like] * 7)
    order = {"newest": "id DESC", "oldest": "id ASC", "updated": "updated_at DESC, id DESC",
             "title": "title COLLATE NOCASE ASC, id DESC"}.get(sort, "id DESC")
    condition = " AND ".join(where)
    try:
        total = conn.execute(f"SELECT COUNT(*) n FROM clips WHERE {condition}", args).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT clips.*,
                  (SELECT COUNT(*) FROM clip_notes WHERE clip_id=clips.id) AS note_count,
                  (SELECT COUNT(*) FROM clip_annotations WHERE clip_id=clips.id) AS annotation_count
                FROM clips WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?""",
            [*args, limit, offset]).fetchall()
    except sqlite3.OperationalError:
        # FTS 损坏或特殊查询字符时，保留 LIKE 可用性。
        like = f"%{term}%"
        fallback_where = [w for w in where if "clips_fts" not in w]
        fallback_args = args[:-5] if term and len(term) >= 3 else args
        if term and len(term) >= 3:
            fallback_where.append("(title LIKE ? OR excerpt LIKE ? OR tags LIKE ? OR summary LIKE ? "
                                  "OR content_text LIKE ? OR EXISTS (SELECT 1 FROM clip_annotations a "
                                  "WHERE a.clip_id=clips.id AND (a.quote LIKE ? OR a.note LIKE ?)))")
            fallback_args = [*fallback_args, like, like, like, like, like, like, like]
        condition = " AND ".join(fallback_where)
        total = conn.execute(f"SELECT COUNT(*) n FROM clips WHERE {condition}", fallback_args).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT clips.*,
                  (SELECT COUNT(*) FROM clip_notes WHERE clip_id=clips.id) AS note_count,
                  (SELECT COUNT(*) FROM clip_annotations WHERE clip_id=clips.id) AS annotation_count
                FROM clips WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?""",
            [*fallback_args, limit, offset]).fetchall()
    clips = attach_board(conn, [clip_row(r) for r in rows])
    for item in clips:
        clip_with_images(conn, item, 4)
    return {"clips": clips, "total": total,
            "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}


@route_get("/api/clips/tags")
def api_clip_tags(conn, qs):
    """标签频次表——标签即分类，频次即权重。"""
    rows = conn.execute("SELECT tags FROM clips").fetchall()
    freq = {}
    for r in rows:
        for t in json.loads(r["tags"] or "[]"):
            freq[t] = freq.get(t, 0) + 1
    tags = sorted(freq.items(), key=lambda kv: -kv[1])
    channels = [r["channel"] for r in conn.execute(
        "SELECT DISTINCT channel, COUNT(*) n FROM clips GROUP BY channel ORDER BY n DESC")]
    return {"tags": [{"tag": t, "n": n} for t, n in tags],
            "channels": [c for c in channels if c]}


@route_get("/api/grades")
def api_grades(conn, qs):
    """材料级的计数：跟着当前板块走。

    不跟板块的话，进了空板块上面还挂着全库的 5/3/0/2，
    底下却写「板块 · 0 条」——两个数字打架，人只会觉得坏了。
    """
    board = (qs.get("board") or [""])[0].strip()
    where, args = [], []
    if board == "none":
        where.append("board_id IS NULL")
    elif board.isdigit():
        where.append("board_id=?")
        args.append(int(board))
    cond = (" WHERE " + " AND ".join(where)) if where else ""

    counts = {"canon": 0, "ref": 0, "daily": 0, "unset": 0}
    for r in conn.execute(f"SELECT grade, COUNT(*) n FROM clips{cond} GROUP BY grade", args):
        key = r["grade"] if r["grade"] in GRADES else "unset"
        counts[key] += r["n"]
    total = sum(counts.values())
    stale = conn.execute(
        "SELECT COUNT(*) n FROM clips WHERE " + " AND ".join(
            ["grade='daily'", "created_at < datetime('now','localtime','-60 days')", *where]),
        args).fetchone()["n"]
    return {"ok": True, "counts": {**counts, "all": total, "material": counts["canon"] + counts["ref"]},
            "stale": stale, "board": board, "labels": GRADES, "unset_label": GRADE_UNSET_LABEL}


@route_get("/api/lan")
def api_lan(conn, qs):
    """桌面端可看访问码；手机端只返回是否开启，不把钥匙贴在门上。"""
    settings = lan_normalize_settings()
    local = bool(getattr(CURRENT_REQUEST, "local", False))
    url = f"http://{LAN_IP}:{LAN_PORT}/" if (LAN_MODE and LAN_IP) else ""
    key_visible = bool(LAN_MODE and local)
    return {"ok": True, "mode": bool(LAN_MODE), "enabled": bool(LAN_MODE and LAN_OPEN),
            "url": url, "key": LAN_KEY if key_visible else "",
            "key_visible": key_visible,
            "explicit_choice": bool(settings.get("explicit_choice")),
            "ip": LAN_IP if LAN_MODE else "", "port": LAN_PORT}


@route_post("/api/lan")
def api_lan_set(conn, qs, body):
    """开关手机访问。不用重启：门闩在请求里判，绑不绑端口是启动时的事。"""
    global LAN_OPEN
    if not LAN_MODE:
        return {"error": "服务是以「只本机」方式启动的，先加 --lan 重启一次"}
    LAN_OPEN = bool(body.get("enabled"))
    explicit_choice = (bool(body.get("explicit_choice"))
                       if "explicit_choice" in body else True)
    lan_save(enabled=LAN_OPEN, explicit_choice=explicit_choice)
    return {"ok": True, "enabled": LAN_OPEN, "explicit_choice": explicit_choice}


def clean_id_list(raw, limit=400) -> list[int]:
    """批量操作的 id 列表：去重、去非法值、限量。"""
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out = []
    for item in raw:
        try:
            cid = int(item)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in out:
            out.append(cid)
        if len(out) >= limit:
            break
    return out


@route_post("/api/clips/grade")
def api_clips_grade(conn, qs, body):
    """批量定级。收资讯是一批一批来的，逐条改没人受得了。"""
    ids = clean_id_list(body.get("ids"))
    if not ids:
        return {"error": "先选中至少一条"}
    grade = clean_grade(body.get("grade"))
    marks = ",".join("?" * len(ids))
    conn.execute(
        f"""UPDATE clips SET grade=?, grade_hint='', grade_hint_reason='',
            updated_at=datetime('now','localtime') WHERE id IN ({marks})""",
        [grade, *ids])
    conn.commit()
    done = conn.execute(
        f"SELECT COUNT(*) n FROM clips WHERE id IN ({marks})", ids).fetchone()["n"]
    return {"ok": True, "changed": done, "grade": grade,
            "grade_label": GRADES.get(grade, GRADE_UNSET_LABEL)}


BOARD_NAME_MAX = 20


def board_names(conn) -> dict[int, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM boards")}


def board_meta(conn) -> dict[int, dict]:
    return {r["id"]: {"name": r["name"], "kind": r["kind"] or ""}
            for r in conn.execute("SELECT id,name,kind FROM boards")}


def attach_board(conn, clips: list[dict]) -> list[dict]:
    metas = board_meta(conn)
    for c in clips:
        meta = metas.get(c.get("board_id")) or {}
        c["board_name"] = meta.get("name", "")
        c["board_kind"] = meta.get("kind", "")
    return clips


def clean_board_kind(value) -> str:
    kind = str(value or "").strip().lower()
    if kind not in BOARD_KINDS:
        raise ValidationError("板块类型只有两种：普通板块、资料库")
    return kind


@route_get("/api/boards")
def api_boards(conn, qs):
    rows = conn.execute("SELECT * FROM boards ORDER BY sort, id").fetchall()
    counts = {r["board_id"]: r["n"] for r in conn.execute(
        "SELECT board_id, COUNT(*) n FROM clips WHERE archived=0 GROUP BY board_id")}
    total = conn.execute("SELECT COUNT(*) n FROM clips WHERE archived=0").fetchone()["n"]
    # 资料库里的权威 / 参考级：给「收进来」按钮报个数，点之前就知道会动多少条
    material = {r["board_id"]: r["n"] for r in conn.execute(
        "SELECT board_id, COUNT(*) n FROM clips WHERE archived=0 AND grade IN ('canon','ref') "
        "GROUP BY board_id")}
    return {"boards": [{"id": r["id"], "name": r["name"], "kind": r["kind"] or "",
                        "count": counts.get(r["id"], 0)} for r in rows],
            "none": counts.get(None, 0), "total": total,
            "material": sum(material.values())}


def clean_board_name(name) -> str:
    text = str(name or "").strip()
    if not text:
        raise ValidationError("板块名不能为空")
    if len(text) > BOARD_NAME_MAX:
        raise ValidationError(f"板块名最多 {BOARD_NAME_MAX} 个字")
    return text


@route_post("/api/boards")
def api_board_create(conn, qs, body):
    name = clean_board_name(body.get("name"))
    kind = clean_board_kind(body.get("kind"))
    if conn.execute("SELECT 1 FROM boards WHERE name=?", (name,)).fetchone():
        raise ValidationError(f"已经有「{name}」这个板块了")
    sort = conn.execute("SELECT COALESCE(MAX(sort),0)+1 s FROM boards").fetchone()["s"]
    cur = conn.execute("INSERT INTO boards(name,kind,sort) VALUES (?,?,?)", (name, kind, sort))
    conn.commit()
    return {"ok": True, "board": {"id": int(cur.lastrowid), "name": name,
                                  "kind": kind, "count": 0}}


@route_post("/api/boards/{bid}")
def api_board_update(conn, qs, body, bid):
    row = conn.execute("SELECT * FROM boards WHERE id=?", (bid,)).fetchone()
    if not row:
        return {"error": "板块不存在"}
    name = clean_board_name(body.get("name") or row["name"])
    kind = clean_board_kind(body["kind"]) if "kind" in body else (row["kind"] or "")
    dup = conn.execute("SELECT id FROM boards WHERE name=? AND id<>?", (name, bid)).fetchone()
    if dup:
        raise ValidationError(f"已经有「{name}」这个板块了")
    conn.execute("UPDATE boards SET name=?, kind=? WHERE id=?", (name, kind, bid))
    conn.commit()
    return {"ok": True, "board": {"id": int(bid), "name": name, "kind": kind}}


@route_post("/api/boards/{bid}/collect")
def api_board_collect(conn, qs, body, bid):
    """把散在各处的权威 / 参考级材料整批收进资料库。

    一条条去改板块太慢，这一步就是「把能当依据的东西归到一处」。
    """
    row = conn.execute("SELECT * FROM boards WHERE id=?", (bid,)).fetchone()
    if not row:
        return {"error": "板块不存在"}
    if (row["kind"] or "") != "archive":
        raise ValidationError("只有资料库能这样整批收；普通板块请逐条改")
    grades = ["canon"] if body.get("only") == "canon" else ["canon", "ref"]
    marks = ",".join("?" for _ in grades)
    cur = conn.execute(
        f"""UPDATE clips SET board_id=?, updated_at=datetime('now','localtime')
            WHERE archived=0 AND grade IN ({marks}) AND (board_id IS NULL OR board_id<>?)""",
        (bid, *grades, bid))
    conn.commit()
    return {"ok": True, "moved": cur.rowcount,
            "board": {"id": int(bid), "name": row["name"], "kind": "archive"}}


@route_post("/api/boards/{bid}/delete")
def api_board_delete(conn, qs, body, bid):
    row = conn.execute("SELECT * FROM boards WHERE id=?", (bid,)).fetchone()
    if not row:
        return {"error": "板块不存在"}
    # 条目本身不删，退回未分类——抽屉可以扔，东西不能跟着没。
    moved = conn.execute("UPDATE clips SET board_id=NULL WHERE board_id=?", (bid,)).rowcount
    conn.execute("DELETE FROM boards WHERE id=?", (bid,))
    conn.commit()
    return {"ok": True, "moved": moved}


def find_duplicate_clip(conn, url: str, content_hash: str):
    if url:
        row = conn.execute("SELECT * FROM clips WHERE url=? ORDER BY id LIMIT 1", (url,)).fetchone()
        if row:
            return row
    if content_hash:
        return conn.execute(
            "SELECT * FROM clips WHERE content_hash=? ORDER BY id LIMIT 1", (content_hash,)).fetchone()
    return None


def clean_tier(value) -> str:
    """信源分级：'' 表示未分级，其余必须是 T0–T4。"""
    text = str(value or "").strip().upper()
    if not text or text in {"NONE", "NULL", "未分级"}:
        return ""
    if text not in TIER_CEILING:
        raise ValidationError("信源等级只能是 T0–T4")
    return text


GRADE_ALIAS = {
    "canon": "canon", "authority": "canon", "权威": "canon", "正典": "canon", "a": "canon",
    "ref": "ref", "reference": "ref", "material": "ref", "参考": "ref", "材料": "ref", "b": "ref",
    "daily": "daily", "news": "daily", "stream": "daily", "资讯": "daily", "新闻": "daily", "c": "daily",
}


def clean_grade(value) -> str:
    """材料级：'' 表示未定，其余必须是 canon / ref / daily。"""
    text = str(value or "").strip().lower()
    if not text or text in {"none", "null", "unset", "未定", "未分级", "-"}:
        return ""
    text = GRADE_ALIAS.get(text, text)
    if text not in GRADES:
        raise ValidationError("材料级只能是 canon（权威）/ ref（参考）/ daily（资讯），或留空表示未定")
    return text


def resolve_board(conn, raw) -> int | None:
    """板块 id 归一化：空值/0/'none' 都当未分类，其余必须是真实存在的板块。"""
    if raw in (None, "", "none", "0", 0):
        return None
    try:
        bid = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("板块无效")
    if not conn.execute("SELECT 1 FROM boards WHERE id=?", (bid,)).fetchone():
        raise ValidationError("板块不存在")
    return bid


def insert_clip_record(conn, body: dict) -> dict:
    title = str(body.get("title") or "").strip()
    url = normalize_url(body.get("url") or "")
    excerpt = str(body.get("excerpt") or "").strip()
    content_text = str(body.get("content_text") or body.get("content") or "").strip()[:60_000]
    media = body.get("images") or body.get("media") or []
    if not isinstance(media, (list, tuple)):
        media = []
    if not title and not url and not excerpt and not content_text and not media:
        raise ValidationError("链接、标题或正文至少提供一项")
    if not title:
        seed = (excerpt or content_text).strip().splitlines()[0] if (excerpt or content_text).strip() else ""
        title = (seed[:60] + ("…" if len(seed) > 60 else "")) or _domain_of(url) or "未命名剪藏"
    text_hash = ""
    if not url and (excerpt or content_text):
        text_hash = "text:" + hashlib.sha256((title + "\n" + (excerpt or content_text)).encode("utf-8")).hexdigest()
    duplicate = find_duplicate_clip(conn, url, text_hash)
    if duplicate:
        # 重复的是文字/链接本身；新补的图仍挂到原条目上，不把用户刚贴的图丢掉。
        image_count = attach_clip_images(conn, duplicate["id"], media)
        clip = clip_with_images(conn, attach_board(conn, [clip_row(duplicate)])[0], 4)
        return {"ok": True, "duplicate": True, "image_count": image_count,
                "clip": clip,
                "fetched": {"ok": bool(duplicate["fetch_status"] == "ok"), "error": duplicate["fetch_error"]},
                "import_status": "duplicate"}

    fetched = {"ok": False, "error": ""}
    fetch_status = "stored" if content_text else "idle"
    fetch_error = ""
    if url and body.get("fetch", True):
        # 只取标题：网页正文不再抓、不存、不显示（这是明确要求）
        fetched = fetch_url_title(url)
        if fetched["ok"]:
            fetch_status = "ok"
            if not str(body.get("title") or "").strip() and fetched["title"]:
                title = fetched["title"]
        else:
            fetch_status = "failed"
            fetch_error = fetched.get("error") or "网页标题没取到"
        fetched["text"] = ""
    source_type = body.get("source_type") or ("web" if url else "text")
    tier = clean_tier(body.get("tier"))
    # 没明说要哪一级，就按来源保守猜一次；猜不出就留空，等你判
    grade = clean_grade(body.get("grade")) or guess_grade(
        url, source_type, str(body.get("file_name") or ""), tier)
    conn.execute(
        """INSERT INTO clips (title,url,channel,tags,excerpt,content_text,fetch_status,
           fetch_error,content_hash,source_type,board_id,tier,grade,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
        (title, url, str(body.get("channel") or "").strip(),
         json.dumps(clean_strings(body.get("tags")), ensure_ascii=False), excerpt,
         content_text, fetch_status, fetch_error, text_hash,
         source_type, resolve_board(conn, body.get("board_id")),
         tier, grade))
    row = conn.execute("SELECT * FROM clips WHERE id=last_insert_rowid()").fetchone()
    image_count = attach_clip_images(conn, row["id"], media)
    clip = clip_with_images(conn, attach_board(conn, [clip_row(row)])[0], 4)
    status = "saved" if fetch_status != "failed" else "saved_fetch_failed"
    return {"ok": True, "duplicate": False, "image_count": image_count,
            "clip": clip, "fetched": fetched, "import_status": status}


@route_post("/api/clips")
def api_clip_create(conn, qs, body):
    conn.execute("BEGIN IMMEDIATE")
    result = insert_clip_record(conn, body)
    conn.commit()
    return result


@route_get("/api/clips/{cid}")
def api_clip_one(conn, qs, cid):
    row = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"error": "剪藏不存在"}
    outputs = conn.execute(
        "SELECT * FROM ai_outputs WHERE clip_id=? ORDER BY id DESC", (cid,)).fetchall()
    # 打开一次就记一次，重遇靠这个判断“多久没碰”。杀软 / 备份 / 索引短暂
    # 占住库文件时会报 readonly，这只是一条附属记录，不该让整个详情请求 500
    # （data/app.log 里曾连续几十条 GET /api/clips/* 因此整条失败）。
    try:
        conn.execute("UPDATE clips SET viewed_at=datetime('now','localtime') WHERE id=?", (cid,))
        conn.commit()
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "readonly" not in msg and "locked" not in msg and "busy" not in msg:
            raise
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    verdict = conn.execute("SELECT * FROM verdicts WHERE clip_id=?", (cid,)).fetchone()
    clip = clip_with_images(conn, attach_board(conn, [clip_row(row, full=True)])[0], 24)
    return {"clip": clip,
            "outputs": [ai_output_row(item) for item in outputs],
            "notes": [note_row(r) for r in notes_of(conn, cid)],
            "annotations": [annotation_row(r) for r in annotations_of(conn, cid)],
            "verdict": verdict_row(conn, verdict) if verdict else None}


@route_post("/api/clips/{cid}")
def api_clip_update(conn, qs, body, cid):
    r = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not r:
        return {"error": "剪藏不存在"}
    sets, args = [], []
    for k in ("title", "url", "channel", "excerpt", "summary", "content_text"):
        if k in body:
            value = str(body[k] or "").strip()
            if k == "url":
                value = normalize_url(value)
            if k == "content_text":
                value = value[:60_000]
            sets.append(f"{k}=?")
            args.append(value)
    if "tags" in body:
        sets.append("tags=?")
        args.append(json.dumps(clean_strings(body["tags"]), ensure_ascii=False))
    if "board_id" in body:
        bid = resolve_board(conn, body.get("board_id"))
        if bid is None:
            sets.append("board_id=NULL")
        else:
            sets.append("board_id=?")
            args.append(bid)
    if "tier" in body:
        sets.append("tier=?")
        args.append(clean_tier(body.get("tier")))
    if "grade" in body:
        # 手动定了级，就把 AI 的建议划掉：建议已经用掉，不该再挂着
        sets.extend(["grade=?", "grade_hint=''", "grade_hint_reason=''"])
        args.append(clean_grade(body.get("grade")))
    if "grade_hint" in body:
        # 只用来「不要这个建议」：清掉即可，不写进 grade
        hinted = clean_grade(body.get("grade_hint"))
        sets.extend(["grade_hint=?", "grade_hint_reason=?"])
        args.extend([hinted, str(body.get("grade_hint_reason") or "")[:80] if hinted else ""])
    if not sets:
        return {"error": "无可更新字段"}
    if "url" in body and normalize_url(body.get("url") or "") != (r["url"] or ""):
        if "content_text" not in body:
            sets.extend(["content_text=''", "fetch_status='idle'", "fetch_error=''"])
    if "content_text" in body:
        # 传空正文不是「已入库」：以前无条件写 stored，手动清空正文后
        # 详情页又会显示「正文已入库」。
        has_body = bool(str(body.get("content_text") or "").strip())
        sets.extend(["fetch_status=?", "fetch_error=?"])
        args.extend(["stored" if has_body else "failed",
                     "" if has_body else "原件里没有可读取的文字"])
    if "title" in body and not str(body.get("title") or "").strip():
        return {"error": "标题不能为空"}
    sets.append("updated_at=datetime('now','localtime')")
    args.append(cid)
    conn.execute(f"UPDATE clips SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()
    return {"ok": True, "clip": attach_board(conn, [clip_row(conn.execute(
        "SELECT * FROM clips WHERE id=?", (cid,)).fetchone(), full=True)])[0]}


# ── 检索：字段加权 + 片段 ───────────────────────────────

SEARCH_WEIGHTS = (("title", 10), ("tags", 6), ("summary", 4), ("annotations", 4),
                  ("excerpt", 4), ("content_text", 1), ("notes", 3))
SEARCH_FIELDS = ("title", "summary", "excerpt", "tags", "content_text")
WHERE_LABEL = {"title": "标题", "tags": "标签", "summary": "简介",
               "excerpt": "我的话", "content_text": "正文", "notes": "随手记",
               "annotations": "正文批注"}


def make_snippet(text: str, terms: list[str], width: int = 64) -> str:
    """截一段带上下文的片段，前端把命中的词高亮。"""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    positions = [flat.lower().find(t.lower()) for t in terms]
    positions = [p for p in positions if p >= 0]
    if not positions:
        return flat[:width * 2].strip()
    start = max(0, min(positions) - width)
    end = min(len(flat), start + width * 2)
    snippet = flat[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(flat) else "")


@route_get("/api/search")
def api_search(conn, qs):
    """在标题 / 标签 / 简介 / 我的话 / 正文 / 随手记里一次搜，按字段权重排序。"""
    query = (qs.get("q") or [""])[0].strip()
    try:
        limit = min(60, max(1, int((qs.get("limit") or ["24"])[0])))
    except ValueError:
        raise ValidationError("limit 必须是整数")
    if not query:
        return {"ok": True, "q": "", "total": 0, "results": []}

    terms = [t for t in re.split(r"\s+", query) if t][:6]
    clauses, args = [], []
    for term in terms:
        like = f"%{term}%"
        clauses.append("(title LIKE ? OR summary LIKE ? OR excerpt LIKE ? OR tags LIKE ? "
                       "OR content_text LIKE ? OR EXISTS (SELECT 1 FROM clip_notes n "
                       "WHERE n.clip_id = clips.id AND n.text LIKE ?) "
                       "OR EXISTS (SELECT 1 FROM clip_annotations a "
                       "WHERE a.clip_id = clips.id AND (a.quote LIKE ? OR a.note LIKE ?)))")
        args.extend([like] * 8)
    rows = conn.execute(
        f"""SELECT id,title,summary,excerpt,tags,tier,grade,board_id,content_text,created_at
            FROM clips WHERE {' OR '.join(clauses)} ORDER BY id DESC LIMIT 4000""",
        args).fetchall()

    notes_map: dict[int, str] = {}
    annotations_map: dict[int, str] = {}
    if rows:
        marks = ",".join("?" * len(rows))
        ids = [r["id"] for r in rows]
        for r in conn.execute(
                f"SELECT clip_id, text FROM clip_notes WHERE clip_id IN ({marks})", ids):
            notes_map[r["clip_id"]] = (notes_map.get(r["clip_id"], "") + " " + (r["text"] or "")).strip()
        for r in conn.execute(
                f"""SELECT clip_id, quote, note FROM clip_annotations
                    WHERE clip_id IN ({marks}) ORDER BY id""", ids):
            annotations_map[r["clip_id"]] = (annotations_map.get(r["clip_id"], "") + " "
                                             + (r["quote"] or "") + " " + (r["note"] or "")).strip()

    boards = {b["id"]: b["name"] for b in conn.execute("SELECT id,name FROM boards")}
    results = []
    for r in rows:
        fields = {k: (r[k] or "") for k in SEARCH_FIELDS}
        fields["notes"] = notes_map.get(r["id"], "")
        fields["annotations"] = annotations_map.get(r["id"], "")
        total, best_field, best_hits = 0, "title", 0
        for field, weight in SEARCH_WEIGHTS:
            text = fields.get(field, "")
            if not text:
                continue
            low = text.lower()
            hits = sum(low.count(t.lower()) for t in terms)
            if not hits:
                continue
            total += min(hits, 6) * weight
            if hits > best_hits:
                best_field, best_hits = field, hits
        if not total:
            continue
        results.append({
            "id": r["id"], "title": r["title"], "tier": r["tier"],
            "grade": r["grade"] or "", "grade_label": grade_tag(r["grade"]),
            "board": boards.get(r["board_id"], ""),
            "where": WHERE_LABEL.get(best_field, best_field),
            "snippet": make_snippet(fields.get(best_field, ""), terms),
            "score": total,
        })

    results.sort(key=lambda x: (-x["score"], -x["id"]))
    return {"ok": True, "q": query, "total": len(results), "results": results[:limit],
            "terms": terms}


# ── 保存的检索 ──────────────────────────────────────────

@route_get("/api/searches")
def api_searches(conn, qs):
    rows = conn.execute(
        "SELECT id,q,filter,created_at FROM saved_searches ORDER BY id DESC").fetchall()
    return {"ok": True, "searches": [
        {"id": r["id"], "q": r["q"], "filter": json_value(r["filter"], {}),
         "created_at": r["created_at"]} for r in rows]}


@route_post("/api/searches")
def api_searches_create(conn, qs, body):
    query = str(body.get("q") or "").strip()[:200]
    if not query:
        return {"error": "先写一个检索词再保存"}
    flt = body.get("filter") if isinstance(body.get("filter"), dict) else {}
    exists = conn.execute(
        "SELECT id FROM saved_searches WHERE q=? AND filter=?",
        (query, json.dumps(flt, ensure_ascii=False, sort_keys=True))).fetchone()
    if exists:
        return {"ok": True, "duplicate": True, "id": exists["id"]}
    conn.execute("INSERT INTO saved_searches (q,filter) VALUES (?,?)",
                 (query, json.dumps(flt, ensure_ascii=False, sort_keys=True)))
    conn.commit()
    return {"ok": True, "duplicate": False,
            "id": conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]}


@route_post("/api/searches/{sid}/delete")
def api_searches_delete(conn, qs, body, sid):
    conn.execute("DELETE FROM saved_searches WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


# ── 重遇：把压箱底的东西翻出来 ──────────────────────────

@route_get("/api/resurface")
def api_resurface(conn, qs):
    try:
        n = min(6, max(1, int((qs.get("n") or ["3"])[0])))
        seed = int((qs.get("seed") or ["0"])[0])
    except ValueError:
        n, seed = 3, 0
    if not seed:
        seed = random.randint(1, 10 ** 6)

    rows = conn.execute(
        """SELECT clips.id, clips.title, clips.created_at, clips.viewed_at, clips.tags,
                  length(clips.content_text) AS body_len,
                  (SELECT COUNT(*) FROM clip_notes WHERE clip_id=clips.id) AS note_count,
                  COALESCE(b.name, '') AS board
           FROM clips LEFT JOIN boards b ON b.id = clips.board_id
           WHERE clips.archived=0""").fetchall()
    if not rows:
        return {"ok": True, "seed": seed, "items": []}

    counts = {}
    for r in rows:
        for t in json_value(r["tags"], []):
            counts[t] = counts.get(t, 0) + 1

    now = datetime.now()
    scored = []
    for r in rows:
        try:
            created = datetime.fromisoformat(r["created_at"])
        except (TypeError, ValueError):
            continue
        age = (now - created).days
        if age < 2:                       # 刚收的，先让它躺一会儿
            continue
        if r["viewed_at"]:
            try:
                gap = (now - datetime.fromisoformat(r["viewed_at"])).days
            except ValueError:
                gap = 999
            if gap < 3:                   # 刚看过的不重推
                continue
        else:
            gap = None
        score = math.log1p(age) * (1.8 if gap is None else 1.0)
        tags = json_value(r["tags"], [])
        if any(counts.get(t, 0) == 1 for t in tags):
            score *= 1.25                 # 冷门标签，容易忘
        if (r["body_len"] or 0) > 1500:
            score *= 1.15
        scored.append((score, r, age, gap))

    scored.sort(key=lambda x: -x[0])
    rng = random.Random(seed)
    pool = scored[:18]
    picked = rng.sample(pool, min(n, len(pool))) if pool else []

    def describe(r, age, gap):
        if gap is None:
            reason = f"收进来 {age} 天，一次没打开过" if age else "今天刚收的，再看一遍"
        elif gap >= 30:
            reason = f"{gap} 天没看了 · 收进来 {age} 天"
        else:
            reason = f"{gap} 天没看了"
        return {"id": r["id"], "title": r["title"], "reason": reason, "age": age, "gap": gap}

    items = [describe(r, age, gap) for _s, r, age, gap in picked]

    # 库里东西还少、或者都太新时，用随机翻代替，功能不至于空着
    if len(items) < n:
        taken = {it["id"] for it in items}
        leftovers = [r for r in rows if r["id"] not in taken]
        rng.shuffle(leftovers)
        for r in leftovers[:n - len(items)]:
            try:
                age = max(0, (now - datetime.fromisoformat(r["created_at"])).days)
            except (TypeError, ValueError):
                age = 0
            where = r["board"] or "未分类"
            items.append({"id": r["id"], "title": r["title"],
                          "reason": f"{where} · 收进来 {age} 天" if age else f"{where} · 今天收的",
                          "age": age, "gap": None})
    return {"ok": True, "seed": seed, "items": items}


# ── 我的判断（轻量正典）────────────────────────────────

def verdict_row(conn, row) -> dict:
    refs = []
    for item in json_value(row["refs"], []):
        if not isinstance(item, dict):
            continue
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        ref = conn.execute("SELECT id,title FROM clips WHERE id=?", (rid,)).fetchone()
        if ref:
            refs.append({"id": rid, "title": ref["title"], "kind": item.get("kind") or "counter"})
    return {"clip_id": row["clip_id"], "text": row["text"],
            "confidence": row["confidence"], "refs": refs,
            "updated_at": row["updated_at"] or row["created_at"]}


@route_get("/api/verdicts")
def api_verdicts(conn, qs):
    rows = conn.execute("SELECT * FROM verdicts ORDER BY updated_at DESC, clip_id DESC").fetchall()
    out = []
    for r in rows:
        clip = conn.execute(
            "SELECT title,tier,board_id,created_at FROM clips WHERE id=?", (r["clip_id"],)).fetchone()
        if not clip:
            continue
        item = verdict_row(conn, r)
        item.update({"title": clip["title"], "tier": clip["tier"],
                     "board": (conn.execute("SELECT name FROM boards WHERE id=?",
                                            (clip["board_id"],)).fetchone() or {"name": ""})["name"]})
        out.append(item)
    return {"ok": True, "verdicts": out}


@route_post("/api/clips/{cid}/verdict")
def api_verdict_set(conn, qs, body, cid):
    if not conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone():
        return {"error": "剪藏不存在"}
    text = str(body.get("text") or "").strip()[:500]
    conf = str(body.get("confidence") or "mid").lower()
    if conf not in {"low", "mid", "high"}:
        conf = "mid"
    refs = []
    for item in (body.get("refs") or [])[:6]:
        try:
            rid = int(item.get("id") if isinstance(item, dict) else item)
        except (TypeError, ValueError):
            continue
        if rid != cid and conn.execute("SELECT 1 FROM clips WHERE id=?", (rid,)).fetchone():
            refs.append({"id": rid, "kind": "counter"})
    if not text and not refs:
        conn.execute("DELETE FROM verdicts WHERE clip_id=?", (cid,))
        conn.commit()
        return {"ok": True, "verdict": None}
    conn.execute(
        """INSERT INTO verdicts (clip_id,text,confidence,refs,updated_at)
           VALUES (?,?,?,?,datetime('now','localtime'))
           ON CONFLICT(clip_id) DO UPDATE SET text=excluded.text,
             confidence=excluded.confidence, refs=excluded.refs, updated_at=excluded.updated_at""",
        (cid, text, conf, json.dumps(refs, ensure_ascii=False)))
    conn.commit()
    row = conn.execute("SELECT * FROM verdicts WHERE clip_id=?", (cid,)).fetchone()
    return {"ok": True, "verdict": verdict_row(conn, row)}


@route_post("/api/clips/{cid}/verdict/delete")
def api_verdict_delete(conn, qs, body, cid):
    conn.execute("DELETE FROM verdicts WHERE clip_id=?", (cid,))
    conn.commit()
    return {"ok": True}


def reextract_file(conn, cid, row) -> dict:
    """文件条目重新抽正文：导入时自动做过一次，这里给历史件和手动重试用。"""
    path = UPLOAD_DIR / (row["file_path"] or "")
    if not row["file_path"] or not path.is_file():
        return {"error": "原件不在本机，无法提取正文"}
    suffix = Path(path).suffix.lower()[:12]
    if (row["file_size"] or 0) > EXTRACT_LIMIT and suffix != ".pdf":
        return {"error": f"原件超过 {EXTRACT_LIMIT // 1024 // 1024} MB，暂不自动提取"}
    raw, status = extract_file_text(path, suffix)
    text, parse_status = _usable_body(raw, status)
    old_text = (row["content_text"] or "").strip()
    if text:
        content, new_status = text, "stored"
        reason = ("正文较长，已按前 200 页 / 6 万字截断，后半部分未入库"
                  if status == "partial" else "")
    elif old_text:
        # 重提失败绝不能把已经入库的正文清空 —— 这是数据丢失，不是「重试」。
        # 保留原文与状态，只把本次失败原因记下来给界面提示。
        content, new_status = row["content_text"], "stored"
        reason = (parse_status or "重新提取失败") + f"，已保留原有正文（{len(old_text)} 字）"
    else:
        content, new_status = "", "failed"
        reason = parse_status or "原件里没有可读取的文字"
    conn.execute(
        """UPDATE clips SET content_text=?, fetch_status=?, fetch_error=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (content, new_status, reason, cid))
    conn.commit()
    fresh = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "chars": len(text), "ai": False, "images": 0,
            "parse_message": reason, "clip": clip_row(fresh, full=True)}


@route_post("/api/tools/backfill")
def api_tools_backfill(conn, qs, body):
    """给历史上「只存原件」的文件条目补抽正文；新导入的件不必再调。

    body 里带 {"redo": "pdf"} 时改成「把已有的 PDF 正文按新版版面还原重抽
    一遍」—— 排版解析升级后，老条目存着的还是旧版拼出来的段落，重抽才会
    拿到分栏、双语对照的正确版式。批注不动：前端按原句重新锚定。
    """
    redo = str((body or {}).get("redo") or "").strip().lower()
    if redo == "pdf":
        rows = conn.execute(
            """SELECT * FROM clips WHERE source_type='file' AND file_path<>''
               AND lower(file_path) LIKE '%.pdf' ORDER BY id""").fetchall()
    elif redo == "all":
        # redo=all 以前也只扫 PDF：EPUB / DOCX / 文本类条目被静默跳过，
        # 与参数名和界面上的「全部重抽」完全不符。
        rows = conn.execute(
            """SELECT * FROM clips WHERE source_type='file' AND file_path<>''
               ORDER BY id""").fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM clips WHERE source_type='file' AND file_path<>''
               AND (content_text IS NULL OR trim(content_text)='') ORDER BY id""").fetchall()
    done, failed = [], []
    for r in rows:
        res = reextract_file(conn, r["id"], r)
        fresh = res.get("clip") or {}
        if res.get("chars"):
            done.append({"id": r["id"], "chars": res["chars"], "title": r["title"]})
        elif (fresh.get("content_text") or "").strip():
            # 这次没抽出新正文，但原有正文被完整保留 —— 不算失败
            done.append({"id": r["id"], "chars": 0, "kept_existing": True,
                         "title": r["title"]})
        else:
            failed.append({"id": r["id"], "title": r["title"],
                           "reason": res.get("error") or res.get("parse_message") or "无文字层"})
    return {"ok": True, "scanned": len(rows), "done": done, "failed": failed}


@route_post("/api/clips/{cid}/extract")
def api_clip_extract(conn, qs, body, cid):
    """抓正文：网页走静态/浏览器渲染；文件件重新读原件并整理版式。"""
    row = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"error": "剪藏不存在"}
    if row["source_type"] == "file":
        return reextract_file(conn, cid, row)
    if not row["url"]:
        return {"error": "这条没有网页链接"}

    art, via, soft_error, blocked, page_html = None, "http", "", False, ""
    try:
        page = _fetch_html(row["url"])
        page_html = page["html"]
        blocked = looks_blocked(page["html"])
        if not blocked:      # 静态页只是张挑战页，抽它没有意义，直接上浏览器
            art = extract_article(page.get("raw") or b"", page["html"],
                                  page.get("charset") or "",
                                  base_url=page.get("final_url") or row["url"])
    except (ValidationError, ValueError) as e:
        soft_error = str(e)

    if (art is None or not looks_like_body(art["text"])) and RENDER_ON:
        tried, detail = 0, ""
        try:
            for _tag, html in render_candidates(row["url"],
                                                prefer_old=blocked or bool(soft_error)):
                tried += 1
                if _render_error_page(html):
                    detail = "浏览器也打不开这个地址——网络层就断了。检查一下代理/梯子是不是开着"
                    break
                try:
                    got = extract_article(html.encode("utf-8"), html, "utf-8",
                                          base_url=row["url"])
                except ValidationError as e:
                    detail = str(e)
                    continue
                if looks_like_body(got["text"]):
                    art, via, page_html = got, "render", html
                    break
                detail = "页面打开了，正文是脚本后加载的，等到最后也没长出来"
        except ValidationError as e:
            detail = str(e)
        if not tried and not detail:
            detail = "浏览器没能打开这个地址：可能网络不通，或本机没装 Chrome/Edge"
        if detail and not (art and looks_like_body(art["text"])):
            soft_error = detail

    if art is None or not looks_like_body(art["text"]):
        detail = soft_error or "抓到的内容不像正文：可能是图片型页面、已失效，或者要登录才能看"
        # 网页重抓失败也要落库：以前只返回一个 error，刷新详情页后
        # fetch_error 还是上一次的旧值，列表一直显示「已入库」。
        has_body = bool((row["content_text"] or "").strip())
        conn.execute(
            """UPDATE clips SET fetch_status=?, fetch_error=?,
               updated_at=datetime('now','localtime') WHERE id=?""",
            ("stored" if has_body else "failed", detail, cid))
        conn.commit()
        return {"error": detail, "retryable": True}

    text = art["text"]
    used_ai = False
    # 结构化正文、站点自己的内容接口：本来就是干净的段落，再过一遍模型只会搅乱
    if body.get("ai") is not False and art.get("method") not in {"structured", "arc"}:
        text, used_ai = ai_polish_article(text)

    images = place_body_images(text, art.get("images") or [], art.get("method") or "dom")
    saved = []
    if images:
        try:
            saved = download_body_images(cid, images, art.get("final_url") or row["url"])
        except Exception as e:                # noqa: BLE001 - 配图是加分项，坏了也要留下正文
            print(f"[body-img] 下载失败 cid={cid}: {type(e).__name__}: {e}", flush=True)
            saved = []
    title = row["title"] or ""
    if page_html and _placeholder_title(title, row["url"]):
        found = _page_title(page_html)
        if found and not _placeholder_title(found, row["url"]):
            title = found
    if images and not saved:
        # download_body_images 一进来就 purge 了配图目录。下载失败时若再把
        # body_images 字段写成空串，旧配图的记录与磁盘就一起没了；留着
        # 原值至少不会让已有配图凭空消失。
        images_field = row["body_images"] or ""
    else:
        images_field = json.dumps(saved, ensure_ascii=False) if saved else ""
    conn.execute(
        """UPDATE clips SET title=?, content_text=?, body_images=?, fetch_status='stored',
           fetch_error='', updated_at=datetime('now','localtime') WHERE id=?""",
        (title, text[:60_000], images_field, cid))
    conn.commit()
    fresh = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "chars": len(text), "ai": used_ai, "encoding": art["encoding"],
            "via": via, "extract": art.get("method") or "dom", "images": len(saved),
            "clip": clip_row(fresh, full=True)}



@route_post("/api/clips/{cid}/fetch")
def api_clip_fetch(conn, qs, body, cid):
    """只刷新标题：网页正文不再抓取（链接件不存正文）。"""
    row = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"error": "剪藏不存在"}
    if not row["url"]:
        return {"error": "这条没有网页链接"}
    fetched = fetch_url_title(row["url"])
    if not fetched["ok"]:
        conn.execute(
            "UPDATE clips SET fetch_status='failed',fetch_error=?,updated_at=datetime('now','localtime') WHERE id=?",
            (fetched.get("error") or "网页打不开", cid))
        conn.commit()
        return {"error": fetched.get("error") or "网页打不开", "retryable": True}
    title = fetched["title"] or row["title"]
    # 标题抓成功不等于正文状态。特别是「无正文 + failed + fetch_error」
    # 的历史条目：以前会被洗成 ok + 空 error，前端「没抓到」兜底条件
    # 随即失效，9 条存量脏行就永远装作已入库。
    has_body = bool((row["content_text"] or "").strip())
    old_status = row["fetch_status"] or ""
    old_error = row["fetch_error"] or ""
    new_status = ("stored" if has_body
                  else ("failed" if old_status == "failed" else "ok"))
    keep_error = old_error if ("截断" in old_error or not has_body) else ""
    conn.execute(
        """UPDATE clips SET title=?,fetch_status=?,fetch_error=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (title, new_status, keep_error, cid))
    conn.commit()
    return {"ok": True, "clip": clip_row(conn.execute(
        "SELECT * FROM clips WHERE id=?", (cid,)).fetchone())}


@route_get("/api/clips/search")
def api_clip_search(conn, qs):
    scoped = dict(qs)
    scoped["limit"] = ["60"]
    return api_clips(conn, scoped)


@route_post("/api/clips/delete")
def api_clip_delete(conn, qs, body):
    cid = body.get("id")
    # 缺 id / 非数字时别落到 purge_* 里炸成 500（前端正常调用总会带 id，
    # 但手工调接口或旧版前端发空载荷时应当得到明确的 400 提示）。
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return {"error": "id 无效"}
    row = conn.execute("SELECT file_path FROM clips WHERE id=?", (cid,)).fetchone()
    purge_annotations(conn, cid)
    purge_notes(conn, cid)
    purge_clip_images(conn, cid)
    purge_body_images(cid)
    # 阅读中心：虚表没有外键，删条目之前手工收走目录/书签/笔记/关联/索引
    try:
        import reading as _reading_mod
        _reading_mod.purge_clip(conn, int(cid))
    except Exception as exc:
        print(f"[reading] 清理关联数据失败 cid={cid}: {type(exc).__name__}", flush=True)
    try:
        import event_topics as _event_topics_mod
        _event_topics_mod.purge_clip(conn, int(cid))
    except Exception as exc:
        print(f"[event_topics] 清理关联数据失败 cid={cid}: {type(exc).__name__}", flush=True)
    conn.execute("DELETE FROM clips WHERE id=?", (cid,))
    conn.commit()
    if row and row["file_path"]:
        remove_upload(row["file_path"])
    return {"ok": True}


UPLOAD_DIR = storage.DATA_DIR / "uploads"
NOTES_DIR = storage.DATA_DIR / "notes"                # 随手记里的图片，独立目录
MAX_NOTE_IMAGE = 15 * 1024 * 1024                     # 单张 15MB
NOTE_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
NOTE_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".gif": "image/gif", ".webp": "image/webp"}
NOTE_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".webm"}
NOTE_AUDIO_MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
                   ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
                   ".aac": "audio/aac", ".flac": "audio/flac", ".webm": "audio/webm"}
NOTE_MEDIA_EXT = NOTE_IMAGE_EXT | NOTE_AUDIO_EXT
NOTE_MEDIA_MIME = {**NOTE_IMAGE_MIME, **NOTE_AUDIO_MIME}
NOTE_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".url", ".xml",
    ".yaml", ".yml", ".ini", ".conf", ".toml", ".sql", ".srt", ".vtt",
    ".py", ".js", ".ts", ".css", ".sh", ".bat", ".ps1", ".java", ".c", ".h",
    ".cpp", ".go", ".rs", ".rb", ".php", ".tex",
}
NOTE_TEXT_MIME = {
    ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8", ".markdown": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".tsv": "text/tab-separated-values; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".xml": "application/xml; charset=utf-8",
    ".url": "text/plain; charset=utf-8",
}
NOTE_PREVIEW_MAX_BYTES = 4 * 1024 * 1024              # 文本预览上限，原件仍完整保留
NOTE_PREVIEW_MAX_CHARS = 500_000
MAX_NOTE_AUDIO = 80 * 1024 * 1024                     # 单段音频 80MB
MAX_NOTE_FILE = 100 * 1024 * 1024                     # 单个附件 100MB
MAX_FILE_SIZE = 6 * 1024 * 1024                       # 老 JSON 通道的上限（base64 会再胀 4/3）
MAX_UPLOAD = int(float(os.environ.get("GL_MAX_UPLOAD_MB") or 200) * 1024 * 1024)
EXTRACT_LIMIT = 40 * 1024 * 1024                      # 超过这个体积只存原件，不再抽正文


def remove_upload(stored_name: str) -> None:
    if not stored_name or Path(stored_name).name != stored_name:
        return
    try:
        (UPLOAD_DIR / stored_name).unlink(missing_ok=True)
    except OSError:
        pass


def remove_note_image(stored_name: str) -> None:
    if not stored_name or Path(stored_name).name != stored_name:
        return
    try:
        (NOTES_DIR / stored_name).unlink(missing_ok=True)
    except OSError:
        pass


def note_media_still_used(conn, name: str, *, skip_note_id=None, skip_clip_id=None) -> bool:
    """同一个 data/notes 文件可能同时被主收存图和随手记引用；删之前先问一圈。"""
    if not name:
        return False
    try:
        rows = conn.execute(
            "SELECT clip_id FROM clip_images WHERE file=?", (name,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        if skip_clip_id is not None and str(r["clip_id"]) == str(skip_clip_id):
            continue
        return True
    for r in conn.execute("SELECT id, clip_id, images FROM clip_notes"):
        if skip_note_id is not None and str(r["id"]) == str(skip_note_id):
            continue
        if skip_clip_id is not None and str(r["clip_id"]) == str(skip_clip_id):
            continue
        for item in json_value(r["images"], []):
            used = item if isinstance(item, str) else str((item or {}).get("f") or "")
            if used == name:
                return True
    return False


def remove_note_image_if_unused(conn, name: str, **skip) -> None:
    if not note_media_still_used(conn, name, **skip):
        remove_note_image(name)


def prune_note_images(conn) -> None:
    """启动时清一次孤儿图：上传了但最终没挂到任何随手记或主收存图上的。"""
    if not NOTES_DIR.is_dir():
        return
    used = set()
    try:
        for r in conn.execute("SELECT file FROM clip_images"):
            used.add(str(r["file"] or ""))
    except sqlite3.OperationalError:
        pass
    for r in conn.execute("SELECT images FROM clip_notes"):
        for item in json_value(r["images"], []):
            name = item if isinstance(item, str) else str((item or {}).get("f") or "")
            if name:
                used.add(name)
    for f in NOTES_DIR.iterdir():
        if f.is_file() and f.name not in used:
            # 删不掉就留着：一次清理失败不该把整个启动拖死（回收站不可用时就会这样）
            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                print(f"[prune] 跳过 {f.name}: {type(e).__name__}: {e}", flush=True)


# ── 正文配图 ────────────────────────────────────────────────
# 抓正文时顺手把文章里的图落盘，正文面板里按原位插回去。
# 单独一个目录：随手记的孤儿清理不认领这些文件，混在一起会被误删。

BODY_DIR = storage.DATA_DIR / "body"
BODY_MAX_IMAGES = 12
BODY_IMG_MAX = 8 * 1024 * 1024
BODY_IMG_MIN = 6 * 1024                               # 比这还小多半是图标或像素点
BODY_IMG_EXT = {".jpg", ".png", ".gif", ".webp", ".avif"}
BODY_IMG_MIME = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
                 ".webp": "image/webp", ".avif": "image/avif"}
IMG_MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"),
             (b"GIF87a", ".gif"), (b"GIF89a", ".gif"))


def _sniff_image(raw: bytes) -> str:
    """只看文件头定类型：绝不按 URL 后缀或 Content-Type 直接信对方。"""
    for magic, ext in IMG_MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[4:8] == b"ftyp" and raw[8:12] in (b"avif", b"avis"):
        return ".avif"
    return ""


def _jpeg_size(raw: bytes) -> tuple[int, int]:
    i, n = 2, len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(raw[i + 2:i + 4], "big")
        if seg < 2:
            return 0, 0
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (int.from_bytes(raw[i + 7:i + 9], "big"),
                    int.from_bytes(raw[i + 5:i + 7], "big"))
        i += 2 + seg
    return 0, 0


def image_size(raw: bytes) -> tuple[int, int]:
    """读图片宽高（够用就好）：前端拿它先占位，加载完不跳版。"""
    try:
        ext = _sniff_image(raw)
        if ext == ".png" and len(raw) >= 24:
            return (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
        if ext == ".gif" and len(raw) >= 10:
            return (int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little"))
        if ext == ".jpg":
            return _jpeg_size(raw)
        if ext == ".webp" and len(raw) >= 30:
            chunk = raw[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(raw[24:27], "little") + 1,
                        int.from_bytes(raw[27:30], "little") + 1)
            if chunk == b"VP8L" and len(raw) >= 25:
                bits = int.from_bytes(raw[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8 ":
                return (int.from_bytes(raw[26:28], "little") & 0x3FFF,
                        int.from_bytes(raw[28:30], "little") & 0x3FFF)
    except (IndexError, ValueError):
        pass
    return 0, 0


def purge_body_images(cid: int) -> None:
    """重抓前把这条的老图收走，别让文件越堆越多。"""
    if not BODY_DIR.is_dir():
        return
    prefix = f"b{int(cid)}-"
    for f in BODY_DIR.iterdir():
        if f.is_file() and f.name.startswith(prefix):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def prune_body_images(conn) -> None:
    """启动时清一次孤儿图：库改了、文件却还留着的那种。"""
    if not BODY_DIR.is_dir():
        return
    used = set()
    for r in conn.execute("SELECT body_images FROM clips"):
        for item in json_value(r["body_images"], []):
            if isinstance(item, dict) and item.get("f"):
                used.add(str(item["f"]))
    for f in BODY_DIR.iterdir():
        if f.is_file() and f.name not in used:
            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                print(f"[prune] 跳过正文图 {f.name}: {type(e).__name__}: {e}", flush=True)


def _anchor_at(paras: list[str], anchor: str) -> int:
    """清洗换了段落，就靠“图前面那段话”把位置找回来。"""
    key = re.sub(r"\s+", "", anchor or "")
    if not key:
        return 0
    probe = key[:24]
    for i, para in enumerate(paras):
        flat = re.sub(r"\s+", "", para)
        if flat and (flat.startswith(probe) or probe.startswith(flat[:24])):
            return i + 1
    short = key[:12]                    # 段落被压缩过，退一步用短前缀找
    for i, para in enumerate(paras):
        if short and short in re.sub(r"\s+", "", para):
            return i + 1
    return len(paras)


def place_body_images(text: str, images: list[dict], method: str) -> list[dict]:
    """定下每张图插在第几段之后。结构化正文的段落不会变，直接用原位置。"""
    paras = [p for p in text.split("\n\n") if p.strip()]
    out = []
    for item in images:
        at = int(item.get("at") or 0)
        if method != "structured":
            at = _anchor_at(paras, item.get("anchor") or "")
        out.append({**item, "at": max(0, min(at, len(paras)))})
    out.sort(key=lambda it: it["at"])
    return out


def download_body_images(cid: int, images: list[dict], referer: str = "") -> list[dict]:
    """把正文里的图拉回本机。一张失败就跳一张，不拿配图拖垮整次抓取。"""
    purge_body_images(cid)
    if not images:
        return []
    BODY_DIR.mkdir(parents=True, exist_ok=True)
    picked = []
    for item in images:
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        picked.append({**item, "url": url})
        if len(picked) >= BODY_MAX_IMAGES:
            break

    def grab(pair):
        pos, item = pair
        try:
            validate_fetch_url(item["url"])
            req = urllib.request.Request(item["url"], headers={
                "User-Agent": BROWSER_UA,
                "Accept": "image/webp,image/png,image/jpeg,image/gif,*/*;q=0.5",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": referer or "",
            })
            raw = b""
            for opener in url_openers():     # 和正文同一条出口：系统代理不行就直连
                try:
                    with opener.open(req, timeout=12) as resp:
                        if (resp.headers.get_content_type() or "").split("/")[0] != "image":
                            return None
                        raw = resp.read(BODY_IMG_MAX + 1)
                    break
                except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
                    continue
            if not raw:
                return None
        except Exception:                     # noqa: BLE001 - 图坏了不该算正文失败
            return None
        if not (BODY_IMG_MIN <= len(raw) <= BODY_IMG_MAX):
            return None
        ext = _sniff_image(raw)
        if ext not in BODY_IMG_EXT:
            return None
        width, height = image_size(raw)
        if width and height and (width < 200 or height < 110):
            return None                       # 横幅广告条、图标之类
        name = f"b{int(cid)}-{pos:02d}-{hashlib.sha256(raw[:4096]).hexdigest()[:8]}{ext}"
        try:
            (BODY_DIR / name).write_bytes(raw)
        except OSError:
            return None
        return {"f": name, "n": str(item.get("caption") or "")[:200],
                "c": str(item.get("credit") or "")[:80],
                "at": int(item.get("at") or 0), "w": width, "h": height}

    saved = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for got in pool.map(grab, list(enumerate(picked))):
            if got:
                saved.append(got)
    saved.sort(key=lambda it: (it["at"], it["f"]))
    return saved


def _note_entry(item) -> dict | None:
    """一条附件：新格式是对象，老数据是纯文件名。"""
    if isinstance(item, str):
        name, orig, size = item, "", 0
    elif isinstance(item, dict):
        name = str(item.get("f") or "")
        orig = str(item.get("n") or "")
        size = int(item.get("s") or 0)
    else:
        return None
    if not name or Path(name).name != name:
        return None
    ext = Path(name).suffix.lower()
    kind = "image" if ext in NOTE_IMAGE_EXT else "audio" if ext in NOTE_AUDIO_EXT else "file"
    return {"file": name, "name": orig or name, "size": size, "kind": kind}


def note_row(r: sqlite3.Row) -> dict:
    """一个 files 列，读出来按类型拆好，前端不用自己判后缀。"""
    d = dict(r)
    entries = [e for e in (_note_entry(i) for i in json_value(d.get("images"), [])) if e]
    d["images"] = [e for e in entries if e["kind"] == "image"]
    d["audios"] = [e for e in entries if e["kind"] == "audio"]
    d["files"] = [e for e in entries if e["kind"] == "file"]
    return d


def clean_note_files(names) -> list[dict]:
    """校验提交上来的附件：文件真实存在、名字干净；顺手补上原始文件名和大小。"""
    out, seen = [], set()
    for raw in (names or [])[:20]:
        if isinstance(raw, dict):
            name = str(raw.get("file") or "")
            orig = str(raw.get("name") or "").strip()[:120]
        else:
            name, orig = str(raw or ""), ""
        if Path(name).name != name or name in seen:
            continue
        path = NOTES_DIR / name
        if not path.is_file():
            continue
        seen.add(name)
        out.append({"f": name, "n": orig or name, "s": path.stat().st_size})
    return out


def clean_clip_images(raw) -> list[dict]:
    """主收存图只认浏览器能安全内联展示的位图格式。"""
    return [it for it in clean_note_files(raw)
            if Path(it["f"]).suffix.lower() in NOTE_IMAGE_EXT][:20]


def attach_clip_images(conn, clip_id, raw) -> int:
    """把已经上传到 data/notes 的图挂到一条收存上；非法文件名直接忽略。"""
    items = clean_clip_images(raw)
    for it in items:
        conn.execute(
            """INSERT OR IGNORE INTO clip_images (clip_id,file,name,size)
               VALUES (?,?,?,?)""",
            (clip_id, it["f"], it["n"], it["s"]))
    return len(items)


def notes_of(conn, cid: int):
    return conn.execute(
        "SELECT * FROM clip_notes WHERE clip_id=? ORDER BY id ASC", (cid,)).fetchall()


def annotation_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["color"] = d.get("color") if d.get("color") in ANNOTATION_COLORS else "amber"
    d["color_label"] = ANNOTATION_COLORS[d["color"]]
    d["has_note"] = bool((d.get("note") or "").strip())
    return d


def annotations_of(conn, cid: int):
    return conn.execute(
        """SELECT * FROM clip_annotations WHERE clip_id=?
           ORDER BY start_offset ASC, id ASC""", (cid,)).fetchall()


def purge_annotations(conn, cid: int) -> None:
    conn.execute("DELETE FROM clip_annotations WHERE clip_id=?", (cid,))


def purge_notes(conn, cid: int) -> None:
    """删条目前把它的随手记和图片一起收走，不留孤儿文件。"""
    for r in notes_of(conn, cid):
        for item in json_value(r["images"], []):
            name = item if isinstance(item, str) else str((item or {}).get("f") or "")
            remove_note_image_if_unused(conn, name, skip_clip_id=cid)
    conn.execute("DELETE FROM clip_notes WHERE clip_id=?", (cid,))


def purge_clip_images(conn, cid: int) -> None:
    """删主收存图的记录；磁盘文件只有在别处也不再引用时才删。"""
    try:
        rows = conn.execute(
            "SELECT file FROM clip_images WHERE clip_id=?", (cid,)).fetchall()
        conn.execute("DELETE FROM clip_images WHERE clip_id=?", (cid,))
    except sqlite3.OperationalError:
        return
    for r in rows:
        remove_note_image_if_unused(conn, str(r["file"] or ""), skip_clip_id=cid)


def valid_annotation_color(value) -> str:
    color = str(value or "amber").strip().lower()
    return color if color in ANNOTATION_COLORS else "amber"


@route_get("/api/annotations/{cid}")
def api_annotations_list(conn, qs, cid):
    if not conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone():
        return {"error": "剪藏不存在"}
    return {"ok": True, "annotations": [
        annotation_row(r) for r in annotations_of(conn, cid)]}


@route_post("/api/annotations/{cid}")
def api_annotation_create(conn, qs, body, cid):
    clip = conn.execute("SELECT content_text FROM clips WHERE id=?", (cid,)).fetchone()
    if not clip:
        return {"error": "剪藏不存在"}
    source = clip["content_text"] or ""
    if not source.strip():
        return {"error": "这条还没有正文，先抓正文再标注"}
    try:
        start = int(body.get("start"))
        end = int(body.get("end"))
    except (TypeError, ValueError):
        return {"error": "标注位置无效"}
    if start < 0 or end <= start or end > max(len(source) + 1024, 120_000):
        return {"error": "标注位置超出正文范围"}
    quote = str(body.get("quote") or "").strip()
    if not quote:
        return {"error": "没有选中文字"}
    if len(quote) > 8_000:
        return {"error": "一次最多批注 8000 字，长段落可以拆成几处"}
    note = str(body.get("note") or "").strip()[:4_000]
    prefix = str(body.get("prefix") or "")[-80:]
    suffix = str(body.get("suffix") or "")[:80]
    color = valid_annotation_color(body.get("color"))
    count = conn.execute(
        "SELECT COUNT(*) n FROM clip_annotations WHERE clip_id=?", (cid,)).fetchone()["n"]
    if count >= 500:
        return {"error": "这篇的标注已经够多（上限 500 条）"}
    conn.execute(
        """INSERT INTO clip_annotations
           (clip_id,start_offset,end_offset,quote,note,color,prefix,suffix)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cid, start, end, quote, note, color, prefix, suffix))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM clip_annotations WHERE id=last_insert_rowid()").fetchone()
    return {"ok": True, "annotation": annotation_row(row)}


@route_post("/api/annotations/{aid}/update")
def api_annotation_update(conn, qs, body, aid):
    row = conn.execute("SELECT * FROM clip_annotations WHERE id=?", (aid,)).fetchone()
    if not row:
        return {"error": "这条标注不存在"}
    color = valid_annotation_color(body.get("color", row["color"]))
    note = str(body.get("note") if "note" in body else row["note"]).strip()[:4_000]
    conn.execute(
        """UPDATE clip_annotations SET note=?,color=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (note, color, aid))
    conn.commit()
    fresh = conn.execute("SELECT * FROM clip_annotations WHERE id=?", (aid,)).fetchone()
    return {"ok": True, "annotation": annotation_row(fresh)}


@route_post("/api/annotations/{aid}/delete")
def api_annotation_delete(conn, qs, body, aid):
    conn.execute("DELETE FROM clip_annotations WHERE id=?", (aid,))
    conn.commit()
    return {"ok": True}


def docx_preview_html(path: Path) -> tuple[str, str]:
    """把 DOCX 转成只读、已转义的结构化 HTML；保留标题、列表、链接和表格。"""
    import html as _html
    import xml.etree.ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

    def safe_href(value: str) -> str:
        href = str(value or "").strip()
        return href if href.startswith(("http://", "https://", "mailto:")) else ""

    def run_html(run) -> str:
        chunks = []
        for node in run.iter():
            if node.tag == W + "t":
                chunks.append(node.text or "")
            elif node.tag == W + "tab":
                chunks.append("\t")
            elif node.tag in {W + "br", W + "cr"}:
                chunks.append("\n")
        raw = "".join(chunks)
        if not raw:
            return ""
        html_text = _html.escape(raw).replace("\t", "&#9;").replace("\n", "<br>")
        props = run.find(W + "rPr")
        if props is not None:
            def enabled(name):
                node = props.find(W + name)
                return node is not None and str(node.get(W + "val", "1")).lower() not in {
                    "0", "false", "off", "none"}
            if enabled("b"):
                html_text = f"<strong>{html_text}</strong>"
            if enabled("i"):
                html_text = f"<em>{html_text}</em>"
            if enabled("u"):
                html_text = f"<u>{html_text}</u>"
        return html_text

    def inline_html(node, links) -> str:
        parts = []
        for child in list(node):
            if child.tag == W + "r":
                parts.append(run_html(child))
            elif child.tag == W + "hyperlink":
                inner = inline_html(child, links)
                href = safe_href(links.get(child.get(R + "id"), ""))
                parts.append(
                    f'<a href="{_html.escape(href)}" target="_blank" rel="noreferrer">{inner}</a>'
                    if inner and href else inner)
            elif child.tag in {
                    W + "ins", W + "smartTag", W + "sdt", W + "sdtContent", W + "customXml"}:
                parts.append(inline_html(child, links))
        return "".join(parts)

    def visible_text(node) -> str:
        return "".join(n.text or "" for n in node.iter(W + "t"))

    def paragraph_html(p, links) -> str:
        inner = inline_html(p, links)
        text = visible_text(p).strip()
        if not inner.strip() or not text:
            return ""
        props = p.find(W + "pPr")
        style = ""
        centered = False
        is_list = False
        heading_level = 0
        large = False
        if props is not None:
            style_node = props.find(W + "pStyle")
            if style_node is not None:
                style = str(style_node.get(W + "val") or "").strip().lower()
            jc = props.find(W + "jc")
            centered = jc is not None and str(jc.get(W + "val") or "").lower() == "center"
            is_list = props.find(W + "numPr") is not None
            outline = props.find(W + "outlineLvl")
            if outline is not None:
                try:
                    heading_level = min(6, max(1, int(outline.get(W + "val")) + 1))
                except (TypeError, ValueError):
                    heading_level = 0
        for size in p.iter(W + "sz"):
            try:
                large = large or int(size.get(W + "val") or 0) >= 28
            except ValueError:
                continue
        if not heading_level:
            m = re.search(r"(?:heading|标题)\s*([1-6])", style, re.I)
            if m:
                heading_level = int(m.group(1))
        if style in {"title", "标题"} or (centered and large):
            heading_level = 1
        if heading_level:
            return f"<h{heading_level}>{inner}</h{heading_level}>"
        if is_list:
            return f'<div class="docx-list"><span aria-hidden="true">•</span><div>{inner}</div></div>'
        return f"<p>{inner}</p>"

    def table_html(table, links) -> str:
        rows = table.findall(W + "tr")
        if not rows:
            return ""
        rendered = []
        for index, row in enumerate(rows):
            cells = []
            for cell in row.findall(W + "tc"):
                cell_parts = []
                for p in cell.findall(W + "p"):
                    part = inline_html(p, links)
                    if part:
                        cell_parts.append(part)
                cells.append("<br>".join(cell_parts))
            tag = "th" if index == 0 else "td"
            rendered.append("<tr>" + "".join(
                f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>")
        if not rendered:
            return ""
        head, body = rendered[0], "".join(rendered[1:])
        return (f'<div class="md-table-wrap docx-table"><table><thead>{head}</thead>'
                f"<tbody>{body}</tbody></table></div>")

    def blocks_html(parent, links) -> str:
        out = []
        for child in list(parent):
            if child.tag == W + "p":
                out.append(paragraph_html(child, links))
            elif child.tag == W + "tbl":
                out.append(table_html(child, links))
            elif child.tag in {W + "sdt", W + "sdtContent", W + "customXml"}:
                out.append(blocks_html(child, links))
        return "".join(out)

    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 20 * 1024 * 1024:
                return "", "Word 文档正文过大，未生成排版预览"
            root = ET.fromstring(archive.read(info))
            links = {}
            if "word/_rels/document.xml.rels" in archive.namelist():
                rel_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
                for rel in rel_root:
                    href = safe_href(rel.get("Target", ""))
                    if rel.get("Id") and href:
                        links[rel.get("Id")] = href
        body = root.find(".//" + W + "body")
        if body is None:
            return "", "Word 文档结构无法识别"
        html_out = blocks_html(body, links)
        if not html_out:
            return "", "Word 文档里没有可读取的文字"
        return html_out[:2_000_000], "stored"
    except (zipfile.BadZipFile, KeyError, OSError, ET.ParseError):
        return "", "Word 文档结构无法解析，原件已保存"


def note_file_display_name(conn, stored_name: str) -> str:
    for row in conn.execute("SELECT images FROM clip_notes"):
        for item in json_value(row["images"], []):
            entry = _note_entry(item)
            if entry and entry["file"] == stored_name:
                return entry["name"]
    return stored_name


@route_get("/api/notes/preview")
def api_note_preview(conn, qs):
    """把随手记附件变成可直接阅读的文本；原件不动，也不写回数据库。"""
    name = (qs.get("file") or [""])[0]
    if not name or Path(name).name != name:
        raise ValidationError("附件名无效")
    target = (NOTES_DIR / name).resolve()
    if not target.is_relative_to(NOTES_DIR.resolve()) or not target.is_file():
        return {"error": "附件不存在"}
    suffix = Path(name).suffix.lower()
    size = target.stat().st_size
    preview_html = ""
    if size > MAX_NOTE_FILE:
        return {"error": "附件超过预览上限"}
    if suffix in NOTE_TEXT_EXT:
        with target.open("rb") as fh:
            raw = fh.read(NOTE_PREVIEW_MAX_BYTES + 1)
        truncated = len(raw) > NOTE_PREVIEW_MAX_BYTES
        text = decode_text(raw[:NOTE_PREVIEW_MAX_BYTES])
        status = "stored"
        kind = "markdown" if suffix in {".md", ".markdown"} else "text"
    elif suffix == ".docx":
        preview_html, status = docx_preview_html(target)
        truncated = len(preview_html) >= 2_000_000
        text = ""
        kind = "document"
        if not preview_html:
            text, status = extract_file_text(target, suffix)
            truncated = len(text) >= 60_000
            kind = "extracted"
    elif suffix in {".html", ".htm", ".pptx", ".xlsx", ".epub", ".pdf"}:
        text, status = extract_file_text(target, suffix)
        truncated = len(text) >= 60_000
        kind = "extracted"
    else:
        return {"ok": False, "name": name,
                "original_name": note_file_display_name(conn, name), "size": size,
                "suffix": suffix, "kind": "unsupported", "text": "",
                "status": f"暂不支持在应用内预览 {suffix or '该格式'}，请下载后用本机程序打开。",
                "truncated": False}
    if len(text) > NOTE_PREVIEW_MAX_CHARS:
        text = text[:NOTE_PREVIEW_MAX_CHARS]
        truncated = True
    if status == "partial":
        # 预览面板直接把 status 当人话显示，别漏一个英文内部状态出去
        status = "正文较长，这里只显示前 200 页 / 6 万字"
        truncated = True
    return {"ok": True, "name": name, "original_name": note_file_display_name(conn, name),
            "size": size, "suffix": suffix, "kind": kind, "text": text,
            "html": preview_html, "status": status, "truncated": truncated}


@route_get("/api/notes/{cid}")
def api_notes_list(conn, qs, cid):
    if not conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone():
        return {"error": "剪藏不存在"}
    return {"ok": True, "notes": [note_row(r) for r in notes_of(conn, cid)]}


@route_post("/api/notes/{cid}")
def api_note_create(conn, qs, body, cid):
    if not conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone():
        return {"error": "剪藏不存在"}
    text = str(body.get("text") or "").strip()[:8000]
    submitted = [*(body.get("media") or []), *(body.get("images") or []),
                 *(body.get("audios") or [])]
    files = clean_note_files(submitted)
    if not text and not files:
        return {"error": "写点什么，或者贴张图、挂个附件"}
    conn.execute(
        "INSERT INTO clip_notes (clip_id,text,images) VALUES (?,?,?)",
        (cid, text, json.dumps(files, ensure_ascii=False)))
    conn.commit()
    row = conn.execute("SELECT * FROM clip_notes WHERE id=last_insert_rowid()").fetchone()
    return {"ok": True, "note": note_row(row)}


@route_post("/api/notes/{nid}/update")
def api_note_update(conn, qs, body, nid):
    row = conn.execute("SELECT * FROM clip_notes WHERE id=?", (nid,)).fetchone()
    if not row:
        return {"error": "这条随手记不存在"}
    text = str(body.get("text") or "").strip()[:8000]
    submitted = [*(body.get("media") or []), *(body.get("images") or []),
                 *(body.get("audios") or [])]
    files = clean_note_files(submitted)
    if not text and not files:
        return {"error": "写点什么，或者贴张图、挂个附件"}
    keep = {f["f"] for f in files}
    for item in json_value(row["images"], []):
        name = item if isinstance(item, str) else str((item or {}).get("f") or "")
        if name and name not in keep:
            remove_note_image_if_unused(conn, name, skip_note_id=nid)
    conn.execute(
        """UPDATE clip_notes SET text=?, images=?, updated_at=datetime('now','localtime')
           WHERE id=?""", (text, json.dumps(files, ensure_ascii=False), nid))
    conn.commit()
    return {"ok": True, "note": note_row(
        conn.execute("SELECT * FROM clip_notes WHERE id=?", (nid,)).fetchone())}


@route_post("/api/notes/{nid}/delete")
def api_note_delete(conn, qs, body, nid):
    row = conn.execute("SELECT * FROM clip_notes WHERE id=?", (nid,)).fetchone()
    if not row:
        return {"ok": True}
    for item in json_value(row["images"], []):
        name = item if isinstance(item, str) else str((item or {}).get("f") or "")
        remove_note_image_if_unused(conn, name, skip_note_id=nid)
    conn.execute("DELETE FROM clip_notes WHERE id=?", (nid,))
    conn.commit()
    return {"ok": True}


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _extract_file_text_raw(path: Path, suffix: str) -> tuple[str, str]:
    """返回 (正文, 状态)。读取只发生在内存里，不把抽出的正文写回数据库。

    PDF 由随应用携带的 pypdf 读取，用户不需要另装依赖。读取层故意保持
    保守：能读到文字就用，扫描件或损坏文件不伪造“解析成功”。
    """
    text_exts = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".url", ".xml"}
    if suffix in text_exts:
        return decode_text(path.read_bytes())[:60_000], "stored"
    if suffix in {".html", ".htm"}:
        return extract_main_text(decode_text(path.read_bytes())), "stored"
    if suffix == ".docx":
        import html as _html
        try:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 20 * 1024 * 1024:
                    return "", "文档正文过大，未解析"
                xml = archive.read(info).decode("utf-8", "replace")
            xml = re.sub(r"</w:p\s*>", "\n", xml)
            text = _html.unescape(re.sub(r"<[^>]+>", "", xml))
            return re.sub(r"\n{3,}", "\n\n", text).strip()[:60_000], "stored"
        except (zipfile.BadZipFile, KeyError, OSError):
            return "", "DOCX 结构无法解析，原件已保存"
    if suffix == ".pptx":
        import html as _html
        try:
            with zipfile.ZipFile(path) as archive:
                slides = sorted(
                    (name for name in archive.namelist()
                     if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                    key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))
                parts = []
                for index, name in enumerate(slides[:200], 1):
                    info = archive.getinfo(name)
                    if info.file_size > 20 * 1024 * 1024:
                        continue
                    xml = archive.read(info).decode("utf-8", "replace")
                    lines = [_html.unescape(re.sub(r"<[^>]+>", "", item)).strip()
                             for item in re.findall(r"<a:t\b[^>]*>(.*?)</a:t>", xml, re.S)]
                    lines = [line for line in lines if line]
                    if lines:
                        parts.append(f"第 {index} 页\n" + "\n".join(lines))
                    if sum(map(len, parts)) >= 60_000:
                        break
            text = "\n\n".join(parts).strip()[:60_000]
            return (text, "stored") if text else ("", "PPTX 里没有可读取的文字")
        except (zipfile.BadZipFile, KeyError, OSError):
            return "", "PPTX 结构无法解析，原件已保存"
    if suffix == ".xlsx":
        import html as _html
        try:
            with zipfile.ZipFile(path) as archive:
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    info = archive.getinfo("xl/sharedStrings.xml")
                    if info.file_size <= 20 * 1024 * 1024:
                        xml = archive.read(info).decode("utf-8", "replace")
                        for item in re.findall(r"<si\b[^>]*>(.*?)</si>", xml, re.S):
                            shared.append(_html.unescape(
                                re.sub(r"<[^>]+>", "", " ".join(
                                    re.findall(r"<t\b[^>]*>(.*?)</t>", item, re.S)))).strip())
                sheet_names = sorted(
                    (name for name in archive.namelist()
                     if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
                    key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))
                rows_out = []
                for index, name in enumerate(sheet_names[:20], 1):
                    info = archive.getinfo(name)
                    if info.file_size > 20 * 1024 * 1024:
                        continue
                    xml = archive.read(info).decode("utf-8", "replace")
                    lines = []
                    for row in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, re.S):
                        cells = []
                        for cell in re.findall(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                            attrs, body = cell
                            value = ""
                            if 't="s"' in attrs:
                                ref = re.search(r"<v>(\d+)</v>", body)
                                if ref and int(ref.group(1)) < len(shared):
                                    value = shared[int(ref.group(1))]
                            else:
                                node = re.search(r"<(?:v|t)\b[^>]*>(.*?)</(?:v|t)>", body, re.S)
                                if node:
                                    value = _html.unescape(re.sub(r"<[^>]+>", "", node.group(1))).strip()
                            if value:
                                cells.append(value)
                        if cells:
                            lines.append(" | ".join(cells))
                    if lines:
                        rows_out.append(f"工作表 {index}\n" + "\n".join(lines))
                    if sum(map(len, rows_out)) >= 60_000:
                        break
            text = "\n\n".join(rows_out).strip()[:60_000]
            return (text, "stored") if text else ("", "XLSX 里没有可读取的文字")
        except (zipfile.BadZipFile, KeyError, OSError):
            return "", "XLSX 结构无法解析，原件已保存"
    if suffix == ".epub":
        import html as _html
        try:
            with zipfile.ZipFile(path) as archive:
                names = sorted(
                    name for name in archive.namelist()
                    if name.lower().endswith((".xhtml", ".html", ".htm")))
                parts = []
                for name in names[:200]:
                    info = archive.getinfo(name)
                    if info.file_size > 20 * 1024 * 1024:
                        continue
                    raw = archive.read(info)
                    body = extract_main_text(decode_text(raw))
                    if body:
                        parts.append(body)
                    if sum(map(len, parts)) >= 60_000:
                        break
            text = "\n\n".join(parts).strip()[:60_000]
            return (text, "stored") if text else ("", "EPUB 里没有可读取的文字")
        except (zipfile.BadZipFile, KeyError, OSError):
            return "", "EPUB 结构无法解析，原件已保存"
    if suffix == ".pdf":
        # 版面还原（分栏 / 页眉页脚 / 双语对照 / 段首缩进）交给 pdf_text，
        # 这里只负责把「为什么没读到」翻译成一句人话。
        try:
            text, status = pdf_text.extract_pdf_text(path)
            return text, status
        except Exception as e:  # 解析器异常要转成可见状态
            return "", "PDF 解析失败，原件已保存（内部错误 ref=" + internal_error_ref("pdf_extract", e) + "）"
    return "", f"暂不支持解析 {suffix or '该格式'}，原件已保存"


def extract_file_text(path: Path, suffix: str) -> tuple[str, str]:
    """统一出口：空正文绝不许带上 "stored" 状态。

    空的 .txt / .md / .html / .docx 以前返回 ("", "stored")，这个 "stored"
    会被写进 fetch_error，界面于是显示「暂未提取到文字（stored）」——
    一个英文内部状态被当成错误原因端给用户看。
    """
    text, status = _extract_file_text_raw(path, suffix)
    # 只有空格/换行的文件也算没读到：`.txt` 分支直接返回原字符串，
    # 下游 normalize_body_text 才判空，中间这段已经把 fetch_error 写坏了。
    if not (text or "").strip() and status in {"stored", "partial"}:
        return "", "原件里没有可读取的文字"
    return text, status


def _usable_body(raw: str, status: str) -> tuple[str, str]:
    """抽取结果 → (可入库正文, 面向用户的状态)。

    判空必须发生在 normalize_body_text **之后**：纯页码行的 .txt 在 raw
    层非空、整理后是空串；纯控制字符则相反，整理后仍是一串垃圾。
    内部状态（stored / partial）绝不许落进 fetch_error。
    """
    text = normalize_body_text(raw) if raw else ""
    if text and pdf_text.looks_garbled(text):
        return "", "原件里的文字编码无法识别"
    if not text:
        if status in {"stored", "partial"}:
            return "", "原件里没有可读取的文字"
        return "", status or "原件里没有可读取的文字"
    return text, status


# ── 正文版式整理：文件与网页抽出来的文字统一走这里 ─────────────
def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF or 0x3040 <= code <= 0x30FF)


def _smart_join(parts) -> str:
    """中文之间直接相连；只在 ASCII 字母/数字边界补空格，不让半角空格撑散版面。

    另外两条边界也要补空格：ASCII 句号/逗号接汉字（"1776." + "中大陆会议"），
    汉字接英文单词。缺了这两条，双语对照的原文与译文会粘成一个词。
    """
    out = ""
    for part in parts:
        part = str(part).strip()
        if not part:
            continue
        if not out:
            out = part
            continue
        prev, nxt = out[-1], part[0]
        need_space = ((prev.isascii() and prev.isalnum())
                      or (nxt.isascii() and nxt.isalnum())
                      or (prev.isascii() and _is_cjk_char(nxt))
                      or (_is_cjk_char(prev) and nxt.isascii() and nxt.isalnum()))
        out += (" " if need_space else "") + part
    return out


def _strip_inline_md(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.strip()


# CJK 部首补充区（U+2E80–2EFF）没有 Unicode 兼容分解，PDF 字体却常用它们
# 冒充简化字：⻔→门、⻅→见。按 Unicode 名称逐字给出常用映射。
_CJK_RADICAL_FIX = {
    "⻔": "门", "⻅": "见", "⻓": "长", "⻉": "贝", "⻋": "车",
    "⻌": "辶", "⻍": "辶", "⻎": "辶", "⻏": "阝", "⻐": "钅",
    "⻈": "讠", "⻊": "足", "⺟": "母", "⺠": "民",
}

# PDF 字体 CMap 把汉字映射到非 CJK 区（APL 符号、括注字母、梵文字母等）。
# 实测人口普查公报 PDF 的 CMap 错误映射，按需扩充。
_PDF_CMAP_FIX = {
    "⍤": "中",      # U+2364 → 中（从⍤ → 中国/中华）
    "ⓨ䬝": "澳门",   # U+24E8 + U+4B1D → 澳门
    "थ⎳": "台湾",   # U+0925 + U+23B3 → 台湾
    "喕ϯ\x01person喖": "",  # 损坏的乱码段，整段丢弃
}


def normalize_body_text(text: str) -> str:
    """把抽出的正文整理成观澜阅读器可直接显示的纯文本。

    readerHtml() 用 esc() 做纯文本渲染、批注按字符坐标定位，所以正文中不能
    留 Markdown 标记；PDF 抽取常见的逐行硬换行也要并回段落。统一输出
    「一个段落 = 一个长行、段落之间空行」的形状，短行独立成段，避免被
    两端对齐拉开字距。只对成体系的 Markdown 做块级转换，普通正文不误伤。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""
    # PDF 的 CMap 常把汉字映射成康熙部首/兼容表意文字（⽼→老、⼀→一、⻔→门），
    # 全角字母数字也混在其中。只归一化这些字符，中文标点保持全角不动，
    # 否则「，：；？」会被降级成半角，还会破坏幂等。
    try:
        import unicodedata

        def _compat_char(ch: str) -> str:
            code = ord(ch)
            if ch in _CJK_RADICAL_FIX:
                return _CJK_RADICAL_FIX[ch]
            if (0x2E80 <= code <= 0x2FDF) or (0xF900 <= code <= 0xFAFF):
                fixed = unicodedata.normalize("NFKC", ch)
                return fixed if len(fixed) == 1 else ch
            if (0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A
                    or 0xFF41 <= code <= 0xFF5A):
                return chr(code - 0xFEE0)
            return ch

        # 先做多字符映射（如「ⓨ䬝」→「澳门」），再做单字符
        for wrong, right in _PDF_CMAP_FIX.items():
            raw = raw.replace(wrong, right)
        raw = "".join(_compat_char(ch) for ch in raw)
    except Exception:
        pass
    raw = re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]", " ", raw)
    lines = []
    for line in raw.split("\n"):
        s = line.strip()
        # 行首的双语标签（EN / 中 / 译文…）后面那个空格是排版用的，不能当
        # 「逐字拆开」的空隙删掉，否则译文会变成「中大陆会议」。先把标签
        # 摘出来，只对后面的正文做去空格。
        tag = ""
        mark = re.match(r"^(EN|ZH|CN|JP|中|英|日|中译|英译|原文|译文)[ \t]+(?=\S)", s)
        if mark:
            tag, s = mark.group(0), s[mark.end():]
        # pypdf 常把中文逐字拆开（"视 频 整 理"）；中文与标点之间的空格也多余
        for _ in range(2):
            s = re.sub(r"(?<=[\u4e00-\u9fff\u3400-\u4dbf])[ \t]+"
                       r"(?=[\u4e00-\u9fff\u3400-\u4dbf，。、；：？！）」》】])", "", s)
        s = re.sub(r"(?<=[（「《【])[ \t]+(?=[\u4e00-\u9fff\u3400-\u4dbf])", "", s)
        s = tag + s
        # 只删明确的页码行；纯数字行可能是数据，必须保留
        if (re.fullmatch(r"[—–\-]\s*\d{1,4}\s*[—–\-]", s)
                or re.fullmatch(r"第\s*\d{1,4}\s*页", s)
                or re.fullmatch(r"Page\s+\d+\s*(of|/)\s*\d+", s, re.I)):
            continue
        lines.append(s)

    head = sum(1 for l in lines if re.match(r"^\s{0,3}#{1,6}\s+\S", l))
    quote = sum(1 for l in lines if re.match(r"^\s{0,3}>\s", l))
    table = sum(1 for l in lines if re.match(r"^\s*\|.*\|\s*$", l))
    fence = sum(1 for l in lines if re.match(r"^\s*(```|~~~)", l))
    lists = sum(1 for l in lines if re.match(r"^\s{0,3}([-*+]|\d+[.)])\s+\S", l))
    is_md = (head >= 2 or table >= 2 or bool(fence)
             or (head >= 1 and (quote or table or lists)))

    blocks: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            joined = _smart_join(pending)
            if joined:
                blocks.append(joined)
            pending.clear()

    def flush_if_sentence() -> None:
        # 按句末标点断段；兜底阈值放宽到 1200 字，避免把长段在半句中间腰斩
        size = sum(len(x) for x in pending)
        last = pending[-1] if pending else ""
        if (size >= 80 and re.search(r"[。！？.!?；;：:]$", last)) or size >= 1200:
            flush()

    i, in_code = 0, False
    while i < len(lines):
        line = lines[i]
        if not line:
            flush()
            i += 1
            continue
        if is_md and re.match(r"^\s*(```|~~~)", line):
            flush()
            in_code = not in_code
            i += 1
            continue
        if is_md and in_code:
            flush()
            blocks.append(line)
            i += 1
            continue
        if is_md:
            m = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
            if m:
                flush()
                blocks.append(_strip_inline_md(m.group(1)))
                i += 1
                continue
            if re.match(r"^\s{0,3}>\s?", line):
                quoted = []
                while i < len(lines) and re.match(r"^\s{0,3}>\s?", lines[i]):
                    quoted.append(_strip_inline_md(re.sub(r"^\s{0,3}>\s?", "", lines[i])))
                    i += 1
                flush()
                joined = "；".join(x for x in quoted if x)
                if joined:
                    blocks.append(joined)
                continue
            if re.match(r"^\s*\|.*\|\s*$", line):
                rows = []
                while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                    # 空单元格必须留位，否则 zip(header, cells) 整行串列
                    # （A 列的值会被记到 B 列上）。只跳过 Markdown 分隔行。
                    cells = [_strip_inline_md(c).strip()
                             for c in lines[i].strip().strip("|").split("|")]
                    i += 1                         # 分隔行也要往前走，别死循环
                    if all(re.fullmatch(r"[\s:\-]*", c) for c in cells):
                        continue
                    if any(cells):
                        rows.append(cells)
                flush()
                if rows:
                    header = ["序号" if c.strip() in {"#", "No", "no"} else c.strip()
                              for c in rows[0]]
                    blocks.append("表格字段：" + " / ".join(header))
                    for n, cells in enumerate(rows[1:], 1):
                        label = ""
                        if (header and header[0] == "序号"
                                and re.fullmatch(r"\d+", cells[0].strip())):
                            label = cells[0].strip() + "）"
                            pairs = list(zip(header[1:], cells[1:]))
                        elif header and len(header) == len(cells):
                            label = str(n) + "）"
                            pairs = list(zip(header, cells))
                        else:
                            label = str(n) + "）"
                            pairs = [(f"第{k}列", c) for k, c in enumerate(cells, 1)]
                        blocks.append(label + "；".join(f"{h}：{c}" for h, c in pairs))
                continue
            if re.match(r"^\s{0,3}([-*+]|\d+[.)])\s+\S", line):
                items = []
                while i < len(lines) and re.match(
                        r"^\s{0,3}([-*+]|\d+[.)])\s+\S", lines[i]):
                    items.append(_strip_inline_md(
                        re.sub(r"^\s{0,3}([-*+]|\d+[.)])\s+", "", lines[i])))
                    i += 1
                flush()
                joined = "；".join(x for x in items if x)
                if joined:
                    blocks.append(joined)
                continue
            if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
                flush()
                i += 1
                continue
        # 短标题/元信息行后接长正文行：先断开，避免封面标题与正文粘成一片
        if (not is_md and pending and len(pending[-1]) <= 24
                and len(line) >= 32):
            flush()
        pending.append(_strip_inline_md(line) if is_md else line)
        flush_if_sentence()
        i += 1
    flush()
    return "\n\n".join(blocks)[:60_000]


def nice_title(raw: str) -> str:
    """文件名当标题时顺手收拾一下：去掉日期前缀和扩展名，下划线变空格。"""
    stem = Path(str(raw or "")).stem or str(raw or "")
    stem = re.sub(r"^\d{4}[-_.]?\d{2}[-_.]?\d{2}[-_ .]*", "", stem)
    stem = re.sub(r"[_]{1,}", " ", stem).strip(" -_.")
    return stem or Path(str(raw or "")).stem or "未命名"


def register_file(conn, *, name, mime, stored_name, path, size, digest,
                  tags, board_id, tier, title="", excerpt="", grade="") -> dict:
    """文件已经落到 data/uploads 之后的事：去重、抽正文、入库。"""
    duplicate = find_duplicate_clip(conn, "", digest)
    if duplicate:
        old = UPLOAD_DIR / (duplicate["file_path"] or "")
        if duplicate["file_path"] and old.is_file():
            remove_upload(stored_name)      # 旧原件还在，放心删掉新副本
        else:
            # 旧原件已经不在本机（历史误删 / 换了机器）：把这次上传的
            # 副本补位并修正条目，否则两份原件都没了、条目却还在。
            conn.execute(
                "UPDATE clips SET file_path=?, file_size=?, mime_type=?, "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (stored_name, size, _server_mime(name, mime), duplicate["id"]))
            conn.commit()
            duplicate = conn.execute(
                "SELECT * FROM clips WHERE id=?", (duplicate["id"],)).fetchone()
        return {"ok": True, "duplicate": True, "import_status": "duplicate",
                "clip": clip_row(duplicate)}
    # 文件入库即抽正文：整理好版式再写回，阅读器可以直接展示，不再只留原件。
    # 扫描件 / 加密件 / 解析失败时保留原件，并把原因写进 fetch_error 供界面提示。
    suffix = Path(name).suffix.lower()[:12]
    raw_text, parse_status = "", ""
    try:
        if size <= EXTRACT_LIMIT:
            raw_text, parse_status = extract_file_text(path, suffix)
        elif suffix == ".pdf":
            # PDF 超过 40MB 不再拒绝：启动后台线程抽取，完成后 sync_notify
            parse_status = "PDF 原件较大，正在后台提取正文…"
        else:
            parse_status = f"原件超过 {EXTRACT_LIMIT // 1024 // 1024} MB，未自动提取正文"
    except Exception as e:  # 抽取失败不能拖垮入库
        parse_status = "正文提取失败，原件已保存（" + type(e).__name__ + "）"
    text, parse_status = _usable_body(raw_text, parse_status)
    # 没抽出正文就不是「已入库」。以前无论成败都写 stored，详情页因此
    # 显示「正文已入库，随时可问」，实际 content_text 是空的，列表也不会
    # 打上「没抓到」标记。截断则如实写进 fetch_error，刷新后仍看得见。
    status = "stored" if text else ("extracting" if suffix == ".pdf" and size > EXTRACT_LIMIT else "failed")
    notice = ("正文较长，已按前 200 页 / 6 万字截断，后半部分未入库"
              if text and parse_status == "partial" else "")
    try:
        conn.execute("BEGIN IMMEDIATE")
        clean = clean_tier(tier)
        graded = clean_grade(grade) or guess_grade("", "file", name, clean)
        conn.execute(
            """INSERT INTO clips (title,url,channel,tags,excerpt,content_text,fetch_status,fetch_error,
               content_hash,source_type,file_name,file_path,mime_type,file_size,board_id,tier,grade,
               updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (str(title or "").strip() or nice_title(name), "", "文件",
             json.dumps(clean_strings(tags), ensure_ascii=False),
             str(excerpt or "").strip(), text, status,
             notice if text else parse_status, digest, "file", name, stored_name,
             _server_mime(name, mime),
             size, resolve_board(conn, board_id), clean, graded))
        row = conn.execute("SELECT * FROM clips WHERE id=last_insert_rowid()").fetchone()
        conn.commit()                       # 提交必须是 try 里最后一句：
        # 之后任何异常（响应构造等）都不该再删掉已入库条目的原件。
    except Exception:
        conn.rollback()
        remove_upload(stored_name)
        raise
    result = {"ok": True, "duplicate": False, "clip": clip_row(row),
            "import_status": "saved" if text else "saved_unparsed",
            "parse_message": notice if text else parse_status}
    # PDF 超过 40MB：启动后台线程抽取，完成后 sync_notify 通知前端刷新
    if suffix == ".pdf" and size > EXTRACT_LIMIT and not text:
        import threading
        threading.Thread(target=_bg_extract_pdf, args=(row["id"], path), daemon=True).start()
    return result


def _bg_extract_pdf(cid: int, path: Path):
    """后台线程：大 PDF 抽正文，完成后写库并 sync_notify 通知前端。"""
    conn = None
    try:
        # db() 也可能抛（库被独占、磁盘满），放进 try 才能把状态收口成 failed，
        # 否则线程静默死掉，条目永远停在 extracting（init_db 下次启动才兜底）。
        conn = db()
        raw, status = extract_file_text(path, ".pdf")
        text, parse_status = _usable_body(raw, status)
        # 只认还在 extracting 的条目：抽取期间用户可能已手动重提、改状态或删条目，
        # 后台结果不该把人家后来的操作盖掉。
        if text:
            conn.execute(
                """UPDATE clips SET content_text=?, fetch_status='stored', fetch_error=?,
                   updated_at=datetime('now','localtime')
                   WHERE id=? AND fetch_status='extracting'""",
                (text, "" if parse_status != "partial" else "正文较长，已按前 200 页 / 6 万字截断", cid))
        else:
            conn.execute(
                """UPDATE clips SET fetch_status='failed', fetch_error=?,
                   updated_at=datetime('now','localtime')
                   WHERE id=? AND fetch_status='extracting'""",
                (parse_status or "PDF 里没有可读取的文字层，可能是扫描件或图片版", cid))
        conn.commit()
        sync_notify()  # 叫醒 SSE，前端自动刷新显示正文
    except Exception as e:
        try:
            if conn is not None:
                conn.execute(
                    """UPDATE clips SET fetch_status='failed', fetch_error=?,
                       updated_at=datetime('now','localtime')
                       WHERE id=? AND fetch_status='extracting'""",
                    (f"后台提取失败（{type(e).__name__}）", cid))
                conn.commit()
            sync_notify()
        except Exception:
            pass
    finally:
        if conn is not None:
            conn.close()


def safe_file_name(raw) -> str:
    name = str(raw or "未命名文件").replace("\\", "/").split("/")[-1]
    # Windows 非法字符与尾部空格/点：原名落盘会直接抛 OSError，而上传
    # 接口没包 try，结果是 500 + 半截临时文件。扩展名也在这里收口。
    name = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "", name)
    name = name.strip().rstrip(".").strip()[:180] or "未命名文件"
    suffix = Path(name).suffix
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        name = name[:-len(suffix)] + ".bin"
    return name


_SAFE_DOWNLOAD_MIMES = {
    "application/pdf", "application/octet-stream", "application/epub+zip",
    "application/zip", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/svg+xml", "text/plain", "text/markdown", "text/csv",
    "audio/mpeg", "audio/mp4", "audio/wav", "video/mp4",
}


def _server_mime(name: str, claimed: str = "") -> str:
    """入库 MIME 一律由服务端按扩展名决定。

    客户端传的 `mime_type` 不可信：把 .txt 标成 text/html，下载接口的内联
    白名单是按后缀判的，浏览器就会用 HTML 解释同一个源下的文件 —— 存储型
    XSS；带 CRLF 的值还能注入响应头。
    """
    guessed = mimetypes.guess_type(name or "")[0] or "application/octet-stream"
    cleaned = re.sub(r"[\r\n\x00-\x1f\x7f]", "", str(claimed or ""))[:120]
    return cleaned if cleaned in _SAFE_DOWNLOAD_MIMES else guessed


@route_post("/api/clips/file")
def api_clip_file(conn, qs, body):
    """老的 JSON+base64 通道。新前端走 /api/clips/upload，这条留着兼容。"""
    name = safe_file_name(body.get("name"))
    encoded = str(body.get("content_base64") or "")
    if "," in encoded and encoded.lstrip().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return {"error": "文件内容不是有效的 base64"}
    if not raw:
        return {"error": "文件为空"}
    if len(raw) > MAX_FILE_SIZE:
        return {"error": "单文件最大 6MB（旧 JSON 通道限制，大文件请走 /api/clips/upload）"}
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    suffix = Path(name).suffix.lower()[:12]
    stored_name = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + os.urandom(8).hex() + suffix
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / stored_name
    path.write_bytes(raw)
    return register_file(conn, name=name, mime=str(body.get("mime_type") or ""),
                         stored_name=stored_name, path=path, size=len(raw), digest=digest,
                         tags=body.get("tags"), board_id=body.get("board_id"),
                         tier=body.get("tier"), title=body.get("title"),
                         excerpt=body.get("excerpt"), grade=body.get("grade"))


@route_post("/api/clips/batch")
def api_clip_batch(conn, qs, body):
    try:
        ids = list(dict.fromkeys(int(v) for v in (body.get("ids") or [])))[:100]
    except (TypeError, ValueError):
        return {"error": "ids 必须是剪藏 ID 数组"}
    if not ids:
        return {"error": "请先选择剪藏"}
    action = body.get("action")
    marks = ",".join("?" for _ in ids)
    paths = []
    touched = None            # 真实被动过的条数；total_changes 会被 FTS 触发器灌水
    conn.execute("BEGIN IMMEDIATE")
    if action in {"add_tags", "set_tags"}:
        new_tags = clean_strings(body.get("tags"))
        if not new_tags:
            conn.rollback()
            return {"error": "批量标签不能为空"}
        for row in conn.execute(f"SELECT id,tags FROM clips WHERE id IN ({marks})", ids).fetchall():
            tags = new_tags if action == "set_tags" else clean_strings([
                *clean_strings(json_value(row["tags"], [])), *new_tags])
            conn.execute("UPDATE clips SET tags=?,updated_at=datetime('now','localtime') WHERE id=?",
                         (json.dumps(tags, ensure_ascii=False), row["id"]))
    elif action == "move":
        # 批量挪抽屉：board 为空 / null 表示退回未分类（也就是移出资料库）
        try:
            board_id = resolve_board(conn, body.get("board"))
        except ValidationError as e:
            conn.rollback()
            return {"error": str(e)}
        cur = conn.execute(
            f"""UPDATE clips SET board_id=?, updated_at=datetime('now','localtime')
                WHERE id IN ({marks})""", (board_id, *ids))
        touched = cur.rowcount
    elif action == "delete":
        paths = [r["file_path"] for r in conn.execute(
            f"SELECT file_path FROM clips WHERE id IN ({marks})", ids).fetchall() if r["file_path"]]
        for cid in ids:
            purge_annotations(conn, cid)
            purge_notes(conn, cid)
            purge_clip_images(conn, cid)
            purge_body_images(cid)      # 单条删除有，批量删除漏了 → 孤儿配图
            try:
                import reading as _reading_mod
                _reading_mod.purge_clip(conn, cid)
            except Exception as exc:
                print(f"[reading] 批量清理失败 cid={cid}: {type(exc).__name__}", flush=True)
            try:
                import event_topics as _event_topics_mod
                _event_topics_mod.purge_clip(conn, cid)
            except Exception as exc:
                print(f"[event_topics] 批量清理失败 cid={cid}: {type(exc).__name__}", flush=True)
        conn.execute(f"DELETE FROM clips WHERE id IN ({marks})", ids)
    else:
        conn.rollback()
        return {"error": "未知批量操作"}
    changed = touched if touched is not None else conn.total_changes
    conn.commit()
    for stored in paths:
        remove_upload(stored)
    return {"ok": True, "changed": changed, "action": action}


@route_get("/api/clips/export")
def api_clip_export(conn, qs):
    rows = conn.execute("SELECT * FROM clips ORDER BY id").fetchall()
    outputs = conn.execute("SELECT * FROM ai_outputs ORDER BY id").fetchall()
    annotations = conn.execute("SELECT * FROM clip_annotations ORDER BY clip_id,id").fetchall()
    try:
        clip_images = [dict(r) for r in conn.execute(
            "SELECT * FROM clip_images ORDER BY clip_id,id")]
    except sqlite3.OperationalError:
        clip_images = []
    event_data = {}
    try:
        import event_topics as _event_topics_mod
        event_data = _event_topics_mod.api_export(conn, qs)
    except Exception as exc:
        print(f"[event_topics] 导出关联数据失败：{type(exc).__name__}", flush=True)
    return {"version": 5, "exported_at": datetime.now().isoformat(timespec="seconds"),
            "clips": [clip_row(r, full=True) for r in rows],
            "ai_outputs": [ai_output_row(r) for r in outputs],
            "annotations": [annotation_row(r) for r in annotations],
            "clip_images": clip_images, "event_topics": event_data,
            "note": "JSON 不含上传文件原件；完整备份请停止服务后复制 data 目录。"}


def ai_output_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["content"] = json_value(d.get("content"), {})
    d["source_ids"] = json_value(d.get("source_ids"), [])
    return d


def save_ai_output(conn, kind: str, title: str, content: dict,
                   clip_id=None, source_ids=None) -> int:
    cur = conn.execute(
        """INSERT INTO ai_outputs (clip_id,kind,title,content,source_ids,model)
           VALUES (?,?,?,?,?,?)""",
        (clip_id, kind, title[:200], json.dumps(content, ensure_ascii=False),
         json.dumps(source_ids or [], ensure_ascii=False), ai_config()["model"] or "unknown"))
    return int(cur.lastrowid)


def save_insight_record(kind: str, title: str, content: dict,
                        source_ids=None, attempts: int = 4):
    """写洞察记录时单独开一条新连接，并允许重试。

    为什么不用外面那条：这个请求在模型上要待十几到几十秒，连接就那么一直挂着。
    期间只要有人（杀软、备份、索引）短暂占住文件，写就会报
    「attempt to write a readonly database」——而答案其实已经拿到了，
    不该为了记一笔历史把它丢掉。
    """
    last = None
    for attempt in range(attempts):
        conn = db()
        try:
            out = save_ai_output(conn, kind, title, content, None, source_ids)
            conn.commit()
            return out
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "readonly" not in msg and "locked" not in msg and "busy" not in msg:
                raise
            last = e
            time.sleep(0.25 * (attempt + 1))
        finally:
            conn.close()
    raise last


def file_text_for_ai(row: sqlite3.Row) -> tuple[str, str]:
    """文件件的原件文字，只在入库正文缺失时临时读一次，不写库。

    第二个返回值是给用户看的读取状态。这样 PDF 是扫描件、加密或格式损坏时，
    详情页能说清是哪一种，而不是笼统地怪用户“正文不足”。
    """
    if row["source_type"] != "file" or not row["file_path"]:
        return "", ""
    path = UPLOAD_DIR / row["file_path"]
    if not path.is_file():
        return "", "原件不在本机，可能已被移动或删除"
    if (row["file_size"] or 0) > EXTRACT_LIMIT:
        return "", f"原件超过 {EXTRACT_LIMIT // 1024 // 1024} MB，暂时不自动读取"
    text, status = extract_file_text(path, Path(path).suffix.lower()[:12])
    if not text:
        return "", status
    # 抽出来是一堆控制字符/替换符的，等于没抽——不如让 AI 说"读不了"。
    # 旧判据用 isprintable()：\ufffd 和私用区码位都是「可打印」，真乱码
    # 反而抓不到；统一走 pdf_text 的编码坏信号判定，也不会误伤目录点线。
    if pdf_text.looks_garbled(text):
        return "", "原件里的文字编码无法识别"
    return text, ""


def usable_clip_content(conn, row: sqlite3.Row,
                        allow_fetch: bool = True) -> tuple[str, str, str]:
    """AI 能读到的材料：已入库正文 → 原件临时读取 → 我写的话 → 已有的简介。

    第三个返回值是材料来源（text / file / excerpt / summary / notes）。
    文件件导入时已自动抽好正文，优先用入库版本，保证与阅读器里的版式一致。"""
    content = ""
    origin = ""
    file_error = ""
    # 优先用入库正文：版式与正文面板一致，也不必每次提问都重新解析原件。
    content = (row["content_text"] or "").strip()
    if content:
        origin = "text"
    # 旧条目 / 扫描件 / 抽取失败时，再临时读原件兜底
    if not content and row["source_type"] == "file" and row["file_path"]:
        content, file_error = file_text_for_ai(row)
        if content:
            origin = "file"
    if not content:
        content = (row["excerpt"] or "").strip()
        if content:
            origin = "excerpt"
    if not content:
        content = (row["summary"] or "").strip()
        if content:
            origin = "summary"
    # 自己写的随手记也算材料：AI 应该看到我在这条上面补过什么
    notes = [r["text"].strip() for r in notes_of(conn, row["id"]) if (r["text"] or "").strip()]
    if notes:
        joined = "\n".join(f"- {t}" for t in notes)[:4000]
        content = (content + "\n\n【我写的随手记】\n" + joined).strip() if content else joined
        origin = "notes" if not origin else origin
    # 正文批注比整段正文更接近“我的阅读结果”，让 AI 能区分原文与我的判断。
    anns = annotations_of(conn, row["id"])
    if anns:
        lines = []
        for a in anns[:80]:
            quote = re.sub(r"\s+", " ", a["quote"] or "").strip()[:180]
            note = re.sub(r"\s+", " ", a["note"] or "").strip()[:260]
            label = ANNOTATION_COLORS.get(a["color"], ANNOTATION_COLORS["amber"])
            lines.append(f"- [{label}]「{quote}」" + (f"：{note}" if note else ""))
        joined = "\n".join(lines)[:5000]
        content = (content + "\n\n【我在正文上的批注】\n" + joined).strip() if content else joined
        origin = "annotations" if not origin else origin
    return content[:60_000], file_error or row["fetch_error"] or "", origin



def missing_material_message(row: sqlite3.Row, detail: str = "", *,
                             lead: str = "", min_chars: int = 20) -> str:
    """把“正文不足”翻译成当前材料真正能执行的下一步。"""
    detail = str(detail or "").strip().rstrip("。；; ")
    if row["source_type"] == "file":
        name = row["file_name"] or row["title"] or "这件原件"
        reason = f"：{detail}" if detail else ""
        return (f"{lead}AI 没从「{name}」里读到足够的文字{reason}。"
                "如果这是扫描版 PDF，需要先做 OCR；原件本身仍可直接打开核对。")
    if row["url"]:
        reason = f"（{detail}）" if detail else ""
        return f"{lead}这个链接目前只有标题和地址{reason}。先点「抓正文」，AI 才有材料可读。"
    reason = f"：{detail}" if detail else ""
    return f"{lead}可用文字不足 {min_chars} 字{reason}。再补一段材料或说明。"



@route_post("/api/ai/summarize")
def api_ai_summarize(conn, qs, body):
    """读网页正文（或剪藏摘录）→ AI 生成简短介绍 → 存入 clip.summary。"""
    clip_id = body.get("id")
    r = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not r:
        return {"error": "剪藏不存在"}
    clip = clip_row(r)

    content_text, fetch_error, _origin = usable_clip_content(conn, r)
    if len(content_text) < 20:
        return {"error": missing_material_message(r, fetch_error)}

    try:
        summary = ai_chat([
            {"role": "system", "content":
                "你是信息剪藏的摘要助手。用不超过 3 句中文概括给定内容的核心："
                "它说了什么（事实或观点）、来源是谁/什么立场（如果看得出）。"
                "直接陈述，不要'这篇文章介绍了'之类的客套开头。"},
            {"role": "user", "content": f"标题：{clip['title']}\n\n内容：\n{content_text[:6000]}"},
        ], max_tokens=400)
    except RuntimeError as e:
        return {"error": str(e)}
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        return {"error": f"AI 调用失败: {e}"}

    conn.execute("UPDATE clips SET summary=?,updated_at=datetime('now','localtime') WHERE id=?",
                 (summary, clip_id))
    conn.commit()
    return {"ok": True, "id": clip_id, "summary": summary}


@route_post("/api/ai/classify")
def api_ai_classify(conn, qs, body):
    """AI 辅助归类：给剪藏建议标签 + 是否值得升格为命题。"""
    title = str(body.get("title") or "")
    excerpt = str(body.get("excerpt") or "")
    if not (title or excerpt):
        return {"error": "内容为空"}
    freq = {}
    for r in conn.execute("SELECT tags FROM clips").fetchall():
        for t in json.loads(r["tags"] or "[]"):
            freq[t] = freq.get(t, 0) + 1
    top_tags = [t for t, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:30]]
    try:
        raw = ai_chat([
            {"role": "system", "content":
                "你是信息归档助手。为给定的信息剪藏建议 2-4 个中文标签（优先从已有标签池选，"
                "没有合适的可新造），并判断：这是一条可验证的事实陈述，还是仅仅是值得记录的观点/资讯？"
                "输出 JSON：{\"tags\": [\"..\"], \"kind\": \"fact|opinion\", \"reason\": \"一句话\"}。只输出 JSON。"},
            {"role": "user", "content":
                f"已有标签池：{top_tags}\n\n标题：{title}\n摘录：{excerpt[:2000]}"},
        ], max_tokens=500)
        data = parse_json_object(raw)
        tags = clean_strings(data.get("tags"), 6)
        kind = data.get("kind") if data.get("kind") in {"fact", "opinion"} else "opinion"
        reason = str(data.get("reason") or "")[:500]
        if body.get("id") and body.get("apply"):
            row = conn.execute("SELECT tags FROM clips WHERE id=?", (body["id"],)).fetchone()
            if row:
                merged = clean_strings([*clean_strings(json_value(row["tags"], [])), *tags])
                conn.execute("UPDATE clips SET tags=?,updated_at=datetime('now','localtime') WHERE id=?",
                             (json.dumps(merged, ensure_ascii=False), body["id"]))
                conn.commit()
        return {"ok": True, "tags": tags, "kind": kind, "reason": reason}
    except RuntimeError as e:
        return {"error": str(e)}
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"AI 归类结果无法解析：{e}"}


ANALYZE_SYSTEM = """你是「观澜」的研究加工模块。只能依据给定原文，不使用外部知识，不补造数字。
输出一个 JSON 对象，结构如下：
{
  "summary": "不超过120字的摘要",
  "key_points": ["3-6条结构化要点，每条含必要语境"],
  "comparison": {"agreements": [], "differences": [], "uncertainties": []},
  "conclusion": "对研究或决策的辅助结论；证据不足时明确说不足",
  "todos": ["可执行的后续核验或行动，最多5项"],
  "tags": ["2-5个便于检索的中文标签"],
  "chart": {
    "suitable": true或false,
    "reason": "适合或不适合图表的具体原因",
    "type": "bar或line",
    "title": "图表标题",
    "unit": "单位",
    "data": [{"label": "期间或类别", "value": 数字}]
  }
}
要求：
1. 只有原文存在至少两组可直接比较的数值时才令 chart.suitable=true；时间序列用 line，类别比较用 bar。
2. 图表数值必须逐字来自原文，不估算、不换算、不插值；不满足时必须 suitable=false，并明确反馈原因。
3. 单条材料无需强行对比，comparison 三个数组可为空。
4. 不把建议写成已发生事实。只输出 JSON，不要代码围栏。"""


def parse_json_object(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型返回的 JSON 顶层不是对象")
    return value


def text_list(value, limit=8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:800] for item in value if str(item).strip()][:limit]


def validated_chart(chart, source_text: str) -> dict:
    if not isinstance(chart, dict) or not chart.get("suitable"):
        reason = chart.get("reason") if isinstance(chart, dict) else ""
        return {"suitable": False, "reason": str(reason or "内容不含适合可视化的可比数值"),
                "type": "", "title": "", "unit": "", "data": []}
    chart_type = chart.get("type")
    minimum = 3 if chart_type == "line" else 2
    if chart_type not in {"bar", "line"}:
        minimum = 0
    numbers = []
    for token in re.findall(r"-?\d[\d,]*(?:\.\d+)?", source_text):
        try:
            numbers.append(float(token.replace(",", "")))
        except ValueError:
            pass
    points, seen = [], set()
    for item in (chart.get("data") or [])[:30]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if (not label or label in seen or not math.isfinite(value)
                or not any(math.isclose(value, n, rel_tol=1e-9, abs_tol=1e-9) for n in numbers)):
            continue
        seen.add(label)
        points.append({"label": label, "value": value})
    if not minimum or len(points) < minimum or len(points) > 24:
        return {"suitable": False,
                "reason": "未生成图表：可核验的原文数值不足、格式无效或数量超出范围",
                "type": "", "title": "", "unit": "", "data": []}
    return {"suitable": True, "reason": str(chart.get("reason") or "数值均可在原文中核验"),
            "type": chart_type, "title": str(chart.get("title") or "原文数值")[:120],
            "unit": str(chart.get("unit") or "")[:30], "data": points}


@route_post("/api/ai/analyze")
def api_ai_analyze(conn, qs, body):
    try:
        ids = list(dict.fromkeys(int(v) for v in (body.get("ids") or [body.get("id")]) if v))[:4]
    except (TypeError, ValueError):
        return {"error": "剪藏 ID 无效"}
    if not ids:
        return {"error": "请选择至少一条剪藏"}
    comparing = len(ids) > 1
    if comparing and len(ids) < 2:
        return {"error": "对比分析至少需要两条剪藏"}
    rows = []
    parts = []
    for cid in ids:
        row = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
        if not row:
            return {"error": f"剪藏 {cid} 不存在"}
        content, fetch_error, _origin = usable_clip_content(conn, row)
        if len(content) < 20:
            return {"error": missing_material_message(
                row, fetch_error, lead=f"剪藏 {cid}「{row['title']}」：")}
        rows.append(row)
        parts.append(f"【剪藏 {cid}｜{row['title']}】\n{content[:14_000]}")
    source_text = "\n\n".join(parts)
    title = f"{len(ids)} 条剪藏对比" if comparing else f"研究加工 · {rows[0]['title']}"
    clip_id = None if comparing else ids[0]
    used_chars = len(source_text)
    head = "请对比以下材料，重点列出共识、差异与无法裁决处。\n" if comparing else ""

    def build_user(piece: str, limit: int) -> str:
        tip = f"（只给到正文开头 {limit} 字，请据此作答，并在 summary 里注明只覆盖开头。）\n" if limit else ""
        return tip + head + piece

    try:
        raw, used_chars = ai_with_excerpt(ANALYZE_SYSTEM, build_user,
                                          source_text[:56_000], 5000)
    except AIRefusal:
        return {"error": REFUSAL_MESSAGE, "refused": True}
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        data = parse_json_object(raw)
    except (ValueError, json.JSONDecodeError) as e:
        output_id = save_ai_output(conn, "compare" if comparing else "analyze", title + "（待修复）",
                                   {"raw": raw, "parse_error": str(e)}, clip_id, ids)
        conn.commit()
        return {"error": "AI 返回内容不是合法 JSON，原始结果已保存，可在研究记录中编辑", "output_id": output_id}
    comparison = data.get("comparison") if isinstance(data.get("comparison"), dict) else {}
    normalized = {
        "summary": str(data.get("summary") or "").strip()[:2000],
        "key_points": text_list(data.get("key_points"), 8),
        "comparison": {
            "agreements": text_list(comparison.get("agreements"), 6),
            "differences": text_list(comparison.get("differences"), 6),
            "uncertainties": text_list(comparison.get("uncertainties"), 6),
        },
        "conclusion": str(data.get("conclusion") or "").strip()[:3000],
        "todos": text_list(data.get("todos"), 6),
        "tags": clean_strings(data.get("tags"), 8),
        "chart": validated_chart(data.get("chart"), source_text),
        "coverage": used_chars,          # 用上了多少字；小于原文长度就说明只覆盖开头
        "source_chars": len(source_text),
    }
    output_id = save_ai_output(conn, "compare" if comparing else "analyze", title, normalized, clip_id, ids)
    if not comparing:
        updates, args = ["summary=?", "updated_at=datetime('now','localtime')"], [normalized["summary"]]
        if normalized["tags"]:
            merged = clean_strings([*clean_strings(json_value(rows[0]["tags"], [])), *normalized["tags"]])
            updates.insert(1, "tags=?")
            args.insert(1, json.dumps(merged, ensure_ascii=False))
        args.append(ids[0])
        conn.execute(f"UPDATE clips SET {', '.join(updates)} WHERE id=?", args)
    conn.commit()
    return {"ok": True, "output_id": output_id, "result": normalized,
            "chart_message": normalized["chart"]["reason"],
            "partial": used_chars < len(source_text),
            "coverage": used_chars, "source_chars": len(source_text)}


DIGEST_SYSTEM = """你在给「观澜」的收存写卡片简介。只依据给定原文，不用外部知识。

输出一个 JSON 对象：
{"summary": "不超过80字：这是什么、说了什么、看得出什么立场", "tags": ["2-4个中文标签"],
 "grade": "canon | ref | daily 之一", "grade_reason": "不超过30字，说明为什么判这一级"}

材料级的判据（按内容性质判，不按你好不好奇判）：
- canon（权威）：法规原文、统计公报、官方通报、机构报告、期刊论文、一手数据。有可引用的数字或条文。
- ref（参考）：深度报道、专业分析、书评影评、长文论述。有论证过程，不是纯报事。
- daily（资讯）：每日新闻、快讯、事件通报、推送、公众号短文。只交代发生了什么，没有论证也没有可引数据。

要求：
1. 直接陈述内容，不写"这篇文章""本文提到"这类开头。
2. 看不出立场和来源就不要断言可信度，别硬猜。
3. 不评价、不推荐，只做摘要。
4. grade 宁低勿高：拿不准就在 ref 和 daily 之间选，绝不为了讨好而给 canon。"""


@route_post("/api/ai/digest")
def api_ai_digest(conn, qs, body):
    """收下之后自动跑一次：一句话简介 + 标签 + 材料级建议，然后把正文丢掉。"""
    cid = body.get("id")
    row = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"error": "剪藏不存在"}
    content, fetch_error, _origin = usable_clip_content(conn, row)
    if len(content) < 20:
        conn.commit()
        return {"error": missing_material_message(row, fetch_error)}
    used = len(content)
    try:
        raw, used = ai_with_excerpt(
            DIGEST_SYSTEM,
            lambda piece, limit: (f"标题：{row['title']}\n"
                                  + (f"（只有正文开头 {limit} 字）\n" if limit else "")
                                  + f"\n{piece}"),
            content[:6000], 700)
        data = parse_json_object(raw)
    except AIRefusal:
        conn.commit()
        return {"error": REFUSAL_MESSAGE, "refused": True}
    except RuntimeError as e:
        return {"error": str(e)}
    except (ValueError, json.JSONDecodeError):
        conn.commit()
        return {"error": "AI 返回的内容不是合法 JSON，简介没生成"}

    summary = str(data.get("summary") or "").strip()[:300]
    new_tags = clean_strings(data.get("tags"), 6)
    merged = clean_strings([*clean_strings(json_value(row["tags"], [])), *new_tags])

    # 材料级只作建议：写进 grade_hint，等你点一下才落到 grade 上。
    # 让模型直接给东西盖上「权威」的章，是这个库最快失效的方式。
    try:
        hinted = clean_grade(data.get("grade"))
    except ValidationError:
        hinted = ""
    hint_reason = str(data.get("grade_reason") or "").strip()[:80]
    current = str(row["grade"] or "")
    if not hinted or hinted == current:
        hinted, hint_reason = "", ""

    conn.execute(
        """UPDATE clips SET summary=?, tags=?, grade_hint=?, grade_hint_reason=?,
           updated_at=datetime('now','localtime') WHERE id=?""",
        (summary, json.dumps(merged, ensure_ascii=False), hinted, hint_reason, cid))
    conn.commit()
    return {"ok": True, "id": cid, "summary": summary, "tags": merged,
            "grade_hint": hinted, "grade_hint_label": GRADES.get(hinted, ""),
            "grade_hint_reason": hint_reason,
            "partial": used < len(content), "coverage": used, "source_chars": len(content)}


REVIEW_SYSTEM = """你在写「观澜」的收存回顾，读者就是收存这些东西的人。只依据给出的条目，不用外部知识。

输出中文纯文本，三段式，不要小标题、不要编号清单：
1. 一句话概括这段时间收的东西整体在讲什么。
2. 3–6 句主线，每句一句一事，句末带 [S-编号] 引用。
3. 一句话点出哪里还缺证据、或者哪条值得回头再看。

不写"根据材料""以上"，不复述标题，不评价好坏。"""


@route_post("/api/ai/review")
def api_ai_review(conn, qs, body):
    """把最近收的东西串成一段回顾——这是「存了之后有什么用」的那个用处。"""
    try:
        days = max(1, min(365, int(body.get("days") or 7)))
    except (TypeError, ValueError):
        days = 7
    rows = conn.execute(
        """SELECT id,title,tier,grade,summary,excerpt FROM clips
           WHERE archived=0 AND created_at >= datetime('now','localtime',?)
           ORDER BY id DESC LIMIT 40""", (f"-{days} days",)).fetchall()
    if not rows:
        return {"ok": True, "answer": "", "used": [], "count": 0, "output_id": None,
                "empty": f"最近 {days} 天没有新收存。先存点东西，回顾才有得写。"}

    blocks = []
    for r in rows:
        text = (r["summary"] or r["excerpt"] or "").strip()
        blocks.append(f"{material_head(r, r['id'])}\n{text[:600]}")
    prompt = f"时间范围：最近 {days} 天，共 {len(rows)} 条。\n\n" + "\n\n".join(blocks)
    try:
        answer = ai_chat([
            {"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content": prompt[:40_000]},
        ], max_tokens=2000)
    except RuntimeError as e:
        return {"error": str(e)}

    # 引用只能指向真实条目；编出来的编号剔掉，句子保留
    allowed = {f"S-{r['id']}" for r in rows}
    answer = re.sub(r"\[(S-\d+)\]", lambda m: m.group(0) if m.group(1) in allowed else "", answer)
    answer = re.sub(r"[ \t]+\n", "\n", answer).strip()
    if not answer:
        return {"ok": True, "answer": "", "used": [], "count": len(rows), "output_id": None,
                "empty": "模型这次没写出东西，再试一次。"}

    used = [{"id": r["id"], "title": r["title"], "tier": r["tier"] or "",
             "grade": r["grade"] or "", "grade_label": grade_tag(r["grade"])} for r in rows]
    output_id = save_ai_output(conn, "review", f"回顾 · 最近 {days} 天",
                               {"days": days, "answer": answer, "used": used},
                               source_ids=[r["id"] for r in rows])
    conn.commit()
    return {"ok": True, "answer": answer, "used": used, "count": len(rows),
            "output_id": output_id}


@route_get("/api/ai/outputs")
def api_ai_outputs(conn, qs):
    clip_id = (qs.get("clip_id") or [None])[0]
    kind = (qs.get("kind") or [None])[0]
    where, args = ["1=1"], []
    if clip_id:
        where.append("clip_id=?")
        args.append(clip_id)
    if kind:
        where.append("kind=?")
        args.append(kind)
    rows = conn.execute(
        f"SELECT * FROM ai_outputs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 100", args).fetchall()
    return {"outputs": [ai_output_row(r) for r in rows]}


@route_post("/api/ai/outputs/{oid}")
def api_ai_output_update(conn, qs, body, oid):
    row = conn.execute("SELECT * FROM ai_outputs WHERE id=?", (oid,)).fetchone()
    if not row:
        return {"error": "研究记录不存在"}
    content = body.get("content")
    if not isinstance(content, dict):
        return {"error": "content 必须是 JSON 对象"}
    title = str(body.get("title") or row["title"]).strip()[:200]
    conn.execute("UPDATE ai_outputs SET title=?,content=?,updated_at=datetime('now','localtime') WHERE id=?",
                 (title, json.dumps(content, ensure_ascii=False), oid))
    conn.commit()
    return {"ok": True}


@route_post("/api/ai/outputs/{oid}/delete")
def api_ai_output_delete(conn, qs, body, oid):
    conn.execute("DELETE FROM ai_outputs WHERE id=?", (oid,))
    conn.commit()
    return {"ok": True}


INSTRUCT_SYSTEM = """你在读「观澜」里的一条收存材料，按用户的指令处理它。

规则：
1. 只依据这条材料，不引入外部知识；材料里没有的就说没有。
2. 用户让你做什么就做什么——提炼、挑刺、换角度、翻译成人话都行，别自作主张改成摘要。
3. 不加开场白和总结句，不写"根据材料"。
4. 中文。要列点就列点，不要用 Markdown 符号。"""


@route_post("/api/ai/instruct")
def api_ai_instruct(conn, qs, body):
    """让用户自己下指令：不是选功能，是直接跟 AI 说一句话。"""
    cid = body.get("id")
    prompt_text = str(body.get("prompt") or "").strip()
    if len(prompt_text) < 2:
        return {"error": "指令太短，多写两个字"}
    if len(prompt_text) > 200:
        return {"error": "指令最多 200 字"}
    row = conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"error": "剪藏不存在"}

    content, fetch_error, origin = usable_clip_content(conn, row)
    material = (content or row["summary"] or row["excerpt"] or "").strip()
    if len(material) < 20:
        conn.commit()
        return {"error": missing_material_message(row, fetch_error)}

    used = len(material)
    try:
        answer, used = ai_with_excerpt(
            INSTRUCT_SYSTEM,
            lambda piece, limit: (f"材料标题：{row['title']}\n"
                                  + (f"（只给到开头 {limit} 字）\n" if limit else "")
                                  + f"\n材料：\n{piece}\n\n---\n\n我的指令：{prompt_text}"),
            material[:20_000], 2000)
    except AIRefusal:
        return {"error": REFUSAL_MESSAGE, "refused": True}
    except RuntimeError as e:
        return {"error": str(e)}
    finally:
        # 这条指令用完，正文照旧不留在库里
        conn.commit()

    text = answer.strip()
    if not text:
        return {"error": "模型这次没给出内容，换个说法再试"}
    output_id = save_ai_output(conn, "instruct", prompt_text[:60],
                               {"prompt": prompt_text, "text": text,
                                "coverage": used, "source_chars": len(material),
                                "source": origin},
                               clip_id=cid, source_ids=[cid])
    conn.commit()
    return {"ok": True, "output_id": output_id, "text": text, "prompt": prompt_text,
            "partial": used < len(material), "coverage": used, "source_chars": len(material),
            "source": origin, "direct_file": origin == "file"}


@route_post("/api/ai/test")
def api_ai_test(conn, qs, body):
    try:
        answer = ai_chat([
            {"role": "system", "content": "连接测试，只回复 OK。"},
            {"role": "user", "content": "ping"},
        ], max_tokens=20)
        return {"ok": True, "answer": answer[:40], "model": ai_config()["model"]}
    except RuntimeError as e:
        return {"error": str(e)}


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
            tier = it.get("tier") or it.get("origin_tier") or "T2"
            if tier not in TIER_CEILING:
                continue
            conf = float(it.get("confidence") or TIER_CEILING[tier])
            if not math.isfinite(conf):
                continue
            conf = max(0, min(conf, TIER_CEILING[tier]))
            parsed.append({
                "kind": "question" if it.get("question") else "claim",
                "statement": it.get("statement") or it.get("question") or "",
                "question": it.get("question") or "",
                "resolution": it.get("resolution") or "",
                "tier": tier, "confidence": round(conf, 2),
                "as_of": it.get("as_of") or "",
                "entities": clean_strings(it.get("entities")),
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


def relevant_claims(conn, query: str, limit=24, exclude_id=None) -> list[sqlite3.Row]:
    """按实际文本重合排序；没有相关证据时返回空，绝不用最新/随机条目冒充上下文。"""
    rows = conn.execute(
        "SELECT * FROM claims WHERE status='active' AND kind='claim' ORDER BY id DESC LIMIT 5000").fetchall()
    ascii_terms = {t.lower() for t in re.findall(r"[A-Za-z0-9_.-]{2,}", query)}
    zh = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    stop = {"什么", "怎么", "如何", "现在", "状况", "是否", "一个", "我们", "他们", "因为", "所以"}
    grams = {zh[i:i + 2] for i in range(max(0, len(zh) - 1))}
    grams.update(zh[i:i + 3] for i in range(max(0, len(zh) - 2)))
    grams = {g for g in grams if g not in stop}
    ranked = []
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        c = row_to_claim(row)
        text = " ".join([c["statement"], c["notes"] or "", " ".join(c["entities"]),
                         " ".join(c["tags"]), c["code"]]).lower()
        score = sum(4 for term in ascii_terms if term in text)
        if len(zh) >= 2 and zh in text:
            score += max(2, len(zh))
        score += sum(1 for gram in grams if gram in text)
        if c["code"].lower() in query.lower():
            score += 100
        if score >= 2:
            ranked.append((score, row["id"], row))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in ranked[:limit]]


# ── AI 洞察：换一副眼镜看同一个库 ─────────────────────
# flomo 的洞察靠「视角」；这里一样，但视角是围绕「材料库」设计的：
# 看模式、看价值、看缺口、看立场变化，而不是看情绪和人格。

INSIGHT_BASE = """你是「观澜」的洞察模块。你会读到一个人自己收存、并写下过判断的材料，任务是穿过这些材料，说出他没说出来的东西。

硬规矩：
1. 只用这里给出的材料。看不到的就说不确定，不要用常识补，不要虚构条目。
2. 说人话。不写「综上所述」「总而言之」「值得一提的是」「可以看出」。
3. 指到具体条目时写《标题》，标题必须一字不差地来自材料。
4. 每段先给判断，再给依据。不要先铺垫一大段再落结论。
5. 至少有一条是他可能不想听的。不夸、不安慰、不罗列他的优点。
6. 中文，600–900 字，4–6 段，段落之间空一行。不加小标题，不列编号清单，不用 Markdown 记号。
最后单独一行，以「下一步：」开头，给 1–2 个具体、今天就能做的动作。"""

INSIGHT_LENSES = [
    {"key": "default", "name": "默认洞察", "by": "观澜", "icon": "pattern",
     "desc": "从材料里看出反复出现的模式，和你反复使用却没定义的前提",
     "ask": "找出这些材料里反复出现的主题；指出他反复使用却从未定义的 1–2 个前提；再指出哪两条看似无关的条目其实在讲同一件事。"},
    {"key": "value", "name": "价值澄清", "by": "观澜", "icon": "scale",
     "desc": "从你收什么、在哪类事上花笔墨，反推你真正在意的东西",
     "ask": "不要看他声称的观点，看他选择了什么材料、在哪类事情上花了最多的笔墨。反推他真正在意的东西，并指出他的行动和他说过的优先级之间的落差。"},
    {"key": "invert", "name": "逆向思考", "by": "芒格", "icon": "reverse",
     "desc": "假设你的判断是错的：什么证据能一次推翻它们",
     "ask": "假设这批材料里的判断整体是错的。找出能让它们同时崩塌的那个共同假设，并列出三条能推翻它的证据；若库里根本没有这类证据，直接说这是缺口，而不是替他辩护。"},
    {"key": "second", "name": "二阶思考", "by": "芒格", "icon": "chain",
     "desc": "这条结论成立之后，接下来会发生什么，再接下来呢",
     "ask": "挑出这批材料里最核心的那一个判断，推演它的二阶后果：如果它成立，接下来会发生什么；那之后呢。至少走到第三层，每一层都要说清依据在哪儿。"},
    {"key": "blind", "name": "盲点地图", "by": "观澜", "icon": "eye",
     "desc": "清点你的库缺什么：哪类证据一条都没有",
     "ask": "清点这批材料的结构：哪些板块厚、哪些是空的；哪些结论只有正方没有反方；哪些时间、地区、人群完全没出现。给一张缺口清单，按「补上之后最能改变结论」排序。"},
    {"key": "drift", "name": "立场漂移", "by": "观澜", "icon": "split",
     "desc": "同一件事，你的说法在这段时间里怎么变的",
     "ask": "按时间顺序看这些材料，指出他对同一件事的说法在哪一刻变了、变在哪、是换了材料来源还是自己想通了。如果一直没变，说清是什么让他稳住了，以及这种稳定是好事还是僵硬。"},
    {"key": "devil", "name": "反方代理", "by": "观澜", "icon": "debate",
     "desc": "替最强的反方说话，不搞平衡，不留情面",
     "ask": "你是反方。不要平衡，不要两面都讲。挑出他最核心、最自信的那个判断，用最强的论证打它。允许刻薄，但不许编造事实——所有攻击必须落在库里真实存在的材料上。最后告诉他，如果他是对的，必须满足什么条件。"},
    {"key": "concept", "name": "概念考古", "by": "观澜", "icon": "branch",
     "desc": "你反复用的那几个词，各自在不同条目里指什么",
     "ask": "挑出他反复使用却从未定义的 2–3 个关键词。分别列出这些词在不同条目里实际指的东西，指出它们互相矛盾的地方，并给出一个能消除歧义的区分。"},
]

INSIGHT_BY_KEY = {item["key"]: item for item in INSIGHT_LENSES}


def insight_corpus(conn, scope_kind: str = "all", scope_id: str = "",
                   limit: int = 120, budget: int = 40_000):
    """把库里的材料拼成一段给模型看的文本。

    选取不是简单取最新：有判断、有随手记、有正文批注、有正文的条目更能看出东西，
    但太旧的材料也要给机会——按「信息量 + 新鲜度」打分取前 N，
    再按时间正序排，模型才看得出立场怎么漂。
    """
    where, args = ["archived=0"], []
    if scope_kind == "board":
        if str(scope_id) == "none":
            where.append("board_id IS NULL")
        elif str(scope_id).isdigit():
            where.append("board_id=?")
            args.append(int(scope_id))
    elif scope_kind == "tag" and scope_id:
        where.append("EXISTS (SELECT 1 FROM json_each(clips.tags) j WHERE j.value=?)")
        args.append(str(scope_id))
    elif scope_kind == "recent":
        try:
            days = max(1, min(730, int(scope_id or 30)))
        except ValueError:
            days = 30
        where.append("created_at >= datetime('now','localtime',?)")
        args.append(f"-{days} days")

    rows = conn.execute(
        f"""SELECT id,title,summary,excerpt,content_text,tags,grade,tier,created_at
            FROM clips WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 800""", args).fetchall()
    if not rows:
        return [], ""

    notes = {}
    for r in conn.execute("SELECT clip_id, text FROM clip_notes WHERE text<>'' ORDER BY id"):
        notes.setdefault(r["clip_id"], []).append((r["text"] or "").strip())
    annotations = {}
    for r in conn.execute(
            "SELECT clip_id, quote, note, color FROM clip_annotations ORDER BY id"):
        annotations.setdefault(r["clip_id"], []).append(r)
    verdicts = {r["clip_id"]: r for r in conn.execute("SELECT * FROM verdicts")}

    now = datetime.now()
    scored = []
    for r in rows:
        try:
            age = max(0, (now - datetime.fromisoformat(r["created_at"])).days)
        except (TypeError, ValueError):
            age = 365
        has_verdict = r["id"] in verdicts
        has_notes = bool(notes.get(r["id"]))
        has_annotations = bool(annotations.get(r["id"]))
        score = 0.0
        score += 3.0 if has_verdict else 0.0
        score += 1.6 if has_notes else 0.0
        score += 1.4 if has_annotations else 0.0
        score += 1.2 if (r["summary"] or "").strip() else 0.0
        score += 1.0 if (r["excerpt"] or "").strip() else 0.0
        score += min(len(r["content_text"] or "") / 1200, 2.0)
        score += 0.8 if str(r["grade"] or "") in {"canon", "ref"} else 0.0
        score += max(0.0, 1.6 - age / 120)        # 越新越靠前，但压不过信息量
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    picked = [r for _s, r in scored[:limit]]
    picked.sort(key=lambda r: (r["created_at"] or "", r["id"]))

    blocks, used_total = [], 0
    for r in picked:
        verdict = verdicts.get(r["id"])
        head = f"[#{r['id']}｜{(r['created_at'] or '')[:10]}｜{grade_tag(r['grade'])}"
        if r["tier"]:
            head += f"·{r['tier']}"
        head += "] " + (r["title"] or "").strip()[:90]
        body = ""
        summary = (r["summary"] or "").strip()
        excerpt = (r["excerpt"] or "").strip()
        content = (r["content_text"] or "").strip()
        if summary:
            body += "简介：" + summary[:280] + "\n"
        if excerpt:
            body += "我的话：" + excerpt[:220] + "\n"
        elif content:
            body += "正文：" + re.sub(r"\s+", " ", content)[:420] + "\n"
        note_text = "；".join(n[:120] for n in notes.get(r["id"], [])[:2])
        if note_text:
            body += "随手记：" + note_text + "\n"
        ann_texts = []
        for a in annotations.get(r["id"], [])[:4]:
            quote = re.sub(r"\s+", " ", a["quote"] or "").strip()[:120]
            note = re.sub(r"\s+", " ", a["note"] or "").strip()[:120]
            label = ANNOTATION_COLORS.get(a["color"], ANNOTATION_COLORS["amber"])
            ann_texts.append(f"[{label}]「{quote}」" + (f"：{note}" if note else ""))
        if ann_texts:
            body += "正文批注：" + "；".join(ann_texts) + "\n"
        if verdict:
            conf = {"high": "高", "mid": "中", "low": "低"}.get(verdict["confidence"], "中")
            body += f"我的判断（{conf}置信）：" + (verdict["text"] or "").strip()[:180] + "\n"
        piece = head + "\n" + body.strip()
        if used_total + len(piece) > budget:
            break
        blocks.append(piece)
        used_total += len(piece)

    return picked[:len(blocks)], "\n\n".join(blocks)


@route_get("/api/insights/lenses")
def api_insight_lenses(conn, qs):
    total = conn.execute("SELECT COUNT(*) n FROM clips WHERE archived=0").fetchone()["n"]
    return {"ok": True, "lenses": INSIGHT_LENSES, "total": total,
            "ready": bool(ai_config()["base_url"] and ai_config()["key"])}


@route_post("/api/insights/run")
def api_insight_run(conn, qs, body):
    """拿一个视角把库过一遍。结果存进 ai_outputs，能回头翻。"""
    key = str(body.get("lens") or "default").strip()
    lens = INSIGHT_BY_KEY.get(key)
    if not lens:
        return {"error": "没有这个视角"}
    scope_kind = str(body.get("scope_kind") or "all")
    scope_id = str(body.get("scope_id") or "")
    rows, corpus = insight_corpus(conn, scope_kind, scope_id)
    if len(rows) < 3:
        return {"error": f"这个范围只有 {len(rows)} 条材料，太少，看不出模式。换个范围，或者先收点东西。"}

    scope_text = {"all": "全部收存", "recent": f"最近 {scope_id or 30} 天"}.get(scope_kind)
    if scope_kind == "board":
        if str(scope_id).isdigit():
            scope_text = "板块「" + board_names(conn).get(int(scope_id), "?") + "」"
        else:
            scope_text = "板块「未分类」"
    scope_text = scope_text or "这批材料"

    try:
        answer = ai_chat([
            {"role": "system", "content": INSIGHT_BASE},
            {"role": "user", "content":
                f"视角：{lens['name']}。\n任务：{lens['ask']}\n\n"
                f"范围：{scope_text}，共 {len(rows)} 条材料。\n\n" + corpus},
        ], max_tokens=2600)
    except RuntimeError as e:
        return {"error": str(e)}

    answer = (answer or "").strip()
    if len(answer) < 120:
        return {"error": "模型这次没写出东西，再试一次。"}

    used = [{"id": r["id"], "title": r["title"], "grade": r["grade"] or "",
             "grade_label": grade_tag(r["grade"])} for r in rows]
    title = f"洞察 · {lens['name']} · {len(rows)} 条"
    payload = {"lens": lens["key"], "lens_name": lens["name"], "text": answer,
               "scope": scope_text, "count": len(rows), "used": used}
    saved, warning = True, ""
    try:
        out_id = save_insight_record("insight", title, payload,
                                    source_ids=[r["id"] for r in rows])
    except sqlite3.Error as e:
        # 存不进历史就算了，答案已经在这儿——不能因为记不上账把内容丢了
        out_id, saved = None, False
        warning = f"这篇没能存进「过往洞察」（内部错误 ref={internal_error_ref('save_insight_record', e)}），先复制下来"
        print(f"[insight] 存历史失败: {type(e).__name__}: {e}", flush=True)
    return {"ok": True, "id": out_id, "saved": saved, "warning": warning,
            "title": title, "text": answer,
            "lens": lens["key"], "lens_name": lens["name"], "count": len(rows),
            "scope": scope_text, "used": used}


@route_get("/api/insights")
def api_insights(conn, qs):
    rows = conn.execute(
        """SELECT * FROM ai_outputs WHERE kind='insight' ORDER BY id DESC LIMIT 20""").fetchall()
    out = []
    for r in rows:
        item = ai_output_row(r)
        content = item.get("content") or {}
        out.append({"id": item["id"], "title": item["title"],
                    "lens": content.get("lens", ""), "lens_name": content.get("lens_name", ""),
                    "text": content.get("text", ""), "scope": content.get("scope", ""),
                    "count": content.get("count", 0),
                    "created_at": item["created_at"], "model": item.get("model", "")})
    return {"ok": True, "insights": out}


ASK_SYSTEM = """你是「观澜」的问库模块，回答只能建立在用户自己收存的材料上。

规则：
1. 每句结论后面必须紧跟引用，格式 [S-12]，12 是材料编号。没有引用的句子会被系统丢弃。
2. 每条回答必须是完整的中文句子，不能只给引用编号，也不能只复述问题。
3. 材料里没有的，直接说「收存里没有相关内容」，不要用你的先验知识补。
4. 不写「根据材料」「这篇文章提到」这类空话，直接给结论。
5. 材料带信源等级：T0 一手数据、T1 机构共识、T2 专业分析、T3 自媒体观点、T4 传闻未证。
   T3/T4 只能当线索，不能单独支撑结论；一句只用 T3/T4 作证据会被系统丢弃。
6. 材料还带材料级：权威（法规/公报/报告/论文，可作依据）、参考（深度报道与分析，可引用）、
   资讯（每日新闻，只交代发生了什么）。资讯只能作时效背景，不能支撑结论；
   一句只用资讯作证据同样会被系统丢弃。
7. 中文，能列点就列点，一条一句，句末带引用。"""


def grade_tag(grade: str) -> str:
    """材料块头上那个小标签：权威·T0 / 资讯·T3 / 未定。"""
    g = GRADES.get(str(grade or "").strip().lower(), "")
    return g or GRADE_UNSET_LABEL


def material_head(row, cid) -> str:
    label = grade_tag(row["grade"])
    tier = (row["tier"] or "").strip()
    meta = f"{label}·{tier}" if tier else label
    return f"[S-{row['id']}] ({meta}) {row['title']}"


def relevant_clips(conn, query: str, limit: int = 10):
    """在收存里按实际文本重合排序；找不出相关条就返回空，绝不拿最新条冒充上下文。"""
    rows = conn.execute(
        """SELECT id,title,url,summary,excerpt,tags,tier,grade,
                  substr(content_text,1,8000) AS content_text
           FROM clips WHERE archived=0 ORDER BY id DESC LIMIT 2000""").fetchall()
    ascii_terms = {t.lower() for t in re.findall(r"[A-Za-z0-9_.-]{2,}", query)}
    zh = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    stop = {"什么", "怎么", "如何", "现在", "状况", "是否", "一个", "我们", "他们",
            "因为", "所以", "哪些", "多少", "为什", "这个", "那个", "帮我", "告诉"}
    grams = {zh[i:i + 2] for i in range(max(0, len(zh) - 1))}
    grams.update(zh[i:i + 3] for i in range(max(0, len(zh) - 2)))
    grams = {g for g in grams if g not in stop and len(g) >= 2}

    ranked = []
    for row in rows:
        title = (row["title"] or "").lower()
        body = " ".join(filter(None, [
            row["summary"] or "", row["excerpt"] or "",
            (row["content_text"] or "")[:8000]])).lower()
        tags = (row["tags"] or "").lower()
        score = 0
        for term in ascii_terms:
            if term in title:
                score += 8
            elif term in body or term in tags:
                score += 4
        if len(zh) >= 2:
            if zh in title:
                score += max(5, len(zh))
            elif zh in body:
                score += max(3, len(zh))
        for gram in grams:
            if gram in title:
                score += 3
            elif gram in body or gram in tags:
                score += 1
        # 同样相关时，材料压过资讯：问库要的是依据，不是今天发生了什么
        score *= {"canon": 1.15, "ref": 1.08}.get(str(row["grade"] or ""), 1.0)
        if score >= 4:
            ranked.append((score, row["id"], row))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in ranked[:limit]]


@route_post("/api/ask")
def api_ask(conn, qs, body):
    """问库：只用用户收存的材料回答，引用 [S-编号]，无引用句一律丢弃。"""
    question = str(body.get("q") or "").strip()
    if len(question) < 2:
        return {"error": "问题太短"}
    rows = relevant_clips(conn, question, 10)
    used = [{"id": r["id"], "title": r["title"], "tier": r["tier"] or "",
             "grade": r["grade"] or "", "grade_label": grade_tag(r["grade"])} for r in rows]
    if not rows:
        return {"ok": True, "answer": "", "used": [], "dropped": 0, "weak": 0, "output_id": None,
                "empty": "收存里没有与这个问题相关的条目。换个关键词，或者先把材料收进来。"}

    blocks = []
    for r in rows:
        text = (r["content_text"] or r["excerpt"] or r["summary"] or "").strip()
        blocks.append(f"{material_head(r, r['id'])}\n{text[:2500]}")
    prompt = "问题：" + question + "\n\n用户收存的材料：\n\n" + "\n\n".join(blocks)
    try:
        answer = ai_chat([
            {"role": "system", "content": ASK_SYSTEM},
            {"role": "user", "content": prompt[:60000]},
        ], max_tokens=3000)
    except RuntimeError as e:
        return {"error": str(e)}

    # 引用必须落在本次实际提供的条目上，否则整句丢弃——模型不许发明出处。
    # 剥掉引用后没剩下实词的句子也丢，否则"只回一个 [S-1]"会蒙混过关。
    # I7 落到这里：一句话的证据如果全是 T3/T4，不算证据，只算线索。
    allowed = {f"S-{r['id']}" for r in rows}
    tier_of = {f"S-{r['id']}": (r["tier"] or "") for r in rows}
    grade_of = {f"S-{r['id']}": (r["grade"] or "") for r in rows}
    weak_only = {"T3", "T4"}
    kept, dropped, weak = [], 0, 0
    for sent in re.split(r"(?<=[。！？!?；;\n])", answer):
        if not sent.strip():
            continue
        cites = set(re.findall(r"\[(S-\d+)\]", sent))
        bare = re.sub(r"\[S-\d+\]", "", sent).strip(" \t\r\n。，、；;：:!?！？…—-")
        if cites and cites <= allowed:
            tiers = {tier_of[c] for c in cites} - {""}
            if tiers and tiers <= weak_only:
                weak += 1
                continue
            # 材料级同样卡一道：句子里引的全是你明确标成「资讯」的，就不算依据。
            # 未定不卡——那只是还没判，不该因为没判就没法问。
            grades = {grade_of[c] for c in cites}
            if "daily" in grades and grades <= {"daily"}:
                weak += 1
                continue
            if len(bare) >= 4:
                kept.append(sent)
            else:
                dropped += 1
        elif not cites and 2 <= len(bare) < 10 and kept:
            kept.append(sent)          # 短过渡句，无引用也留
        else:
            dropped += 1
    final = "".join(kept).strip()
    if not final:
        tip = ("只找到资讯或弱信源的材料。按「资讯只作时效背景」的规矩，这里不下结论——"
               "补一条权威或参考级的材料再来问。") if weak else "模型没给出带库内引用的回答，换个问法再试。"
        return {"ok": True, "answer": "", "used": used, "dropped": dropped, "weak": weak,
                "output_id": None, "empty": tip}

    output_id = save_ai_output(conn, "ask", question[:200],
                               {"question": question, "answer": final, "used": used},
                               source_ids=[r["id"] for r in rows])
    conn.commit()
    return {"ok": True, "answer": final, "used": used, "dropped": dropped, "weak": weak,
            "output_id": output_id}


# 库内编号出现在 AI 回答里时，允许两种形状：
#   C-2026-0001 / Q-2026-0002   用户自己的条目（前缀-年份-序号）
#   DEMO-C-0005 / DEMO-Q-0001   示例条目（带 DEMO- 隔离前缀，没有年份段）
# 这两种都要被认出来，否则示例条目的引用会让整句被当成「无有效引用」丢掉。
_CODE_IN_CITATION = r"(?:DEMO-[CPQ]-\d{3,6}|[CPQ]-\d{4}-\d{3,4})"


def normalize_citation_tail(text: str) -> str:
    """把“结论。[C-…]”改成“结论[C-…]。”，免得引用与结论被标点切开。"""
    return re.sub(
        r"([。！？!?；;]+)\s*((?:\[" + _CODE_IN_CITATION + r"\]\s*)+)",
        lambda m: m.group(2).strip() + m.group(1), text)


def extract_citations(sentence: str) -> set[str]:
    return set(re.findall(r"\[(" + _CODE_IN_CITATION + r")\]", sentence))


@route_post("/api/ai/ask")
def api_ai_ask(conn, qs, body):
    question = str(body.get("q") or "").strip()
    if len(question) < 2:
        return {"error": "问题太短"}
    rows = relevant_claims(conn, question, 24)
    if not rows:
        return {"ok": True, "answer": "库内证据不足：没有找到与问题实际相关的正典条目。请先补充来源或换个关键词。",
                "dropped": 0, "used": [], "output_id": None}
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

    # 无引用句、引用不存在条目的句子都丢弃；不允许模型发明来源。
    used = [row_to_claim(r)["code"] for r in rows]
    used_set = set(used)
    # 模型常写成“结论。[C-…]”；先把句末引用移到标点前，避免引用与结论被切开。
    normalized_answer = normalize_citation_tail(answer)
    sents = [s for s in re.split(r"(?<=[。！？!?；;\n])", normalized_answer) if s.strip()]
    kept, dropped = [], 0
    for sent in sents:
        citations = extract_citations(sent)
        if citations and citations <= used_set:
            kept.append(sent)
        else:
            dropped += 1
    final_answer = "".join(kept).strip() or "库内证据不足：模型没有产出带有效库内引用的回答，请换种问法或补充证据。"
    output_id = save_ai_output(conn, "ask", question, {
        "question": question, "answer": final_answer, "used": used[:12],
    }, source_ids=used[:12])
    conn.commit()
    return {"ok": True, "answer": final_answer, "dropped": dropped,
            "used": used[:12], "output_id": output_id}


@route_post("/api/ai/devil")
def api_ai_devil(conn, qs, body):
    code = str(body.get("code") or "").strip()
    r = conn.execute("SELECT * FROM claims WHERE code=?", (code,)).fetchone()
    if not r:
        return {"error": f"找不到 {code}"}
    c = row_to_claim(r)
    related = relevant_claims(
        conn, c["statement"] + " " + " ".join(c["entities"]), 20, exclude_id=r["id"])
    if not related:
        return {"error": "库内没有与该结论实际相关的其他证据，无法构造可靠反方；请先补充相邻证据"}
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
    output_id = save_ai_output(conn, "devil", f"反方 · {code}", {
        "claim_code": code, "answer": answer,
        "used": [row_to_claim(x)["code"] for x in related],
    }, source_ids=[code])
    conn.commit()
    return {"ok": True, "answer": answer, "output_id": output_id}


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
            code = next_code(conn, "C" if data["kind"] == "claim" else "Q", "claims")
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
    channel = {"wb": "WorkBuddy 云 · 账号额度", "openai": "自备 OpenAI 兼容端点",
               "disabled": "已停用"}.get(cfg["style"], "未配置")
    return {"ready": bool(cfg["base_url"] and cfg["key"] and cfg["model"]
                          and not cfg.get("config_error")),
            "model": cfg["model"], "channel": channel,
            "endpoint": _domain_of(cfg["base_url"]), "config_error": cfg.get("config_error") or ""}


# ── 桌面端事件专题：独立表与路由，不覆盖旧 events 时间锚点 ──
try:
    import event_topics as _event_topics
    _event_topics.install(route_get, route_post, ValidationError=ValidationError)
except Exception as _exc:  # 新板块装不上不能拖垮既有收存 / 阅读 / 圣经
    import traceback
    print(f"[event_topics] 路由装配失败：{type(_exc).__name__}: {_exc}", flush=True)
    traceback.print_exc()


# ── Handler ──────────────────────────────────────────

# ── 阅读中心：独立大板块的路由装配 ──────────────────────
# 单独放一个模块，避免把 30 万行的 server.py 再撑大；依赖显式注入，
# 不产生循环导入。AI 伴读直接复用 ai_chat()（WorkBuddy 账号云端额度）。
try:
    import reading as _reading
    _reading.install(
        route_get, route_post,
        db=db, ai_chat=ai_chat, ai_config=ai_config,
        ai_usage=lambda: dict(AI_LAST_USAGE),
        extract_file_text=extract_file_text,
        UPLOAD_DIR=UPLOAD_DIR,
        ValidationError=ValidationError,   # 路径参数非法 → 400，而不是内部错误 500
    )
except Exception as _exc:  # 阅读中心装不上不能拖垮整个应用
    import traceback
    print(f"[reading] 路由装配失败：{type(_exc).__name__}: {_exc}", flush=True)
    traceback.print_exc()

# ── 圣经阅读地：第三个独立大板块的路由装配 ──────────────
# 与阅读中心同样的注入式装配；独立数据表，不碰 clips / reading_items。
try:
    import bible as _bible
    _bible.install(
        route_get, route_post,
        db=db, ai_chat=ai_chat, ai_config=ai_config,
        ensure_logger=app_logger,   # 预热轮转日志，冷启动第一条堆栈也进 app.log
    )
except Exception as _exc:  # 新板块装不上不能拖垮整个应用
    import traceback
    print(f"[bible] 路由装配失败：{type(_exc).__name__}: {_exc}", flush=True)
    traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    server_version = "guanlan/1.0"

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        """客户端提前断开不该刷屏。

        ThreadingHTTPServer 把请求行读取时的 ConnectionResetError /
        ConnectionAbortedError 交给 socketserver.handle_error，后者会往 stderr
        打一整段堆栈。刷新页面、切页、关窗口都会触发，日志里能攒下上千段，
        既盖住真正的错误，也白白写盘。这里只吞掉「连接没了」这一类，
        其它异常照旧往上抛，仍会被记下来。
        """
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def _origin_allowed(self) -> bool:
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True  # 本机 CLI / 测试客户端没有 Origin
        parsed = urlparse(origin)
        return (parsed.scheme in {"http", "https"}
                and parsed.netloc.lower() == (self.headers.get("Host") or "").lower())

    def _json(self, obj, status=200):
        # POST 成功 = 库或设置发生了变化。先推进版本再回响应，客户端收到
        # 自己的写入结果时，另一端通常已经在重绘了。
        if (self.command == "POST" and status < 400
                and not (isinstance(obj, dict) and obj.get("error"))):
            sync_notify((self.headers.get("X-GL-Client") or "").strip())
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self._lan_apply_cookie_headers(self.send_header)
        self.end_headers()
        self.wfile.write(body)

    def _post_route(self, fn, extra_args, path, qs, body):
        """POST 路由统一事务、内部异常和慢任务幂等，不改变返回结构。"""
        inflight = ""
        if is_slow_post_path(path):
            client = ((self.headers.get("X-GL-Client")
                       or (self.client_address[0] if self.client_address else "")
                       or "").strip())
            inflight = slow_request_key(path, qs, body, client)
            if not slow_request_enter(inflight):
                return self._json({"error": "这个任务还在处理中，请勿重复提交",
                                   "in_progress": True}, 409)
        conn = None
        try:
            conn = db()
            result = fn(conn, qs, body, *extra_args)
            if not (isinstance(result, dict) and result.get("error")):
                conn.commit()
            return self._json(result)
        except ValidationError as e:
            if conn is not None:
                conn.rollback()
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            if conn is not None:
                conn.rollback()
            return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
        finally:
            if conn is not None:
                conn.close()
            if inflight:
                slow_request_leave(inflight)

    def _client_is_local(self) -> bool:
        host = (self.client_address[0] if self.client_address else "").lower()
        return host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

    def _cookie_value(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        match = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", raw)
        return match.group(1).strip() if match else ""

    def _is_https_request(self) -> bool:
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return forwarded == "https" or bool(getattr(self.server, "ssl_context", None))

    def _lan_apply_cookie_headers(self, send_header) -> None:
        token = getattr(self, "_lan_new_session", "")
        if not token:
            return
        secure = "; Secure" if self._is_https_request() else ""
        send_header("Set-Cookie",
                    f"gl_session={token}; Path=/; Max-Age={LAN_SESSION_TTL}; "
                    f"HttpOnly; SameSite=Strict{secure}")
        send_header("Set-Cookie",
                    f"gl_key=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}")

    def _lan_check_code(self, given: str) -> bool:
        if not LAN_KEY or not given:
            return False
        # 非 ASCII 的 str 会让 compare_digest 抛 TypeError（中文访问码直接零响应），
        # 统一按 UTF-8 bytes 比较；失败也只返回 false，不再冒泡成断连。
        try:
            if not hmac.compare_digest(given.upper().encode("utf-8"),
                                       LAN_KEY.encode("utf-8")):
                return False
        except (TypeError, UnicodeError):
            return False
        ip = self.client_address[0] if self.client_address else "-"
        LAN_TRIES.pop(ip, None)
        _lan_rate_reset()
        return True

    def _lan_state(self, qs) -> str:
        """本机永远放行；手机端按会话令牌/访问码放行。

        主码不再作为长期 cookie；失败计数是全局的，带指数退避，重启后仍生效。
        """
        if not LAN_MODE or self._client_is_local():
            return "local"
        if not LAN_OPEN:
            return "off"
        ip = self.client_address[0] if self.client_address else "-"
        now = time.time()
        session = self._cookie_value("gl_session")
        if session and _lan_session_valid(session):
            LAN_TRIES.pop(ip, None)
            return "ok"
        if _lan_rate_blocked(now):
            return "need"
        tries = [t for t in LAN_TRIES.get(ip, []) if now - t < 3600]
        LAN_TRIES[ip] = tries
        if len(tries) >= 40:
            return "need"
        given = (qs.get("k") or [""])[0].strip()
        legacy = ""
        if not given:
            legacy = self._cookie_value("gl_key")
            given = legacy
        if not given:
            given = (self.headers.get("X-GL-Key") or "").strip()
        if self._lan_check_code(given):
            # 旧 cookie 自动轮换成 HttpOnly 随机会话令牌；主码不再长期留在浏览器里。
            if legacy and not session:
                self._lan_new_session = _lan_session_create()
                self._lan_clear_legacy = True
            return "ok"
        if given:
            LAN_TRIES.setdefault(ip, []).append(now)
            _lan_rate_record_failure()
        return "need"

    def _lan_login(self, error: str = ""):
        """没带码：给一页六位输入框的门。样式内联，不依赖任何静态资源。"""
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        error_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
        body = LAN_LOGIN_HTML.replace("<!--ERROR-->", error_html).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _lan_off(self):
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        body = LAN_OFF_HTML.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _lan_grant(self):
        """旧式 ?k= 链接仍兼容：换发会话令牌后跳到干净地址。"""
        self._lan_new_session = _lan_session_create()
        self._lan_clear_legacy = True
        self.send_response(302)
        self._lan_apply_cookie_headers(self.send_header)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _lan_login_post(self):
        """POST body 提交访问码；码不进 URL、浏览器历史或访问日志。"""
        if not LAN_MODE or not LAN_OPEN:
            return self._lan_off()
        if not self._origin_allowed():
            return self._json({"error": "拒绝跨站写入请求"}, 403)
        delay = _lan_rate_blocked()
        if delay:
            return self._lan_login(f"试得太频繁，请 {delay} 秒后再试")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Content-Length 无效"}, 400)
        if length < 0 or length > 4096:
            return self._json({"error": "登录请求体无效"}, 413)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        values = parse_qs(raw, keep_blank_values=True)
        given = (values.get("k") or [""])[0].strip().upper()
        if self._lan_check_code(given):
            self._lan_new_session = _lan_session_create()
            self._lan_clear_legacy = True
            self.send_response(302)
            self._lan_apply_cookie_headers(self.send_header)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        delay = _lan_rate_record_failure()
        message = "访问码不对，再试一次"
        if delay:
            message += f"（请 {delay} 秒后再试）"
        return self._lan_login(message)

    def _peer_authorized(self) -> bool:
        given = (self.headers.get("X-GL-Sync-Token") or "").strip()
        expected = peer().token
        return bool(given and expected and hmac.compare_digest(given, expected))

    def _peer_info(self):
        info = peer().host_refresh()
        info["service"] = "guanlan"
        info["protocol"] = 1
        return self._json(info)

    def _peer_snapshot_get(self):
        host = peer()
        meta = host.host_refresh()
        path = host.current_zip
        if not path.is_file():
            return self._json({"error": "电脑端还没有可用快照"}, 404)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-GL-Sync-Revision", str(meta.get("revision") or ""))
        self.send_header("X-GL-Sync-Snapshot", str(meta.get("snapshot_id") or ""))
        self.send_header("X-GL-Sync-Fingerprint", str(meta.get("fingerprint") or ""))
        self.end_headers()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _peer_submit(self, qs):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Content-Length 无效"}, 400)
        if length <= 0 or length > 300 * 1024 * 1024:
            return self._json({"error": "快照为空或超过 300MB"}, 413)
        base = int((qs.get("base_revision") or ["0"])[0] or 0)
        host = peer()
        host.sync_dir.mkdir(parents=True, exist_ok=True)
        temp = host.sync_dir / f"upload-{os.getpid()}-{time.time_ns()}.zip"
        received = 0
        try:
            with temp.open("wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(1024 * 1024, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
            if received != length:
                return self._json({"error": "快照上传中断"}, 400)
            status, result = host.host_submit(temp, base)
            return self._json(result, status)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
        finally:
            temp.unlink(missing_ok=True)

    # ── 云同步（WorkBuddy 云主机，协议与局域网对等同步一致） ──

    def _cloud_config_post(self, body: dict):
        data = DB_PATH.parent
        current = cloud_config_load(data, peer().token)
        endpoint = str(body.get("endpoint") or "").strip() or str(
            current.get("endpoint") or DEFAULT_CLOUD_ENDPOINT)
        key = str(body.get("key") or "").strip() or str(
            current.get("key") or DEFAULT_CLOUD_KEY)
        secret = str(current.get("secret") or "").strip()
        token = str(body.get("token") or "").strip() or str(current.get("token") or "")
        enabled = bool(body.get("enabled")) if "enabled" in body else bool(
            current.get("enabled", False))
        if "explicit_choice" in body:
            explicit_choice = bool(body.get("explicit_choice"))
        elif "enabled" in body:
            explicit_choice = True
        else:
            explicit_choice = bool(current.get("explicit_choice", False))
        try:
            interval = max(15, int(body.get("interval") or current.get("interval") or 20))
        except (TypeError, ValueError):
            interval = 20
        cfg = {
            "enabled": enabled,
            "explicit_choice": explicit_choice,
            "endpoint": endpoint,
            "key": key,
            "secret": secret,
            "token": token,
            "interval": interval,
        }
        cloud_config_save(data, cfg)
        cloud_peer().apply_config(cfg)
        # U5：后台兜底循环原先只在进程启动时拉起一次；启动那一刻配置是
        # 关闭的，之后在设置页打开就再也没人启动它。保存配置后补一次
        # （cloud_sync.start_auto_sync 自带防重复，重复保存不会多起线程）。
        start_cloud_sync_background()
        safe = {k: v for k, v in cfg.items() if k not in {"key", "secret", "token"}}
        safe["configured"] = bool(key and secret)
        return self._json({"ok": True, "config": safe})

    def _cloud_run_post(self):
        cp = cloud_peer()
        if not cp.config.get("enabled"):
            return self._json({"available": False, "reason": "云同步没开（设置页可打开）"})
        if cp.running():
            return self._json({"ok": True, "running": True})

        def run():
            try:
                cp.sync_once()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=run, name="guanlan-cloud-sync-manual", daemon=True).start()
        return self._json({"ok": True, "running": True})

    def _cloud_reset_post(self):
        cloud_peer().reset_state()
        return self._json({"ok": True})

    def _sse_write(self, event: str, data: dict) -> None:
        payload = (f"event: {event}\n"
                   f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n")
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _sync_stream(self):
        """一条长连接推送“库变了”的信号；内容仍由各页面按自己的路由重取。"""
        global SYNC_CLIENT_COUNT
        with SYNC_COND:
            if SYNC_CLIENT_COUNT >= SYNC_CLIENT_LIMIT:
                limited = True
            else:
                limited = False
                SYNC_CLIENT_COUNT += 1
                seen = SYNC_VERSION
                source = SYNC_SOURCE
        if limited:
            return self._json({"error": "实时同步连接数已满，请稍后重试"}, 503)

        self.close_connection = True
        try:
            self.send_response(200)
            self._lan_apply_cookie_headers(self.send_header)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self._sse_write("hello", {"version": seen, "source": source})
            while True:
                with SYNC_COND:
                    changed = SYNC_COND.wait_for(
                        lambda: SYNC_VERSION != seen, timeout=15)
                    version, source = SYNC_VERSION, SYNC_SOURCE
                if changed:
                    seen = version
                    self._sse_write("change", {"version": version, "source": source})
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with SYNC_COND:
                SYNC_CLIENT_COUNT = max(0, SYNC_CLIENT_COUNT - 1)

    def do_GET(self):  # noqa: N802
        CURRENT_REQUEST.local = self._client_is_local()
        self._lan_new_session = ""
        self._lan_clear_legacy = False
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 手机同步走独立的高强度配对令牌，不依赖网页访问码。
        if path.startswith("/api/peer/"):
            if not self._peer_authorized():
                return self._json({"error": "同步令牌无效"}, 403)
            if path == "/api/peer/info":
                return self._peer_info()
            if path == "/api/peer/snapshot":
                return self._peer_snapshot_get()
            return self._json({"error": f"未知同步接口 {path}"}, 404)

        state = self._lan_state(qs)
        if state == "off":
            if path.startswith("/api/"):
                return self._json({"error": "手机访问没开", "lan_off": True}, 403)
            return self._lan_off()
        if path == "/" or not path.startswith("/api/"):
            if path == "/":
                if state == "need":
                    return self._lan_login()
                if state == "ok" and (qs.get("k") or [""])[0]:
                    return self._lan_grant()
            if state == "need":
                # 访问码没通过时静态文件同样不放行；登录页自带内联样式，
                # 不依赖 /app.js、/index.html 这些资源。
                return self._lan_login()
            return self._static(path)
        if state == "need":
            return self._json({"error": "需要访问码", "need_key": True}, 401)
        if path == "/api/sync/peer":
            runner = globals().get("PEER_SYNC_RUNNER")
            if not runner:
                return self._json({"available": False, "reason": "当前设备没有局域网同步客户端"})
            return self._json(runner())
        if path == "/api/sync/peer/status":
            status_runner = globals().get("PEER_SYNC_STATUS")
            if not status_runner:
                return self._json({"available": False, "reason": "当前设备是同步主机"})
            return self._json(status_runner())
        if path == "/api/sync/cloud":
            return self._json(cloud_peer().cloud_status())
        if path == "/api/sync":
            return self._json({"version": sync_version()})
        if path == "/api/events":
            return self._sync_stream()
        file_match = re.fullmatch(r"/api/clips/(\d+)/file", path)
        if file_match:
            want_download = (qs.get("dl") or [""])[0] in {"1", "true", "yes"}
            return self._clip_file(int(file_match.group(1)), want_download)
        reading_media = re.fullmatch(r"/api/reading/media/(\d+)", path)
        if reading_media:
            return self._reading_media(int(reading_media.group(1)))
        note_media = re.fullmatch(r"/api/notes/img/([A-Za-z0-9._-]{1,80})", path)
        if note_media:
            want_download = (qs.get("dl") or [""])[0] in {"1", "true", "yes"}
            return self._note_media_get(note_media.group(1), want_download)
        body_media = re.fullmatch(r"/api/body/img/([A-Za-z0-9._-]{1,80})", path)
        if body_media:
            return self._body_media_get(body_media.group(1))
        if path in ROUTES_GET:
            conn = db()
            try:
                return self._json(ROUTES_GET[path](conn, qs))
            except ValidationError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
            finally:
                conn.close()
        # 参数化路由
        for pattern, fn in ROUTES_GET.items():
            if "{" not in pattern:
                continue
            arg = route_arg(pattern, path)
            if arg is None:
                continue
            conn = db()
            try:
                return self._json(fn(conn, qs, arg))
            except ValidationError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
            finally:
                conn.close()
        self._json({"error": f"未知接口 {path}"}, 404)

    def _note_media(self, qs):
        """随手记的附件：图片 / 音频 / 任意文件，原始二进制直落 data/notes。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Content-Length 无效"}, 400)
        if length <= 0:
            return self._json({"error": "文件是空的"}, 400)

        raw_name = safe_file_name((qs.get("name") or ["attachment"])[0])
        suffix = Path(raw_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix or ""):
            suffix = ".bin"                     # 没后缀或怪后缀，一律当二进制存
        if suffix in NOTE_IMAGE_EXT:
            kind, limit, label = "image", MAX_NOTE_IMAGE, "图片"
        elif suffix in NOTE_AUDIO_EXT:
            kind, limit, label = "audio", MAX_NOTE_AUDIO, "音频"
        else:
            kind, limit, label = "file", MAX_NOTE_FILE, "附件"
        if length > limit:
            return self._json({"error": f"单个{label}上限 {limit // (1024 * 1024)}MB"}, 413)

        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + os.urandom(8).hex() + suffix
        path = NOTES_DIR / name
        received = 0
        try:
            with path.open("wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(256 * 1024, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
        except Exception as e:  # noqa: BLE001
            path.unlink(missing_ok=True)
            print(f"[note-media] 写入失败 {raw_name}: {type(e).__name__}: {e}", flush=True)
            return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
        if received != length:
            path.unlink(missing_ok=True)
            return self._json({"error": f"上传中断：{received}/{length} 字节"}, 400)
        print(f"[note-media] {raw_name} · {length / 1024:.0f}KB · {name}", flush=True)
        return self._json({"ok": True, "file": name, "name": raw_name, "kind": kind,
                           "size": received, "url": "/api/notes/img/" + name})

    def _note_media_get(self, name: str, download: bool = False):
        if Path(name).name != name:
            return self._json({"error": "文件不存在"}, 404)
        target = (NOTES_DIR / name).resolve()
        if not target.is_relative_to(NOTES_DIR.resolve()) or not target.is_file():
            return self._json({"error": "文件不存在"}, 404)
        suffix = Path(name).suffix.lower()
        body = target.read_bytes()
        mime = (NOTE_MEDIA_MIME.get(suffix) or NOTE_TEXT_MIME.get(suffix)
                or mimetypes.guess_type(name)[0] or "application/octet-stream")
        # 浏览器能安全内联展示的：图片、音频、PDF、纯文本；其余一律当下载。
        # HTML 只在 sandbox CSP 下内联（禁脚本、唯一源、无外链），SVG / XML
        # 仍不进白名单，避免同源脚本执行。
        inline = suffix in NOTE_IMAGE_EXT or suffix in NOTE_AUDIO_EXT or suffix in {
            ".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv", ".log"}
        html_inline = (not download) and suffix in {".html", ".htm"}
        inline = inline or html_inline
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000")
        self.send_header("X-Content-Type-Options", "nosniff")
        if html_inline:
            self.send_header("Content-Security-Policy", HTML_SANDBOX_CSP)
        self.send_header("Content-Disposition",
                         ("attachment" if (download or not inline) else "inline")
                         + "; filename=download")
        self.end_headers()
        self.wfile.write(body)

    def _body_media_get(self, name: str):
        """正文配图：整块吐二进制，缓存一年——重抓时文件名带 cid 与哈希，不会撞。"""
        if Path(name).name != name:
            return self._json({"error": "文件不存在"}, 404)
        target = (BODY_DIR / name).resolve()
        if not target.is_relative_to(BODY_DIR.resolve()) or not target.is_file():
            return self._json({"error": "文件不存在"}, 404)
        body = target.read_bytes()
        mime = BODY_IMG_MIME.get(Path(name).suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _upload(self, qs):
        """原始二进制上传：不 base64、不进 JSON，边收边写盘。大文件靠这条。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Content-Length 无效"}, 400)
        name = safe_file_name((qs.get("name") or ["未命名文件"])[0])
        if length <= 0:
            return self._json({"error": "文件为空"}, 400)
        if length > MAX_UPLOAD:
            return self._json({"error": f"单文件上限 {MAX_UPLOAD // (1024 * 1024)}MB，"
                                       "要更大就设 GL_MAX_UPLOAD_MB"}, 413)

        tags_raw = (qs.get("tags") or [""])[0]
        board_raw = (qs.get("board") or [""])[0]
        tier_raw = (qs.get("tier") or [""])[0]
        grade_raw = (qs.get("grade") or [""])[0]
        mime = str(self.headers.get("Content-Type") or "")

        suffix = Path(name).suffix.lower()[:12]
        stored_name = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + os.urandom(8).hex() + suffix
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = UPLOAD_DIR / stored_name
        sha = hashlib.sha256()
        received = 0
        try:
            with path.open("wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(256 * 1024, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    sha.update(chunk)
                    received += len(chunk)
        except Exception as e:  # noqa: BLE001
            path.unlink(missing_ok=True)
            print(f"[upload] 写入失败 {name}: {type(e).__name__}: {e}", flush=True)
            return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
        if received != length:
            path.unlink(missing_ok=True)
            print(f"[upload] 中断 {name}: {received}/{length}", flush=True)
            return self._json({"error": f"上传中断：只收到 {received}/{length} 字节"}, 400)

        print(f"[upload] {name} · {length / 1048576:.1f}MB · {stored_name}", flush=True)
        conn = db()
        try:
            result = register_file(
                conn, name=name, mime=mime, stored_name=stored_name, path=path,
                size=received, digest="sha256:" + sha.hexdigest(),
                tags=[t.strip() for t in re.split(r"[,，]", tags_raw) if t.strip()],
                board_id=board_raw or None, tier=tier_raw, grade=grade_raw)
        except ValidationError as e:
            # 入库前的校验失败：原件还没被任何条目引用，删掉没问题
            path.unlink(missing_ok=True)
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            # 走到这里已经分不清条目有没有提交。宁可留一个孤儿文件，
            # 也不能把已入库条目的原件删掉（S1：响应序列化失败 → 原件蒸发）。
            print(f"[upload] 入库失败 {name}: {type(e).__name__}: {e}", flush=True)
            return self._json(internal_error_payload(e, f"{self.command} {self.path}"), 500)
        finally:
            conn.close()
        # 序列化在 try 之外：它失败不会回头去删原件
        return self._json(result)

    def do_POST(self):  # noqa: N802
        CURRENT_REQUEST.local = self._client_is_local()
        self._lan_new_session = ""
        self._lan_clear_legacy = False
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path.startswith("/api/peer/"):
            if not self._peer_authorized():
                return self._json({"error": "同步令牌无效"}, 403)
            if path == "/api/peer/submit":
                return self._peer_submit(qs)
            return self._json({"error": f"未知同步接口 {path}"}, 404)
        if path == "/api/lan/login":
            return self._lan_login_post()
        state = self._lan_state(qs)
        if state == "off":
            return self._json({"error": "手机访问没开", "lan_off": True}, 403)
        if state == "need":
            return self._json({"error": "需要访问码", "need_key": True}, 401)
        if not self._origin_allowed():
            return self._json({"error": "拒绝跨站写入请求"}, 403)
        if path == "/api/clips/upload":
            return self._upload(qs)
        if path in ("/api/notes/img", "/api/notes/media"):
            return self._note_media(qs)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Content-Length 无效"}, 400)
        if length < 0:
            return self._json({"error": "Content-Length 无效"}, 400)
        if length > 10 * 1024 * 1024:
            return self._json({"error": "请求体超过 10MB 上限"}, 413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json({"error": "请求体不是合法 UTF-8 JSON"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "JSON 请求体顶层必须是对象"}, 400)

        if path == "/api/sync/cloud":
            return self._cloud_config_post(body)
        if path == "/api/sync/cloud/run":
            return self._cloud_run_post()
        if path == "/api/sync/cloud/reset":
            return self._cloud_reset_post()

        if path in ROUTES_POST:
            return self._post_route(ROUTES_POST[path], (), path, qs, body)
        for pattern, fn in ROUTES_POST.items():
            if "{" not in pattern:
                continue
            arg = route_arg(pattern, path)
            if arg is None:
                continue
            return self._post_route(fn, (arg,), path, qs, body)
        self._json({"error": f"未知接口 {path}"}, 404)

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = {"html": "text/html; charset=utf-8",
                 "js": "application/javascript; charset=utf-8",
                 "mjs": "application/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "svg": "image/svg+xml",
                 "png": "image/png", "webmanifest": "application/manifest+json",
                 "ico": "image/x-icon"}.get(
                     target.suffix.lstrip("."), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self._lan_apply_cookie_headers(self.send_header)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _reading_media(self, cid: int):
        """阅读中心的音视频原件：支持 HTTP Range，浏览器才能拖动进度条。"""
        conn = db()
        try:
            row = conn.execute(
                "SELECT file_name,file_path,mime_type FROM clips WHERE id=?", (cid,)).fetchone()
        finally:
            conn.close()
        if not row or not row["file_path"] or Path(row["file_path"]).name != row["file_path"]:
            return self._json({"error": "文件不存在"}, 404)
        target = (UPLOAD_DIR / row["file_path"]).resolve()
        if not target.is_relative_to(UPLOAD_DIR.resolve()) or not target.is_file():
            return self._json({"error": "原件不存在"}, 404)
        import mimetypes
        suffix = target.suffix.lower()
        MIME_FIX = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
                    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
                    ".aac": "audio/aac", ".flac": "audio/flac", ".webm": "video/webm",
                    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}
        mime = MIME_FIX.get(suffix) or (mimetypes.guess_type(
            row["file_name"] or target.name)[0]) or "application/octet-stream"
        size = target.stat().st_size
        start, end, status = 0, max(0, size - 1), 200
        raw_range = (self.headers.get("Range") or "").strip()
        if raw_range:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "inline; filename=media")
        self.end_headers()
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass        # 拖动进度条时浏览器主动断开是常态

    def _clip_file(self, cid: int, download: bool = False):
        conn = db()
        try:
            row = conn.execute(
                "SELECT file_name,file_path,mime_type FROM clips WHERE id=?", (cid,)).fetchone()
        finally:
            conn.close()
        if not row or not row["file_path"] or Path(row["file_path"]).name != row["file_path"]:
            return self._json({"error": "文件不存在"}, 404)
        target = (UPLOAD_DIR / row["file_path"]).resolve()
        if not target.is_relative_to(UPLOAD_DIR.resolve()) or not target.is_file():
            return self._json({"error": "文件原件不存在"}, 404)
        body = target.read_bytes()
        # 存量库里可能存着客户端伪造的 mime（如把 .txt 标成 text/html），
        # 下载时也要按扩展名重算，不能只看表里那一个字段。
        mime = _server_mime(row["file_name"] or "", row["mime_type"] or "")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # PDF / 图片 / 纯文本直接在浏览器里打开；HTML 在 sandbox CSP 下内联直读，
        # 不再触发下载；svg 仍不进白名单（同源内联执行等于给 XSS 开门）。
        inline = mime in {
            "application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp",
            "text/plain", "text/markdown", "text/csv"}
        inline = inline or Path(row["file_name"] or "").suffix.lower() in {
            ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".md", ".csv"}
        html_inline = (not download) and Path(row["file_name"] or "").suffix.lower() in {
            ".html", ".htm"}
        if download:
            inline = False
        else:
            inline = inline or html_inline
            if html_inline:
                self.send_header("Content-Security-Policy", HTML_SANDBOX_CSP)
            elif mime.startswith("image/"):
                # 原件落盘后不会再改写，随机文件名也不会撞；缩略图可以直接吃缓存。
                self.send_header("Cache-Control", "private, max-age=31536000")
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment")
                         + "; filename=download; filename*=UTF-8''" + quote(row["file_name"] or "download"))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    ap = argparse.ArgumentParser(prog="guanlan", description="观澜 · 个人认知平台")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default=None)
    ap.add_argument("--lan", action="store_true",
                    help="允许同一局域网的手机访问（要输入六位访问码）")
    ap.add_argument("--seed", action="store_true", help="灌入示例数据后启动")
    args = ap.parse_args()

    storage.ensure_dirs()
    storage.prune_cache()        # 陈旧的渲染 profile 顺手扫掉，缓存不能只涨不消
    init_db()
    if args.seed or os.environ.get("GL_SEED"):
        import seed
        conn = db()
        try:
            seed.run(conn)
        finally:
            conn.close()

    env_port = os.environ.get("PORT")
    port = int(env_port) if env_port else args.port
    want_lan = bool(args.lan or os.environ.get("GL_LAN") in {"1", "true", "yes"})
    host = args.host or os.environ.get("GL_BIND") or (
        "0.0.0.0" if (env_port or want_lan) else "127.0.0.1")

    global LAN_MODE, LAN_KEY, LAN_PORT, LAN_IP, LAN_OPEN
    LAN_PORT = port
    LAN_MODE = host not in {"127.0.0.1", "localhost", "::1"}
    if LAN_MODE:
        LAN_KEY = lan_key()
        LAN_IP = lan_ip()
        LAN_OPEN = bool(lan_normalize_settings().get("enabled"))

    active_peer = peer()
    if LAN_MODE:
        active_peer.start_discovery_responder(port)
    start_cloud_sync_background()

    url = f"http://127.0.0.1:{port}/"
    print(f"观澜 GUANLAN · {url}")
    print(f"数据库   · {DB_PATH}")
    print(f"存储     · {storage.describe()}")
    ai = ai_config()
    print(f"AI       · {ai.get('config_error') or ('已配置 ' + ai['model'] if ai['base_url'] and ai['key'] and ai['model'] else '未配置（AI 页可看配置指引）')}")
    if LAN_MODE and LAN_IP and LAN_OPEN:
        print(f"手机访问 · http://{LAN_IP}:{port}/  访问码 {LAN_KEY}")
        print("           同一个 WiFi 才行；打不开多半是 Windows 防火墙拦了 python")
    elif LAN_MODE and LAN_IP:
        print(f"手机访问 · 端口开着但开关是关的（设置页里可以打开）· {LAN_IP}:{port}")
    elif not LAN_MODE:
        print("手机访问 · 关（要开就加 --lan 或用 GL_LAN=1 启动）")
    print("Ctrl+C 停止\n")

    try:
        # 显式关掉 SO_REUSEADDR：Windows 下它允许双绑定同一端口，
        # 两个进程同时 LISTEN、请求随机派发——"改了代码却不生效"的元凶。
        class Server(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True

        srv = Server((host, port), Handler)
    except OSError as e:
        if "in use" in str(e).lower() or getattr(e, "errno", None) in (48, 98, 10048):
            # 窗口不归这里管。以前这里 webbrowser.open(url)，启动器随后又开
            # 一个 Edge --app 窗口 —— 点一次出两个界面。界面统一由 launch.py 开。
            print(f"服务已在运行 · {url}（本次不重复启动，也不开窗）")
            return 3
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
