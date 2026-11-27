#!/usr/bin/env python
"""观澜 GUANLAN · 圣经阅读地（独立板块）服务端模块

与 server.py 的边界：
  * 本模块不 import server，避免 __main__ 双实例；依赖通过 install(**ctx) 注入。
  * 只注册 /api/bible/* 下的 JSON 路由，全部用 query string / JSON body 传参，
    不使用路径参数。
  * 自建一套 bible_* 表，不改动 clips / reading_* 等任何现有表。

装配方式（由 server.py 在路由表构建完毕后调用，与 reading.install 同构）::

    import bible
    bible.install(route_get, route_post, db=db, ai_chat=ai_chat, ai_config=ai_config)

数据说明：
  内置 DEMO_VERSES 仅为少量公版和合本（简体）选段，用于演示界面，
  明确不是完整圣经；完整经文请通过 POST /api/bible/import 自行导入。
  演示选段为便于阅读省略了和合本原有的译者括注。
"""
from __future__ import annotations

import functools
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

import app_paths

ROOT = app_paths.RES_DIR
SCHEMA_FILE = ROOT / "bible_schema.sql"

# 依赖由 server.py 注入
_CTX: dict = {}

# 子 logger 会向父 logger "guanlan" 传播，写进 server.app_logger() 的轮转文件；
# 不 import server，避免循环依赖与 __main__ 双实例。
_LOG = logging.getLogger("guanlan.bible")
# 服务刚启动时前端会并发打多个 /api/bible/*，用一把进程级锁把「建表 + 灌演示
# 数据」串行化，避免多连接同时做 DDL/INSERT 撞 SQLite 单写者。
_INIT_LOCK = threading.Lock()

DEMO_VERSION = "演示选段"
MAX_IMPORT = 50_000
MAX_AI_CHARS = 12_000

# ══════════════════════════════════════════════════════════
# 书卷元数据（66 卷，传统顺序）
# ══════════════════════════════════════════════════════════

# (USFM id, 简体名, 中文简称, 是否旧约, 章数)
_BOOK_ROWS = [
    ("gen", "创世记", "创", True, 50),
    ("exo", "出埃及记", "出", True, 40),
    ("lev", "利未记", "利", True, 27),
    ("num", "民数记", "民", True, 36),
    ("deu", "申命记", "申", True, 34),
    ("jos", "约书亚记", "书", True, 24),
    ("jdg", "士师记", "士", True, 21),
    ("rut", "路得记", "得", True, 4),
    ("1sa", "撒母耳记上", "撒上", True, 31),
    ("2sa", "撒母耳记下", "撒下", True, 24),
    ("1ki", "列王纪上", "王上", True, 22),
    ("2ki", "列王纪下", "王下", True, 25),
    ("1ch", "历代志上", "代上", True, 29),
    ("2ch", "历代志下", "代下", True, 36),
    ("ezr", "以斯拉记", "拉", True, 10),
    ("neh", "尼希米记", "尼", True, 13),
    ("est", "以斯帖记", "斯", True, 10),
    ("job", "约伯记", "伯", True, 42),
    ("psa", "诗篇", "诗", True, 150),
    ("pro", "箴言", "箴", True, 31),
    ("ecc", "传道书", "传", True, 12),
    ("sng", "雅歌", "歌", True, 8),
    ("isa", "以赛亚书", "赛", True, 66),
    ("jer", "耶利米书", "耶", True, 52),
    ("lam", "耶利米哀歌", "哀", True, 5),
    ("ezk", "以西结书", "结", True, 48),
    ("dan", "但以理书", "但", True, 12),
    ("hos", "何西阿书", "何", True, 14),
    ("jol", "约珥书", "珥", True, 3),
    ("amo", "阿摩司书", "摩", True, 9),
    ("oba", "俄巴底亚书", "俄", True, 1),
    ("jon", "约拿书", "拿", True, 4),
    ("mic", "弥迦书", "弥", True, 7),
    ("nam", "那鸿书", "鸿", True, 3),
    ("hab", "哈巴谷书", "哈", True, 3),
    ("zep", "西番雅书", "番", True, 3),
    ("hag", "哈该书", "该", True, 2),
    ("zec", "撒迦利亚书", "亚", True, 14),
    ("mal", "玛拉基书", "玛", True, 4),
    ("mat", "马太福音", "太", False, 28),
    ("mrk", "马可福音", "可", False, 16),
    ("luk", "路加福音", "路", False, 24),
    ("jhn", "约翰福音", "约", False, 21),
    ("act", "使徒行传", "徒", False, 28),
    ("rom", "罗马书", "罗", False, 16),
    ("1co", "哥林多前书", "林前", False, 16),
    ("2co", "哥林多后书", "林后", False, 13),
    ("gal", "加拉太书", "加", False, 6),
    ("eph", "以弗所书", "弗", False, 6),
    ("php", "腓立比书", "腓", False, 4),
    ("col", "歌罗西书", "西", False, 4),
    ("1th", "帖撒罗尼迦前书", "帖前", False, 5),
    ("2th", "帖撒罗尼迦后书", "帖后", False, 3),
    ("1ti", "提摩太前书", "提前", False, 6),
    ("2ti", "提摩太后书", "提后", False, 4),
    ("tit", "提多书", "多", False, 3),
    ("phm", "腓利门书", "门", False, 1),
    ("heb", "希伯来书", "来", False, 13),
    ("jas", "雅各书", "雅", False, 5),
    ("1pe", "彼得前书", "彼前", False, 5),
    ("2pe", "彼得后书", "彼后", False, 3),
    ("1jn", "约翰一书", "约一", False, 5),
    ("2jn", "约翰二书", "约二", False, 1),
    ("3jn", "约翰三书", "约三", False, 1),
    ("jud", "犹大书", "犹", False, 1),
    ("rev", "启示录", "启", False, 22),
]

BOOKS = [
    {"id": bid, "name": name, "abbr": abbr, "ot": ot,
     "chapters": chapters, "order": i}
    for i, (bid, name, abbr, ot, chapters) in enumerate(_BOOK_ROWS, start=1)
]
assert len(BOOKS) == 66, f"书卷数应为 66，实际 {len(BOOKS)}"
TOTAL_CHAPTERS = sum(b["chapters"] for b in BOOKS)
assert TOTAL_CHAPTERS == 1189, f"总章数应为 1189，实际 {TOTAL_CHAPTERS}"

BOOK_INDEX = {b["id"]: b for b in BOOKS}
BOOK_POS = {b["id"]: i for i, b in enumerate(BOOKS)}

# 常见繁体卷名（与 _BOOK_ROWS 一一对应）
_TRAD_NAMES = [
    "創世記", "出埃及記", "利未記", "民數記", "申命記", "約書亞記", "士師記",
    "路得記", "撒母耳記上", "撒母耳記下", "列王紀上", "列王紀下", "歷代志上",
    "歷代志下", "以斯拉記", "尼希米記", "以斯帖記", "約伯記", "詩篇", "箴言",
    "傳道書", "雅歌", "以賽亞書", "耶利米書", "耶利米哀歌", "以西結書",
    "但以理書", "何西阿書", "約珥書", "阿摩司書", "俄巴底亞書", "約拿書",
    "彌迦書", "那鴻書", "哈巴谷書", "西番雅書", "哈該書", "撒迦利亞書",
    "瑪拉基書", "馬太福音", "馬可福音", "路加福音", "約翰福音", "使徒行傳",
    "羅馬書", "哥林多前書", "哥林多後書", "加拉太書", "以弗所書", "腓立比書",
    "歌羅西書", "帖撒羅尼迦前書", "帖撒羅尼迦後書", "提摩太前書", "提摩太後書",
    "提多書", "腓利門書", "希伯來書", "雅各書", "彼得前書", "彼得後書",
    "約翰一書", "約翰二書", "約翰三書", "猶大書", "啟示錄",
]

# 英文全名
_EN_NAMES = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua",
    "Judges", "Ruth", "1 Samuel", "2 Samuel", "1 Kings", "2 Kings",
    "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah", "Esther", "Job",
    "Psalms", "Proverbs", "Ecclesiastes", "Song of Songs", "Isaiah", "Jeremiah",
    "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos", "Obadiah",
    "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah", "Haggai", "Zechariah",
    "Malachi", "Matthew", "Mark", "Luke", "John", "Acts", "Romans",
    "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians", "Philippians",
    "Colossians", "1 Thessalonians", "2 Thessalonians", "1 Timothy", "2 Timothy",
    "Titus", "Philemon", "Hebrews", "James", "1 Peter", "2 Peter", "1 John",
    "2 John", "3 John", "Jude", "Revelation",
]

# 英文缩写（多数与 USFM id 同形，显式登记便于大小写/空格归一化后命中）
_EN_ABBR = [
    "Gen", "Exo", "Lev", "Num", "Deu", "Jos", "Jdg", "Rut", "1Sa", "2Sa",
    "1Ki", "2Ki", "1Ch", "2Ch", "Ezr", "Neh", "Est", "Job", "Psa", "Pro",
    "Ecc", "Sng", "Isa", "Jer", "Lam", "Ezk", "Dan", "Hos", "Jol", "Amo",
    "Oba", "Jon", "Mic", "Nam", "Hab", "Zep", "Hag", "Zec", "Mal", "Mat",
    "Mrk", "Luk", "Jhn", "Act", "Rom", "1Co", "2Co", "Gal", "Eph", "Php",
    "Col", "1Th", "2Th", "1Ti", "2Ti", "Tit", "Phm", "Heb", "Jas", "1Pe",
    "2Pe", "1Jn", "2Jn", "3Jn", "Jud", "Rev",
]

assert len(_TRAD_NAMES) == len(_EN_NAMES) == len(_EN_ABBR) == len(BOOKS) == 66

# 额外别名：常见异体、误写、数字前缀写法、罗马数字写法
_EXTRA_ALIASES = {
    "创世纪": "gen", "創世紀": "gen", "Genesis": "gen",
    "列王记上": "1ki", "列王记下": "2ki",
    "歷代誌上": "1ch", "歷代誌下": "2ch",
    "历代誌上": "1ch", "历代誌下": "2ch",
    "撒迦利书": "zec", "撒迦利亚": "zec", "撒迦利亞": "zec",
    "约翰壹书": "1jn", "约翰贰书": "2jn", "约翰叁书": "3jn",
    "約翰壹書": "1jn", "約翰貳書": "2jn", "約翰叁書": "3jn",
    "約翰福音": "jhn", "啟示錄": "rev", "馬太福音": "mat", "詩篇": "psa",
    "诗篇": "psa", "Psalm": "psa", "Ps": "psa", "Psalms": "psa",
    "Song of Solomon": "sng", "Canticles": "sng", "Song": "sng",
    "Eze": "ezk", "Ezek": "ezk", "Nah": "nam", "Phil": "php",
    "Mt": "mat", "Mk": "mrk", "Lk": "luk", "Jn": "jhn",
    "Acts of the Apostles": "act", "Apocalypse": "rev",
    "1Sam": "1sa", "2Sam": "2sa", "1Kgs": "1ki", "2Kgs": "2ki",
    "1Chr": "1ch", "2Chr": "2ch", "1Cor": "1co", "2Cor": "2co",
    "1Thess": "1th", "2Thess": "2th", "1Tim": "1ti", "2Tim": "2ti",
    "1Pet": "1pe", "2Pet": "2pe",
    "1st Samuel": "1sa", "2nd Samuel": "2sa", "I Samuel": "1sa", "II Samuel": "2sa",
    "1st Kings": "1ki", "2nd Kings": "2ki", "I Kings": "1ki", "II Kings": "2ki",
    "1st Chronicles": "1ch", "2nd Chronicles": "2ch",
    "I Chronicles": "1ch", "II Chronicles": "2ch",
    "1st Corinthians": "1co", "2nd Corinthians": "2co",
    "I Corinthians": "1co", "II Corinthians": "2co",
    "1st Thessalonians": "1th", "2nd Thessalonians": "2th",
    "I Thessalonians": "1th", "II Thessalonians": "2th",
    "1st Timothy": "1ti", "2nd Timothy": "2ti", "I Timothy": "1ti", "II Timothy": "2ti",
    "1st Peter": "1pe", "2nd Peter": "2pe", "I Peter": "1pe", "II Peter": "2pe",
    "1st John": "1jn", "2nd John": "2jn", "3rd John": "3jn",
    "I John": "1jn", "II John": "2jn", "III John": "3jn",
}

# 归一化后的别名表：key -> USFM id
ALIASES: dict[str, str] = {}


def _norm_key(s) -> str:
    """归一化别名：小写、去空白、去常见标点与引号。"""
    s = str(s or "").strip().lower()
    return re.sub(r"[\s._\-·•,，、/\\()（）\[\]【】:：;；'\"“”‘’「」『』《》]+", "", s)


def _register(keys, bid: str) -> None:
    for k in keys:
        key = _norm_key(k)
        if not key:
            continue
        # 已有映射且指向别卷时保留先登记者，避免歧义被后来的别名覆盖
        if key in ALIASES and ALIASES[key] != bid:
            continue
        ALIASES[key] = bid


for _b in BOOKS:
    _register((_b["id"], _b["name"], _b["abbr"]), _b["id"])
for _row, _trad, _en, _abbr in zip(_BOOK_ROWS, _TRAD_NAMES, _EN_NAMES, _EN_ABBR):
    _register((_trad, _en, _abbr), _row[0])
for _k, _v in _EXTRA_ALIASES.items():
    _register((_k,), _v)


def normalize_book(name) -> str | None:
    """把简体名 / 繁体名 / 英文缩写 / 中文简称 / USFM id 兜底归一成书卷 id。"""
    key = _norm_key(name)
    if not key:
        return None
    if key in ALIASES:
        return ALIASES[key]
    if key in BOOK_INDEX:
        return key
    # 兜底：唯一前缀匹配（如「创世」「马太」）。候选不唯一则不猜，避免「约」这类歧义。
    if len(key) >= 2:
        cand = {bid for alias, bid in ALIASES.items() if alias.startswith(key)}
        if len(cand) == 1:
            return next(iter(cand))
    return None


# ══════════════════════════════════════════════════════════
# 演示经文（公版和合本简体选段，仅用于演示，不是完整圣经）
# ══════════════════════════════════════════════════════════

DEMO_VERSES = [
    ("gen", 1, 1, "起初，神创造天地。"),
    ("gen", 1, 2, "地是空虚混沌，渊面黑暗；神的灵运行在水面上。"),
    ("gen", 1, 3, "神说：「要有光」，就有了光。"),
    ("gen", 1, 4, "神看光是好的，就把光暗分开了。"),
    ("gen", 1, 5, "神称光为「昼」，称暗为「夜」。有晚上，有早晨，这是头一日。"),

    ("psa", 23, 1, "（大卫的诗。）耶和华是我的牧者，我必不致缺乏。"),
    ("psa", 23, 2, "他使我躺卧在青草地上，领我在可安歇的水边。"),
    ("psa", 23, 3, "他使我的灵魂苏醒，为自己的名引导我走义路。"),
    ("psa", 23, 4, "我虽然行过死荫的幽谷，也不怕遭害，因为你与我同在；你的杖、你的竿，都安慰我。"),
    ("psa", 23, 5, "在我敌人面前，你为我摆设筵席；你用油膏了我的头，使我的福杯满溢。"),
    ("psa", 23, 6, "我一生一世必有恩惠慈爱随着我；我且要住在耶和华的殿中，直到永远。"),

    ("jhn", 1, 1, "太初有道，道与神同在，道就是神。"),
    ("jhn", 1, 2, "这道太初与神同在。"),
    ("jhn", 1, 3, "万物是藉着他造的；凡被造的，没有一样不是藉着他造的。"),
    ("jhn", 1, 4, "生命在他里头，这生命就是人的光。"),
    ("jhn", 1, 5, "光照在黑暗里，黑暗却不接受光。"),
    ("jhn", 3, 16, "神爱世人，甚至将他的独生子赐给他们，叫一切信他的，不至灭亡，反得永生。"),

    ("pro", 3, 5, "你要专心仰赖耶和华，不可倚靠自己的聪明，"),
    ("pro", 3, 6, "在你一切所行的事上都要认定他，他必指引你的路。"),

    ("mat", 6, 9, "所以，你们祷告要这样说：我们在天上的父，愿人都尊你的名为圣。"),
    ("mat", 6, 10, "愿你的国降临；愿你的旨意行在地上，如同行在天上。"),
    ("mat", 6, 11, "我们日用的饮食，今日赐给我们。"),
    ("mat", 6, 12, "免我们的债，如同我们免了人的债。"),
    ("mat", 6, 13, "不叫我们遇见试探；救我们脱离凶恶。因为国度、权柄、荣耀，全是你的，直到永远。阿们！"),
]


# ══════════════════════════════════════════════════════════
# 装配 / 通用工具
# ══════════════════════════════════════════════════════════

def install(route_get, route_post, **ctx) -> None:
    """由 server.py 在路由表构建完毕后调用，注入依赖并注册路由。

    可选 ctx：``ensure_logger`` / ``app_logger``（callable）。server.app_logger()
    是懒加载的，冷启动时 ``guanlan`` logger 还没有 handler，本模块的
    ``_LOG.exception`` 只能落到 logging.lastResort（stderr）。装配时若注入该
    回调就预热一次，第一条 bible 异常即可写进 data/app.log 的轮转 handler。
    不注入则完全保持现状，不产生任何额外副作用。
    """
    global _CTX
    _CTX = ctx
    _warm_logger()
    for path, fn in GET_ROUTES.items():
        route_get(path)(fn)
    for path, fn in POST_ROUTES.items():
        route_post(path)(fn)


def _warm_logger() -> None:
    """只调用装配方注入的初始化回调；回调缺失或失败都不影响路由注册。"""
    for key in ("ensure_logger", "app_logger"):
        fn = _CTX.get(key)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001 - 日志预热失败不能拖垮整块装配
                _LOG.warning("bible 日志预热失败", exc_info=True)
            return


def ensure(conn) -> None:
    """幂等建表 + 首次灌入演示数据。

    每个 API 开头都调用；不依赖进程级全局标志，避免因 DB_PATH 切换或多进程
    导致新库没被初始化。
    事务边界：server.py 的 GET 分发只 close 不 commit，POST 由 _post_route
    统一 commit / rollback。因此这里只提交「建表 + 演示数据」这一初始化部分，
    POST 的业务写入刻意不在此提交，让异常能被 _post_route 完整回滚。
    稳态（表已建、demo 已插）不做任何写操作也不 commit；初始化段用
    _INIT_LOCK 串行，避免冷启动并发请求同时做 DDL。
    """
    with _INIT_LOCK:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('bible_verses','bible_meta')").fetchall()
        names = {r["name"] for r in rows}
        changed = False
        if not {"bible_verses", "bible_meta"} <= names:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            changed = True
        # 老库原地补列：中英对照导入需要 text_en，已有库不重建、不动数据。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bible_verses)")}
        if "text_en" not in cols:
            conn.execute(
                "ALTER TABLE bible_verses ADD COLUMN text_en TEXT NOT NULL DEFAULT ''")
            changed = True
        seeded = conn.execute("SELECT value FROM bible_meta WHERE key='demo'").fetchone()
        if not seeded:
            _seed_demo(conn)
            conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('demo','1')")
            conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('version',?)",
                         (DEMO_VERSION,))
            changed = True
        if changed:
            conn.commit()  # 初始化专用：GET 分发不会替我们提交


def _seed_demo(conn) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO bible_verses(book,chapter,verse,text,version) "
        "VALUES(?,?,?,?,'demo')", DEMO_VERSES)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _err(msg: str) -> dict:
    return {"error": msg}


def _jload(raw, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except (ValueError, TypeError):
        return fallback


def _int(v, default=0, lo=None, hi=None) -> int:
    """安全转 int。注意 lo/hi 也会作用在解析成功的值上（含 0），
    所以「0 表示无 id」的字段不要传 lo=1，改用调用处显式判断。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on", "是"}


def qs_get(qs, key, default=""):
    """parse_qs 的值可能是 list，这里统一取成标量。"""
    if not isinstance(qs, dict):
        return default
    v = qs.get(key)
    if v is None:
        return default
    if isinstance(v, (list, tuple)):
        v = v[0] if v else default
    return default if v is None else v


def body_get(body, key, default=""):
    """安全取 JSON body 字段，保留原始类型（bool/int/list）。"""
    if not isinstance(body, dict):
        return default
    v = body.get(key, default)
    return default if v is None else v


def _guard(fn):
    """每个 handler 的事务清理 + 日志兜底，异常原样交给 server 统一处理。

    刻意不把异常在本模块转成 {"error": str(exc)}：server 的 _post_route /
    do_GET 已有统一的 except Exception -> 500 + 随机 ref + app.log 堆栈，
    而 GET 分发只会把返回值按 200 发出去。若在这里吞掉异常，内部路径、
    SQL 片段会原样回显给浏览器、伪装成 HTTP 200（前端按状态码判断/重试
    会误判），且生产故障零日志零 ref。
    业务校验失败仍走各 handler 主动 return _err(...) 的正常路径，不抛异常。
    这里只负责 rollback + 模块级日志，然后 re-raise；异常不会逃逸出 server
    的请求处理链。
    """
    @functools.wraps(fn)
    def wrapped(conn, qs, *args):
        try:
            return fn(conn, qs, *args)
        except Exception:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:
                pass
            _LOG.exception("bible handler %s 内部异常", getattr(fn, "__name__", "?"))
            raise
    return wrapped


def _resolve_book(raw) -> str | None:
    key = _norm_key(raw)
    if key in BOOK_INDEX:
        return key
    return normalize_book(raw)


def _ref(book_raw, chapter_raw, verse_raw):
    """校验并返回 (book, chapter, verse)；任一不合法返回 None。"""
    bid = _resolve_book(book_raw)
    if not bid:
        return None
    b = BOOK_INDEX[bid]
    ch = _int(chapter_raw, 1)
    vs = _int(verse_raw, 1)
    if not (1 <= ch <= b["chapters"]) or vs < 1:
        return None
    return bid, ch, vs


def _book_name(bid) -> str:
    b = BOOK_INDEX.get(bid)
    return b["name"] if b else str(bid or "")


def _label(bid, chapter, verse) -> str:
    return f"{_book_name(bid)} {chapter}:{verse}"


def _meta(conn) -> dict:
    return {r["key"]: (r["value"] or "") for r in
            conn.execute("SELECT key,value FROM bible_meta")}


def _progress(conn) -> dict:
    row = conn.execute("SELECT * FROM bible_progress WHERE id=1").fetchone()
    if not row:
        return {"book": BOOKS[0]["id"], "chapter": 1, "verse": 1,
                "read": [], "updated_at": ""}
    read = _jload(row["read_json"], [])
    if not isinstance(read, list):
        read = []
    return {"book": row["book"] or BOOKS[0]["id"],
            "chapter": _int(row["chapter"], 1, 1),
            "verse": _int(row["verse"], 1, 1),
            "read": read, "updated_at": row["updated_at"] or ""}


def _stats(conn) -> dict:
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM bible_verses) verses,
                  (SELECT COUNT(*) FROM bible_highlights) highlights,
                  (SELECT COUNT(*) FROM bible_notes) notes,
                  (SELECT COUNT(*) FROM bible_bookmarks) bookmarks""").fetchone()
    return {"verses": row["verses"], "highlights": row["highlights"],
            "notes": row["notes"], "bookmarks": row["bookmarks"],
            "read": len(_progress(conn)["read"]),
            "total_chapters": TOTAL_CHAPTERS}


def _neighbors(bid, chapter):
    """按传统书卷顺序给出上一章 / 下一章，跨卷自动衔接。"""
    b = BOOK_INDEX.get(bid)
    if not b:
        return None, None
    pos = BOOK_POS[bid]
    if chapter > 1:
        prev = {"book": bid, "chapter": chapter - 1}
    elif pos > 0:
        pb = BOOKS[pos - 1]
        prev = {"book": pb["id"], "chapter": pb["chapters"]}
    else:
        prev = None
    if chapter < b["chapters"]:
        nxt = {"book": bid, "chapter": chapter + 1}
    elif pos < len(BOOKS) - 1:
        nxt = {"book": BOOKS[pos + 1]["id"], "chapter": 1}
    else:
        nxt = None
    return prev, nxt


def _row_ref(r) -> tuple:
    d = dict(r)
    return (BOOK_POS.get(d.get("book"), 9999), _int(d.get("chapter"), 0),
            _int(d.get("verse"), 0), _int(d.get("id"), 0))


def _like(q: str) -> str:
    """LIKE 模式，转义 % / _ / \\ 本身，避免用户输入变成通配符。"""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + esc + "%"


def _snippet(text: str, q: str) -> str:
    """命中词前后各约 30 字。"""
    if not text:
        return ""
    idx = text.lower().find(q.lower())
    if idx < 0:
        return text[:60] + ("…" if len(text) > 60 else "")
    start = max(0, idx - 30)
    end = min(len(text), idx + len(q) + 30)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


# ══════════════════════════════════════════════════════════
# GET
# ══════════════════════════════════════════════════════════

def api_bible_state(conn, qs):
    ensure(conn)
    meta = _meta(conn)
    version = meta.get("version") or DEMO_VERSION
    demo = (meta.get("demo") or "1") == "1"
    return {"ok": True, "books": BOOKS, "progress": _progress(conn),
            "stats": _stats(conn), "version": version, "demo": demo}


def api_bible_chapter(conn, qs):
    ensure(conn)
    raw_book = qs_get(qs, "book", "").strip()
    if not raw_book:
        raw_book = _progress(conn)["book"]
    bid = _resolve_book(raw_book)
    if not bid:
        return _err("书卷无效")
    b = BOOK_INDEX[bid]
    chapter = _int(qs_get(qs, "chapter", "1"), 1)
    if not (1 <= chapter <= b["chapters"]):
        return _err(f"章节超出范围（{b['name']} 共 {b['chapters']} 章）")

    verses = [{"verse": r["verse"], "text": r["text"],
               "text_en": r["text_en"] or ""} for r in conn.execute(
        "SELECT verse,text,text_en FROM bible_verses "
        "WHERE book=? AND chapter=? ORDER BY verse", (bid, chapter))]
    highlights = [{"id": r["id"], "verse": r["verse"], "start": r["start"],
                   "end": r["end"], "quote": r["quote"], "color": r["color"],
                   "note": r["note"]} for r in conn.execute(
        "SELECT id,verse,start,end,quote,color,note FROM bible_highlights "
        "WHERE book=? AND chapter=? ORDER BY verse,start,id", (bid, chapter))]
    bookmarks = [{"id": r["id"], "verse": r["verse"], "label": r["label"]}
                 for r in conn.execute(
        "SELECT id,verse,label FROM bible_bookmarks "
        "WHERE book=? AND chapter=? ORDER BY verse,id", (bid, chapter))]
    notes = [{"id": r["id"], "verse": r["verse"], "title": r["title"],
              "content_md": r["content_md"]} for r in conn.execute(
        "SELECT id,verse,title,content_md FROM bible_notes "
        "WHERE book=? AND chapter=? ORDER BY verse,id", (bid, chapter))]
    prev, nxt = _neighbors(bid, chapter)
    return {"ok": True, "book": bid, "book_name": b["name"], "chapter": chapter,
            "chapter_count": b["chapters"], "verses": verses,
            "highlights": highlights, "bookmarks": bookmarks, "notes": notes,
            "prev": prev, "next": nxt}


def api_bible_search(conn, qs):
    ensure(conn)
    q = str(qs_get(qs, "q", "")).strip()
    if not q:
        return {"ok": True, "hits": [], "total": 0}
    like = _like(q)
    total = conn.execute(
        "SELECT COUNT(*) n FROM bible_verses "
        "WHERE text LIKE ? ESCAPE '\\' OR text_en LIKE ? ESCAPE '\\'",
        (like, like)).fetchone()["n"]
    order_case = "CASE book " + " ".join(
        f"WHEN ? THEN {i}" for i in range(len(BOOKS))) + " ELSE 9999 END"
    sql = ("SELECT book,chapter,verse,text,text_en FROM bible_verses "
           "WHERE text LIKE ? ESCAPE '\\' OR text_en LIKE ? ESCAPE '\\' ORDER BY "
           + order_case + ", chapter, verse LIMIT 60")
    rows = conn.execute(sql, (like, like, *[b["id"] for b in BOOKS])).fetchall()
    hits = [{"book": r["book"], "book_name": _book_name(r["book"]),
             "chapter": r["chapter"], "verse": r["verse"], "text": r["text"],
             "text_en": r["text_en"] or "",
             "snippet": _snippet(r["text"] or r["text_en"], q)} for r in rows]
    return {"ok": True, "hits": hits, "total": total}


def api_bible_highlights(conn, qs):
    ensure(conn)
    rows = conn.execute("SELECT * FROM bible_highlights").fetchall()
    return {"ok": True, "highlights": [dict(r) for r in sorted(rows, key=_row_ref)]}


def api_bible_notes(conn, qs):
    ensure(conn)
    rows = conn.execute("SELECT * FROM bible_notes").fetchall()
    return {"ok": True, "notes": [dict(r) for r in sorted(rows, key=_row_ref)]}


def api_bible_export(conn, qs):
    ensure(conn)
    meta = _meta(conn)
    version = meta.get("version") or DEMO_VERSION
    hls = [dict(r) for r in sorted(
        conn.execute("SELECT * FROM bible_highlights").fetchall(), key=_row_ref)]
    notes = [dict(r) for r in sorted(
        conn.execute("SELECT * FROM bible_notes").fetchall(), key=_row_ref)]

    lines = ["# 观澜 · 圣经阅读地导出", "",
             f"> 译本：{version}", f"> 导出时间：{_now()}", ""]
    lines.append(f"## 高亮标注（{len(hls)} 条）")
    lines.append("")
    if not hls:
        lines.append("（无）")
    for h in hls:
        lines.append(f"### {_label(h['book'], h['chapter'], h['verse'])}")
        lines.append("")
        lines.append(f"- 颜色：{h.get('color') or ''}")
        if h.get("quote"):
            lines.append(f"- 引文：{h['quote']}")
        if h.get("note"):
            lines.append(f"- 批注：{h['note']}")
        lines.append("")
    lines.append(f"## 笔记（{len(notes)} 条）")
    lines.append("")
    if not notes:
        lines.append("（无）")
    for n in notes:
        lines.append(f"### {n.get('title') or '无标题'}"
                     f"（{_label(n['book'], n['chapter'], n['verse'])}）")
        lines.append("")
        lines.append(n.get("content_md") or "")
        lines.append("")
    return {"ok": True, "markdown": "\n".join(lines)}


# ══════════════════════════════════════════════════════════
# POST
# ══════════════════════════════════════════════════════════

def api_bible_progress(conn, qs, body):
    ensure(conn)
    ref = _ref(body_get(body, "book"), body_get(body, "chapter", 1),
               body_get(body, "verse", 1))
    if not ref:
        return _err("书卷或章节参数无效")
    bid, chapter, verse = ref
    prog = _progress(conn)
    read = list(prog["read"])
    if _bool(body_get(body, "done", False)):
        key = f"{bid}:{chapter}"
        if key not in read:
            read.append(key)
    now = _now()
    conn.execute(
        """INSERT INTO bible_progress(id,book,chapter,verse,read_json,updated_at)
           VALUES(1,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET book=excluded.book, chapter=excluded.chapter,
             verse=excluded.verse, read_json=excluded.read_json,
             updated_at=excluded.updated_at""",
        (bid, chapter, verse, json.dumps(read, ensure_ascii=False), now))
    return {"ok": True, "progress": _progress(conn)}


def api_bible_highlight(conn, qs, body):
    ensure(conn)
    ref = _ref(body_get(body, "book"), body_get(body, "chapter", 1),
               body_get(body, "verse", 1))
    if not ref:
        return _err("书卷或章节参数无效")
    bid, chapter, verse = ref
    start = _int(body_get(body, "start", 0), 0, 0)
    end = _int(body_get(body, "end", 0), 0, 0)
    quote = str(body_get(body, "quote", ""))[:4000]
    color = str(body_get(body, "color", "yellow")).strip()[:32] or "yellow"
    note = str(body_get(body, "note", ""))[:8000]
    cur = conn.execute(
        """INSERT INTO bible_highlights
           (book,chapter,verse,start,end,quote,color,note,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (bid, chapter, verse, start, end, quote, color, note, _now()))
    row = conn.execute("SELECT * FROM bible_highlights WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    return {"ok": True, "highlight": dict(row)}


def api_bible_highlight_delete(conn, qs, body):
    ensure(conn)
    hid = _int(body_get(body, "id", 0), 0)
    if hid < 1:
        return _err("id 无效")
    conn.execute("DELETE FROM bible_highlights WHERE id=?", (hid,))
    return {"ok": True}


def api_bible_bookmark(conn, qs, body):
    """同一 (book,chapter,verse) 已存在则删除（切换），否则新增。"""
    ensure(conn)
    ref = _ref(body_get(body, "book"), body_get(body, "chapter", 1),
               body_get(body, "verse", 1))
    if not ref:
        return _err("书卷或章节参数无效")
    bid, chapter, verse = ref
    old = conn.execute(
        "SELECT id FROM bible_bookmarks WHERE book=? AND chapter=? AND verse=? "
        "ORDER BY id LIMIT 1", (bid, chapter, verse)).fetchone()
    if old:
        conn.execute("DELETE FROM bible_bookmarks WHERE id=?", (old["id"],))
        return {"ok": True, "bookmark": None, "removed": True}
    label = str(body_get(body, "label", "书签")).strip()[:80] or "书签"
    cur = conn.execute(
        """INSERT INTO bible_bookmarks(book,chapter,verse,label,created_at)
           VALUES(?,?,?,?,?)""", (bid, chapter, verse, label, _now()))
    row = conn.execute("SELECT * FROM bible_bookmarks WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    return {"ok": True, "bookmark": dict(row), "removed": False}


def api_bible_bookmark_delete(conn, qs, body):
    ensure(conn)
    rowid = _int(body_get(body, "id", 0), 0)
    if rowid < 1:
        return _err("id 无效")
    conn.execute("DELETE FROM bible_bookmarks WHERE id=?", (rowid,))
    return {"ok": True}


def api_bible_note(conn, qs, body):
    ensure(conn)
    ref = _ref(body_get(body, "book"), body_get(body, "chapter", 1),
               body_get(body, "verse", 1))
    if not ref:
        return _err("书卷或章节参数无效")
    book, chapter, verse = ref
    title = str(body_get(body, "title", "")).strip()[:160]
    content = str(body_get(body, "content_md", ""))[:60_000]
    nid = _int(body_get(body, "id", 0), 0)
    if nid > 0:
        exists = conn.execute("SELECT id FROM bible_notes WHERE id=?", (nid,)).fetchone()
        if not exists:
            return _err("这条笔记不存在")
        conn.execute(
            """UPDATE bible_notes SET book=?,chapter=?,verse=?,title=?,content_md=?,
               updated_at=? WHERE id=?""",
            (book, chapter, verse, title, content, _now(), nid))
    else:
        now = _now()
        cur = conn.execute(
            """INSERT INTO bible_notes
               (book,chapter,verse,title,content_md,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (book, chapter, verse, title, content, now, now))
        nid = cur.lastrowid
    row = conn.execute("SELECT * FROM bible_notes WHERE id=?", (nid,)).fetchone()
    return {"ok": True, "note": dict(row)}


def api_bible_note_delete(conn, qs, body):
    ensure(conn)
    nid = _int(body_get(body, "id", 0), 0)
    if nid < 1:
        return _err("id 无效")
    conn.execute("DELETE FROM bible_notes WHERE id=?", (nid,))
    return {"ok": True}


def api_bible_import(conn, qs, body):
    ensure(conn)
    verses = body_get(body, "verses", [])
    if not isinstance(verses, list):
        return _err("verses 必须是数组")
    if len(verses) > MAX_IMPORT:
        return _err(f"单批最多 {MAX_IMPORT} 条，当前 {len(verses)} 条")
    mode = str(body_get(body, "mode", "merge")).strip().lower()
    if mode not in {"merge", "replace"}:
        mode = "merge"
    version = str(body_get(body, "version", "")).strip()[:60] or "自导入"

    valid, skipped = [], 0
    for item in verses:
        if not isinstance(item, dict):
            skipped += 1
            continue
        bid = _resolve_book(item.get("book"))
        text = str(item.get("text") or "").strip()
        text_en = str(item.get("text_en") or "").strip()
        if not bid or not text:
            skipped += 1
            continue
        try:
            chapter = int(item.get("chapter"))
            verse = int(item.get("verse"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (1 <= chapter <= BOOK_INDEX[bid]["chapters"]) or verse < 1:
            skipped += 1
            continue
        valid.append((bid, chapter, verse, text, text_en, version))

    if mode == "replace":
        # 一条都过不了校验时绝不能清库：否则用户传了个坏文件，整本经文就没了，
        # 而接口还回 ok —— 数据没了、提示却是成功。
        if not valid:
            return _err(f"没有可导入的经文（{skipped} 条全部被跳过），已保留现有经文")
        old_n = conn.execute("SELECT COUNT(*) FROM bible_verses").fetchone()[0]
        # 只挡「有跳过 + 合法数远少于现有」这种明显的坏文件：完整导入
        # （skipped=0）和合法的少量覆盖都照旧放行，不改动覆盖功能本身。
        if skipped and old_n and len(valid) < old_n * 0.5:
            return _err(f"只有 {len(valid)} 条合法、{skipped} 条被跳过，远少于现有 {old_n} 节；"
                        "已保留现有经文，请检查文件格式后重试")
        conn.execute("DELETE FROM bible_verses")
        # 标注 / 书签 / 笔记里的 start/end 是旧译本的字符偏移；译本一换，
        # 这些偏移就指向别的字。界面承诺「覆盖会清空全部标注、笔记」，
        # 后端必须真的清，否则留下来的是错位的空标注。
        conn.execute("DELETE FROM bible_highlights")
        conn.execute("DELETE FROM bible_bookmarks")
        conn.execute("DELETE FROM bible_notes")
        conn.execute("DELETE FROM bible_progress")
    if valid:
        conn.executemany(
            """INSERT INTO bible_verses(book,chapter,verse,text,text_en,version)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(book,chapter,verse) DO UPDATE SET
                 text=excluded.text,
                 text_en=CASE WHEN excluded.text_en<>'' THEN excluded.text_en
                              ELSE bible_verses.text_en END,
                 version=excluded.version""", valid)
    now = _now()
    conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('demo','0')")
    conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('version',?)",
                 (version,))
    conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('imported_at',?)",
                 (now,))
    return {"ok": True, "imported": len(valid), "skipped": skipped,
            "stats": _stats(conn)}


def api_bible_reset(conn, qs, body):
    ensure(conn)
    if not _bool(body_get(body, "confirm", False)):
        return _err("需要 confirm=true 才能重置")
    conn.execute("DELETE FROM bible_verses")
    conn.execute("DELETE FROM bible_highlights")
    conn.execute("DELETE FROM bible_bookmarks")
    conn.execute("DELETE FROM bible_notes")
    conn.execute("DELETE FROM bible_progress")
    _seed_demo(conn)
    conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('demo','1')")
    conn.execute("INSERT OR REPLACE INTO bible_meta(key,value) VALUES('version',?)",
                 (DEMO_VERSION,))
    conn.execute("DELETE FROM bible_meta WHERE key='imported_at'")
    return {"ok": True}


AI_SYSTEMS = {
    "chat": ("你是圣经阅读助手。只依据用户给出的经文回答问题：先给结论，再引经文依据；"
             "经文里没有的信息就明说「所给经文中没有提到」。绝不编造经文，不引申外部知识，"
             "不添加经文以外的历史背景、神学传统或人物事迹。用简体中文回答。"),
    "summary": ("你是圣经阅读助手。只依据用户给出的本章经文做总结：一句话主旨、3–6 条要点、"
                "一处值得追问的地方，分三部分输出。不引入经文以外的知识，不编造，不引申。"
                "用简体中文。"),
    "explain": ("你是圣经阅读助手。只解释用户选中的这段经文：字面意思、在本章上下文中的作用、"
                "可能的含义，分三段写。不确定的地方明确标注为推测；不引入外部知识，不编造，"
                "不引申。用简体中文。"),
}


def api_bible_ai(conn, qs, body):
    """AI 伴读：只依据给定经文，不编造、不引申外部知识。异常统一返回 error。"""
    try:
        ensure(conn)
        action = str(body_get(body, "action", "chat")).strip().lower()
        if action not in AI_SYSTEMS:
            action = "chat"
        bid = _resolve_book(body_get(body, "book")) or BOOKS[0]["id"]
        b = BOOK_INDEX[bid]
        chapter = _int(body_get(body, "chapter", 1), 1, 1, b["chapters"])
        version = _meta(conn).get("version") or DEMO_VERSION

        rows = conn.execute(
            "SELECT verse,text FROM bible_verses WHERE book=? AND chapter=? ORDER BY verse",
            (bid, chapter)).fetchall()
        full = "\n".join(f"{r['verse']} {r['text']}" for r in rows)
        chapter_text = full[:MAX_AI_CHARS]
        selection = str(body_get(body, "selection", "")).strip()[:6000]
        question = str(body_get(body, "question", "")).strip()[:2000]

        user = [f"书卷：{b['name']}（{bid}）", f"章：第 {chapter} 章",
                f"译本：{version}", "",
                "【本章经文】", chapter_text or "（本章暂无经文，请如实说明）"]
        if selection:
            user += ["", "【选中经文】", selection]
        if question:
            user += ["", "【我的问题】", question]
        else:
            user += ["", "请按系统指令完成阅读任务。"]
        messages = [{"role": "system", "content": AI_SYSTEMS[action]},
                    {"role": "user", "content": "\n".join(user)}]

        fn = _CTX.get("ai_chat")
        if not callable(fn):
            raise RuntimeError("AI 通道未配置：ai_chat 未注入")
        text = fn(messages, max_tokens=2000)
        try:
            cfg_fn = _CTX.get("ai_config")
            model = (cfg_fn() if callable(cfg_fn) else {}).get("model", "") or ""
        except Exception:
            model = ""
        return {"ok": True, "text": str(text or "").strip(), "model": model,
                "context": {"book": bid, "chapter": chapter,
                            "chars": len(chapter_text)}}
    except Exception:  # noqa: BLE001
        # 与其它 15 个接口一致：这里不吞异常。交给 server 的统一处理器转成
        # 500 + 随机 ref + app.log 堆栈，避免把上游 URL / 密钥 / 本机路径
        # 原样回显给浏览器，也避免内部错误伪装成 HTTP 200。
        _LOG.exception("bible ai 调用失败")
        raise


# ══════════════════════════════════════════════════════════
# 路由表（全部用 query string / JSON body，不用路径参数）
# ══════════════════════════════════════════════════════════

GET_ROUTES = {
    "/api/bible/state": _guard(api_bible_state),
    "/api/bible/chapter": _guard(api_bible_chapter),
    "/api/bible/search": _guard(api_bible_search),
    "/api/bible/highlights": _guard(api_bible_highlights),
    "/api/bible/notes": _guard(api_bible_notes),
    "/api/bible/export": _guard(api_bible_export),
}

POST_ROUTES = {
    "/api/bible/progress": _guard(api_bible_progress),
    "/api/bible/highlight": _guard(api_bible_highlight),
    "/api/bible/highlight/delete": _guard(api_bible_highlight_delete),
    "/api/bible/bookmark": _guard(api_bible_bookmark),
    "/api/bible/bookmark/delete": _guard(api_bible_bookmark_delete),
    "/api/bible/note": _guard(api_bible_note),
    "/api/bible/note/delete": _guard(api_bible_note_delete),
    "/api/bible/import": _guard(api_bible_import),
    "/api/bible/reset": _guard(api_bible_reset),
    "/api/bible/ai": _guard(api_bible_ai),
}
