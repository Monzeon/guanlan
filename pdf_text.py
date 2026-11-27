#!/usr/bin/env python
"""观澜 GUANLAN · PDF 正文抽取（版面还原）

pypdf 自带的 extract_text 只按内容流顺序吐字，行列关系全丢：
  * 双栏论文会左右串栏，读起来像两句话被锯开拼在一起；
  * 页眉、页脚、页码混进正文，每页插一句；
  * 双语对照的原文与译文粘成一句（"1776.中大陆会议"）；
  * 段首缩进、行距、短行收尾这些分段线索全部消失，只能按字数硬切。

这里改成「先还原版面，再切段」：
  1. 用 visitor 逐个文字块取 (x, y, 字号)，按基线聚成行；
  2. 找栏缝（整页稳定的一条空白带），双栏按「通栏 → 左栏 → 右栏」重排；
  3. 去掉跨页重复的页眉页脚与页码；
  4. 按行距、首行缩进、短行收尾、字号变化、双语标签换段；
  5. 段内接断行：英文补空格、行末连字符还原、中文不补空格。

只依赖随应用携带的 vendor/pypdf，不新增任何第三方包。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
MAX_PAGES = 200
CHUNK_BUDGET = 60_000

# 双语对照常见的行首标签。只认「单独一个短标签 + 空白」的形态，
# 避免把正文里正常出现的「中国」「英国」当成标签切段。
_MARKERS = ("EN", "ZH", "CN", "JP", "中", "英", "日", "中译", "英译", "原文", "译文")

# 行末连字符：只有「前一段是像样的单词」才当作断词还原
_HYPHEN_RE = re.compile(r"([A-Za-z]{3,})-$")

# 句末标点。判断「上一行是不是收尾了」用，中英都认。
_SENT_END = "。！？.!?；;：:…」』）)"


@dataclass
class _Span:
    x: float
    y: float
    size: float
    text: str
    width: float = 0.0


@dataclass
class _Line:
    y: float
    size: float
    spans: list[_Span] = field(default_factory=list)
    label: str = ""          # 行首双语标签（EN / 中 / 译文…）
    band: str = "body"       # body | head | foot

    @property
    def x0(self) -> float:
        return min(s.x for s in self.spans)

    @property
    def x1(self) -> float:
        return max(s.x + s.width for s in self.spans)

    @property
    def text(self) -> str:
        return _join_spans(self.spans)


# ── 几何 ────────────────────────────────────────────────────
def _apply(tm, cm):
    """PDF 文本矩阵 × CTM，得到设备坐标（3×2 仿射矩阵乘法）。"""
    a, b, c, d, e, f = tm
    A, B, C, D, E, F = cm
    return (a * A + b * C, a * B + b * D,
            c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


_NARROW = set("ijl.,:;'`|!()[]{}/\\-")
_WIDE = set("mwMW@%&")
_CJK_RANGES = ((0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
               (0xFF00, 0xFF60), (0xFFE0, 0xFFE6), (0x3000, 0x303F))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _span_width(text: str, size: float) -> float:
    """估算文字宽度。

    只用来判断「这里是词间空格还是栏缝」，不需要像素级准确：误差在
    0.15 em 以内就足以区分 0.25 em 的词距和 2 em 的栏缝。
    """
    total = 0.0
    for ch in text:
        if ch in " \t":
            total += 0.28
        elif _is_cjk(ch):
            total += 1.0
        elif ch in _NARROW:
            total += 0.30
        elif ch in _WIDE:
            total += 0.85
        elif ch.isupper() or ch.isdigit():
            total += 0.58
        elif ch.islower():
            total += 0.50
        else:
            total += 0.55
    return total * size


def _needs_space(left: str, right: str) -> bool:
    """两个文字块拼接时要不要补空格。中文之间永远不补。"""
    if not left or not right:
        return False
    if left[-1].isspace() or right[0].isspace():
        return False
    a, b = left[-1], right[0]
    if _is_cjk(a) and _is_cjk(b):
        return False
    if _is_cjk(a) and b in "，。、；：？！）」》】":
        return False
    return True


def _join_spans(spans: list[_Span]) -> str:
    """同一行内按 x 排序拼字，只在够宽的空隙处补一个空格。"""
    out = ""
    prev_end = None
    for span in sorted(spans, key=lambda s: s.x):
        text = span.text
        if not out:
            out = text
        else:
            gap = span.x - (prev_end if prev_end is not None else span.x)
            add = ""
            if gap > 0.26 * max(span.size, 1.0) and _needs_space(out, text):
                add = " "
            out += add + text
        prev_end = span.x + span.width
    out = re.sub(r"[ \t]{2,}", " ", out)
    # 全角括号/引号内不该留半角空格（PDF 换字体时常硬塞一个）
    out = re.sub(r"(?<=[（【《「“‘])\s+|\s+(?=[）】》」”’])", "", out)
    # 中文标点前后也不该带空格："大陆会议， 1776" → "大陆会议，1776"
    out = re.sub(r"(?<=[，。、；：？！])\s+", "", out)
    out = re.sub(r"(?<=[^\x00-\x7f])\s+(?=[，。、；：？！])", "", out)
    return out.strip()


# ── 逐页取字 ────────────────────────────────────────────────
def _page_lines(page, page_h: float) -> tuple[list[_Line], str]:
    """用 visitor 拿带坐标的文字块，聚成行；同时带回 pypdf 的裸文本。

    裸文本是兜底用的：坐标拿不到、或者这页是海报式版面时，内容流顺序
    反而是排版软件写入的阅读顺序。同一次调用里两样都拿到，不重复解析。
    """
    spans: list[_Span] = []
    rotated = 0

    def visit(text, cm, tm, _font, size):
        nonlocal rotated
        if not text or not text.strip():
            return
        a, b, c, d, e, f = _apply(list(tm), list(cm))
        if abs(d) < 0.12:            # 竖排/旋转文字，交给兜底路径
            rotated += 1
            return
        scale = math.sqrt(abs(a * d - b * c)) or 1.0
        try:
            size = float(size or 0) * scale
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0.5:
            size = 10.0
        y = f if d < 0 else (page_h - f)
        spans.append(_Span(float(e), float(y), size, text,
                           _span_width(text, size)))

    try:
        plain = page.extract_text(visitor_text=visit) or ""
    except Exception:
        return [], ""
    if not spans:
        return [], plain
    if rotated > len(spans):         # 大半是旋转文字：这页不做版面推断
        return [], plain

    spans.sort(key=lambda s: (s.y, s.x))
    med = sorted(s.size for s in spans)[len(spans) // 2]
    tol = max(1.2, 0.42 * med)

    lines: list[_Line] = []
    for span in spans:
        if lines and abs(span.y - lines[-1].y) <= tol:
            lines[-1].spans.append(span)
        else:
            lines.append(_Line(y=span.y, size=span.size, spans=[span]))
    for line in lines:
        line.size = max(s.size for s in line.spans)
    return lines, plain


def _label_of(line: _Line) -> str:
    """行首是不是双语标签。

    旧实现要求「整个 span 恰好等于标签」且「至少两个 span」，但排版软件
    常把「EN In Congress…」整行写成一个 span：于是 `EN`/`ZH` 交替的
    分栏被并成一大段，译文和原文粘在一起。这里改成认行首前缀。
    """
    if not line.spans:
        return ""
    first = min(line.spans, key=lambda s: s.x)
    head = first.text.lstrip()
    m = re.match(
        r"^(EN|ZH|CN|JP|中译|英译|原文|译文|中|英|日)(?=[\s:：]|$)", head)
    return m.group(1) if m else ""


# ── 版面：分栏 ──────────────────────────────────────────────
def _split_cells(line: _Line) -> list[_Line]:
    """把一行里被大空隙隔开的文字块切成「单元」，用来识别栏缝。"""
    cells: list[_Line] = []
    for span in sorted(line.spans, key=lambda s: s.x):
        if cells:
            last = cells[-1]
            gap = span.x - last.x1
            if gap > 2.0 * max(span.size, 1.0):
                cells.append(_Line(y=line.y, size=span.size, spans=[span]))
                continue
            last.spans.append(span)
            last.size = max(last.size, span.size)
        else:
            cells.append(_Line(y=line.y, size=span.size, spans=[span]))
    for cell in cells:
        cell.size = max((s.size for s in cell.spans), default=line.size)
        cell.band = line.band
    return cells


def _detect_gutter(lines: list[_Line], page_w: float) -> float | None:
    """找一条整页稳定的栏缝；没有就返回 None（单栏）。"""
    if len(lines) < 6:
        return None
    buckets: dict[int, int] = {}
    two_cell = 0
    for line in lines:
        cells = _split_cells(line)
        if len(cells) != 2:
            continue
        left, right = cells
        if left.x1 - left.x0 < page_w * 0.12 or right.x1 - right.x0 < page_w * 0.12:
            continue
        two_cell += 1
        mid = (left.x1 + right.x0) / 2
        key = int(round(mid / max(4.0, page_w * 0.01)))
        buckets[key] = buckets.get(key, 0) + 1
    if two_cell < max(4, int(len(lines) * 0.3)):
        return None
    key = max(buckets, key=buckets.get)
    if buckets[key] < max(4, int(two_cell * 0.6)):
        return None
    gutter = key * max(4.0, page_w * 0.01)

    # 栏缝两侧都不许有字横跨，否则那不是缝
    crossing = sum(1 for ln in lines if ln.x0 < gutter - 2 < gutter + 2 < ln.x1
                   and len(_split_cells(ln)) == 1)
    if crossing > max(2, len(lines) * 0.2):
        return None
    return gutter


def _order_streams(lines: list[_Line], gutter: float | None,
                   page_w: float = 0.0) -> list[list[_Line]]:
    """按阅读顺序排出「文字流」：通栏一段一段，双栏按左栏到底、右栏到底。

    每一栏是一条独立的流：栏宽不同，段落收尾的判据也不同，混在一起算
    就会把窄栏的每一行都当成「没写满」。
    """
    if gutter is None:
        return [sorted(lines, key=lambda l: l.y)]

    bands: list[dict] = []
    for line in sorted(lines, key=lambda l: l.y):
        cells = _split_cells(line)
        if len(cells) == 2:
            left, right = cells
            wide_enough = (page_w <= 0
                           or min(left.x1 - left.x0, right.x1 - right.x0) > page_w * 0.10)
            if wide_enough and left.x1 <= gutter + 4 and right.x0 >= gutter - 4:
                if bands and bands[-1]["kind"] == "cols":
                    bands[-1]["left"].append(left)
                    bands[-1]["right"].append(right)
                else:
                    bands.append({"kind": "cols", "left": [left], "right": [right]})
                continue
        if line.x1 <= gutter + 4:
            cell, side = line, "left"
        elif line.x0 >= gutter - 4:
            cell, side = line, "right"
        else:
            bands.append({"kind": "full", "line": line})
            continue
        if bands and bands[-1]["kind"] == "cols":
            bands[-1][side].append(cell)
        elif side == "left":
            bands.append({"kind": "cols", "left": [cell], "right": []})
        else:
            # 右栏先出现（页首一根通栏线被切断了），当成通栏处理更安全
            bands.append({"kind": "full", "line": cell})

    out: list[list[_Line]] = []
    for band in bands:
        if band["kind"] == "full":
            out.append([band["line"]])
        else:
            if band["left"]:
                out.append(sorted(band["left"], key=lambda l: l.y))
            if band["right"]:
                out.append(sorted(band["right"], key=lambda l: l.y))
    return out


# ── 页眉页脚 ────────────────────────────────────────────────
def _norm_furniture(text: str) -> str:
    """页眉归一化：空白、分隔符的排版差异不该把同一条页眉裂成两个 key。

    实测「…正面态度 | 18」与「…正面态度|18」被旧实现当成两条，
    于是后者漏网、整行页眉混进正文。
    """
    s = re.sub(r"\s+", "", text)
    s = re.sub(r"[|｜·・—–\-/\\]", "", s)
    return re.sub(r"\d+", "#", s).strip()


def _mark_furniture(pages: list[list[_Line]], page_h: float) -> set[str]:
    """跨页重复的页眉页脚、页码行标成 head/foot；返回学到的重复行集合。

    返回值还要给「退回 pypdf 裸文本」的页面用：那些页没有坐标，
    只能按文本把页眉行剔掉，否则页眉会整行留在正文里。
    """
    if len(pages) < 2:
        for lines in pages:
            for line in lines:
                if _is_page_number(line.text) and (
                        line.y < page_h * 0.1 or line.y > page_h * 0.9):
                    line.band = "foot"
        return set()

    seen: dict[str, int] = {}
    for lines in pages:
        keys = set()
        for line in lines:
            if line.y < page_h * 0.1 or line.y > page_h * 0.92:
                keys.add(_norm_furniture(line.text))
        for key in keys:
            if len(key) >= 2:
                seen[key] = seen.get(key, 0) + 1

    # 24 页的小册子里页眉会出现在几乎每一页，但首页/章节页未必带页眉；
    # 0.5 的阈值让「只出现 11/24 次」的页眉漏网。0.3 更贴合实际版式。
    threshold = max(2, int(len(pages) * 0.3))
    repeat = {k for k, n in seen.items() if n >= threshold}
    for lines in pages:
        for line in lines:
            edge = line.y < page_h * 0.1 or line.y > page_h * 0.92
            if not edge:
                continue
            text = line.text
            if _is_page_number(text) or _norm_furniture(text) in repeat:
                line.band = "head" if line.y < page_h * 0.5 else "foot"
    return repeat


def _is_page_number(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if re.fullmatch(r"[-—–(\[]?\s*\d{1,4}\s*[-—–)\]]?", s):
        return True
    if re.fullmatch(r"(?i)page\s*\d{1,4}(\s*(of|/)\s*\d{1,4})?", s):
        return True
    if re.fullmatch(r"第\s*\d{1,4}\s*页(\s*[/／共]\s*\d{1,4}\s*页?)?", s):
        return True
    return False


def _drop_furniture(lines: list[_Line]) -> list[_Line]:
    """只按页眉/页脚标记过滤。

    旧实现还额外丢掉所有 `_is_page_number()` 为真的行，而这个函数把
    「2024」「68」「5」都算页码 —— 图表数据行、年份行、脚注编号会被
    静默删掉，正文里再也搜不到。页码识别限定在 `_mark_furniture` 里
    的页边行，那里有坐标可用。
    """
    return [ln for ln in lines if ln.band == "body"]


# ── 分段 ────────────────────────────────────────────────────
def _script(line: _Line) -> str:
    """cjk / latin / mix —— 判断双语对照的段落边界用。"""
    text = re.sub(r"\s+", "", line.text)
    if not text:
        return "mix"
    cjk = sum(1 for ch in text if _is_cjk(ch))
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if cjk >= latin * 2 and cjk > 2:
        return "cjk"
    if latin >= cjk * 2 and latin > 6:
        return "latin"
    return "mix"


def _mono_gutter(x0: float) -> float:
    """正文左边界：取整到 2pt，抹掉一点抖动。"""
    return round(x0 / 2.0) * 2.0


def _lines_to_blocks(lines: list[_Line]) -> list[str]:
    """把一栏的行切成段落。返回的每一块是一个自然段。

    分段只看几何线索 —— 行距、缩进、短行收尾、字号跳变、双语标签 —— 不按
    字数硬切：一份文件里中英文的栏宽本来就不一样，用固定宽度判断「这行是
    不是收尾了」会把中文段落从中间锯开。
    """
    lines = [ln for ln in lines if ln.text.strip()]
    if not lines:
        return []

    left = _most_common([_mono_gutter(ln.x0) for ln in lines])
    gaps = []
    for prev, cur in zip(lines, lines[1:]):
        dy = cur.y - prev.y
        if 0 < dy < max(prev.size, cur.size) * 3.2:
            gaps.append(dy)
    leading = _median(gaps) if gaps else max((ln.size for ln in lines), default=10) * 1.35
    rights = sorted(ln.x1 for ln in lines)
    col_right = rights[int(len(rights) * 0.92)] if rights else 0.0
    col_width = max(col_right - left, 1.0)

    blocks: list[str] = []
    pending: list[_Line] = []
    list_page = _is_list_page(lines, left, col_right)

    def flush():
        if pending:
            text = _join_block(pending)
            if text:
                blocks.append(text)
            pending.clear()

    for line in lines:
        if pending:
            prev = pending[-1]
            gap = line.y - prev.y
            size = max(prev.size, line.size)
            # 段落自身的左右边界：中英混排时整页右界会被英文撑宽，
            # 拿页面右界判断「中文这行是不是收尾」必然误判。
            window = pending[-6:]
            para_right = max(p.x1 for p in window)
            para_left = min(p.x0 for p in window)
            ended = prev.text[-1:] in _SENT_END
            short_prev = prev.x1 < para_right - max(4.2 * size, 34)
            centered_prev = ((prev.x0 - left) > size * 1.5
                             and (col_right - prev.x1) > size * 6)
            indented = ((line.x0 - para_left) > size * 0.9
                        and line.x1 > col_right - size * 8)

            if gap > leading * 1.42 or gap < 0:
                flush()
            elif abs(line.size - prev.size) > max(1.0, prev.size * 0.16):
                flush()                                     # 换字号：标题 / 脚注
            elif indented:
                flush()                                     # 首行缩进
            elif list_page and prev.x1 < col_right - max(2.2 * size, 24):
                # 条目页：上一条没写满就是下一条。但网址、续行的下一行、
                # 以及上一行以逗号/冒号/破折号收尾的，都是同一句的延续。
                if not (re.match(r"(?i)\s*(https?://|www\.)", line.text)
                        or prev.text[-1:] in "，、：；—-（(「“"
                        or line.text[:1].islower()):
                    flush()
            elif line.label and prev.label != line.label:
                flush()                                     # 双语标签换了
            elif centered_prev and line.x0 < prev.x0 - size * 2:
                flush()                                     # 居中标题接正文
            elif ended and short_prev:
                flush()                                     # 上一行短收尾
            elif ended and _script(prev) != _script(line) \
                    and "mix" not in (_script(prev), _script(line)):
                flush()                                     # 中英交替且已收句
            elif ended and re.match(
                    r"^([•·▪◦]|\(?\d{1,2}[.、)]|[一二三四五六七八九十]{1,3}[、.])\s*\S",
                    line.text):
                flush()                                     # 列表项另起
        pending.append(line)
    flush()
    return blocks


_FURNITURE_CHARS = set("，。、；：？！（）()【】《》「」『』…—-·.,;:!?%|/\\ \t'\"")


def _page_kind(lines: list[_Line]) -> str:
    """这页是「成篇的正文」还是「设计过的版面」。

    海报式版面（公众号长图、印刷网页、封面）里，标题、竖排角标、图注各自
    独立定位，按行距和缩进切段只会把一句话切碎。这种页退回内容流顺序，
    也就是浏览器/排版软件写入的原始阅读顺序，反而更稳。

    但「短行多」不等于海报：带脚注编号和图表数据的密集正文页，短行里
    绝大多数是「5」「68%」「，」这类数字/标点，旧判据把它们全算成
    poster 的角标，把整页退回裸文本，页眉混进来、段落全丢。
    """
    if len(lines) >= 20:
        return "flow"                       # 密集正文页不可能是海报
    texts = [ln.text.strip() for ln in lines]
    tiny = sum(1 for t in texts
               if len(t) <= 2
               and not all(ch.isdigit() or ch.isspace() or ch in _FURNITURE_CHARS
                           for ch in t))
    if tiny > len(lines) * 0.35:
        return "poster"
    body = _most_common([round(ln.size) for ln in lines])
    biggest = max((ln.size for ln in lines), default=0)
    if body and biggest > body * 2.4:
        return "poster"
    return "flow"


def _is_list_page(lines: list[_Line], left: float, col_right: float) -> bool:
    """整页都是短行、且不靠句号收尾 —— 条目页（签署人、地址、目录、诗行）。

    这种页面一行就是一条，不能按「段内接断行」全并成一大段。
    """
    width = max(col_right - left, 1.0)
    short = [ln for ln in lines if (ln.x1 - ln.x0) < width * 0.62]
    if len(short) < 8 or len(short) < len(lines) * 0.4:
        return False
    ended = sum(1 for ln in short if ln.text[-1:] in _SENT_END)
    return ended < len(short) * 0.5


def _most_common(values: list[float]) -> float:
    if not values:
        return 0.0
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


def _median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    if not n:
        return 0.0
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def _join_block(lines: list[_Line]) -> str:
    """段内接断行。中文不补空格，英文补一个，行末连字符还原。"""
    out = ""
    for index, line in enumerate(lines):
        text = line.text
        if not text:
            continue
        if not out:
            out = text
            continue
        # 只丢真正的页眉/页脚行；正文里的「2024」「68」是数据，不能删
        if line.band != "body" and _is_page_number(text):
            continue
        hyphen = _HYPHEN_RE.search(out)
        if hyphen and text[:1].islower():
            tail = out.split()[-1] if out.split() else ""
            if "://" in tail or "/" in tail:
                out = out + text                        # 网址在连字符处换行：连字符是正文
            else:
                out = out[:hyphen.end(1)] + text        # 断词还原：只去掉连字符
            continue
        if _needs_space(out, text):
            out += " "
        out += text
    return out.strip()


# ── 整篇 ────────────────────────────────────────────────────
# 目录页的引导点线：排版软件用几百个半角点把标题推到页码前，版面还原会把
# 整页目录拼成一行几千字。这份报告里 3995 字的目录页有 3854 个点是引导符，
# 占全篇正文近两成，阅读器和 AI 问答都会被噪声淹没。
_LEADER_DOTS = re.compile(r"[.．]{4,}[ \t]*")


def _tidy_leader_dots(text: str) -> str:
    """长串引导点压成一个省略号，并把粘连的目录项拆成独立段落。"""
    if not _LEADER_DOTS.search(text):
        return text
    cleaned = _LEADER_DOTS.sub(" … ", text)
    out: list[str] = []
    for line in cleaned.split("\n"):
        # 只在「一行粘了好几个目录项」时拆条目。两道保险：
        #   1) 行内至少两处 " … "，单项标题不拆；
        #   2) (?<!\d) 防止把「2024 年」从 "024" 处误判成页码。
        if line.count(" … ") >= 2:
            line = re.sub(r"(?<!\d)(\d{1,3})[ \t]+(?=[^\d\s…])", r"\1\n\n", line)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _explicit_page_label(text: str) -> bool:
    s = text.strip()
    return bool(re.fullmatch(r"(?i)page\s*\d{1,4}(\s*(of|/)\s*\d{1,4})?", s)
                or re.fullmatch(r"第\s*\d{1,4}\s*页(\s*[/／共]\s*\d{1,4}\s*页?)?", s))


def _clean_plain(text: str, repeat: set[str]) -> str:
    """兜底页（没有坐标）同样要清页眉、显式页码行和目录引导点线。

    以前这条路径只做了 `strip()`，海报式版面和旋转页会把「… | 4」
    这类页眉整行留在正文里；目录点线也没人管。
    """
    body = re.sub(r"[ \t]+\n", "\n", text or "").strip()
    # 显式页码行（"Page 3" / "第 3 页"）无论有没有学到跨页页眉都要删；
    # 旧写法把它塞在 `if repeat` 里，单页 / 无页眉的兜底页就把页码留在正文。
    kept = []
    for line in body.split("\n"):
        key = _norm_furniture(line)
        if _explicit_page_label(line) or (repeat and key and key in repeat):
            continue
        kept.append(line)
    body = "\n".join(kept)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return _tidy_leader_dots(body)


def _blocks_to_text(blocks: list[str]) -> str:
    return "\n\n".join(b for b in (_tidy_leader_dots(x.strip()) for x in blocks) if b)


def _garble_sample(text: str, limit: int = 4000) -> str:
    """全文均匀取三段，别让目录页 / 封面 / 图表页独占样本。

    旧实现只取开头 4000 字。目录页的点线引导符（"概述............2"）
    一页就能写满 4000 字，于是整篇报告的判定全被这一页代表。
    """
    if len(text) <= limit:
        return text
    part = limit // 3
    mid = max(0, (len(text) - part) // 2)
    return text[:part] + text[mid:mid + part] + text[-(limit - 2 * part):]


def looks_garbled(text: str) -> bool:
    """字形编码坏了（PUA 私用区、替换符、控制符成片）才当作没读出来。

    曾经拿「字母数字占比 < 15%」当判据，结果目录页的点线引导符、表格页
    的竖线与百分比、图注页的短标签全被算成「非内容」：一份文字层完好的
    中文报告（开头 4000 字里 3854 个是目录点线）被判成乱码，整篇正文被
    清空，还回了一句完全误导的「文字层编码异常」。

    低字母占比是正常排版，不是乱码。这里只认真正的编码坏信号：
      1. 替换符 / 私用区码位 / 控制符成片（> 6%）；
      2. 采样里一个字母数字或汉字都找不到。
    「内容太少」不是乱码，由调用方按空页 / 空块另行判断。
    """
    if not text:
        return True
    sample = _garble_sample(text)
    if not sample:
        return True
    bad = sum(1 for ch in sample
              if ch == "\ufffd" or 0xE000 <= ord(ch) <= 0xF8FF
              or (ord(ch) < 32 and ch not in "\n\t"))
    if bad > len(sample) * 0.06:
        return True
    return not any(ch.isalnum() or _is_cjk(ch) for ch in sample)


# 兼容旧调用点（tools/verify_pdf_parse.py 等）
_looks_garbled = looks_garbled


def extract_pdf_pages(reader, budget: int = CHUNK_BUDGET) -> tuple[list[str], int]:
    """按页抽正文，返回 (每页文字, 读了几页)。页面文字已分好段。"""
    try:
        pages_obj = list(reader.pages[:MAX_PAGES])
    except Exception:
        return [], 0

    raw_lines: list[list[_Line]] = []
    widths: list[float] = []
    heights: list[float] = []
    plain: list[str] = []
    empty_streak = 0                    # 连续无文字层的页数（扫描件快速失败）
    first_text_page = -1                # 第一页有文字层的索引
    for page in pages_obj:
        try:
            height = float(page.mediabox.height) or 842.0
            width = float(page.mediabox.width) or 595.0
        except Exception:
            height, width = 842.0, 595.0
        heights.append(height)
        widths.append(width)
        lines, page_plain = _page_lines(page, height)
        # 原始内容流顺序无论走哪条路都留着：某一页的版面还原结果是乱码
        # 时，可以只把那一页退回原始抽取，不必连累整篇。
        plain.append(page_plain)
        # 坐标拿不全（异常字体、纯图片页、旋转页）或者版式是海报式的：
        # 退回 pypdf 的内容流顺序，那是排版软件写入的原始阅读顺序。
        if (len(lines) < 2 or sum(len(l.text) for l in lines) < 8
                or _page_kind(lines) == "poster"):
            raw_lines.append([])
        else:
            raw_lines.append(lines)
            empty_streak = 0
            if first_text_page < 0:
                first_text_page = len(raw_lines) - 1
            continue                    # 有文字层，重置扫描件计数
        # 纯图像页（无文字层）：累计空页
        empty_streak += 1
        # 前 20 页（索引 0-19）都无文字层且没有任何纯文本输出：这是扫描件，不再逐页跑
        # 注意：封面/目录可能是图像页，正文页才有文字层，给 20 页缓冲避免误判
        if empty_streak >= 20 and first_text_page < 0 and not any(plain[:20]):
            break

    repeat: set[str] = set()
    if any(raw_lines):
        # 页眉页脚要跨页比对才知道是不是「每页都一样」，所以整篇一起标；
        # 学到的重复行还要交给没有坐标的兜底页按文本过滤。
        repeat = _mark_furniture([ln for ln in raw_lines if ln],
                                 _median(heights) or 842.0)

    out: list[str] = []
    used = 0
    for index, lines in enumerate(raw_lines):
        if sum(map(len, out)) >= budget:
            break
        used = index + 1
        if not lines:
            out.append(_clean_plain(plain[index] or "", repeat))
            continue
        kept = _drop_furniture(lines)
        for line in kept:
            line.label = _label_of(line)
        gutter = _detect_gutter(kept, widths[index])
        blocks: list[str] = []
        for stream in _order_streams(kept, gutter, widths[index]):
            blocks.extend(_lines_to_blocks(stream))
        page_text = _blocks_to_text(blocks)
        # 逐页兜底：只有这一页的版面还原结果坏了才换内容流顺序，
        # 其余页仍保留整理好的分栏与段落。
        if looks_garbled(page_text) and plain[index]:
            alt = _clean_plain(plain[index], repeat)
            if alt and not looks_garbled(alt):
                page_text = alt
        out.append(page_text)
    return out[:used], used


def extract_pdf_text(path, budget: int = CHUNK_BUDGET) -> tuple[str, str]:
    """PDF → (正文, 状态)。状态只描述「为什么没拿到」，拿到了就是 stored。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF 阅读组件缺失，原件已保存"

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                return "", "PDF 已加密，需要密码才能读取"
        pages, used = extract_pdf_pages(reader, budget)
    except Exception as e:            # 解析器异常要变成可见状态，不能吞掉
        return "", f"PDF 解析失败，原件已保存（{type(e).__name__}）"

    blocks = [p.strip() for p in pages if p and p.strip()]
    if not blocks:
        return "", "PDF 里没有可读取的文字层，可能是扫描件或图片版"
    text = "\n\n".join(blocks).strip()[:budget]

    if not looks_garbled(text):
        # 按 200 页 / 6 万字截断是性能保护，但要如实说出来 —— 以前一律
        # 报 stored，用户以为全文都在库里，AI 问答却永远看不到后半本。
        try:
            total_pages = len(reader.pages)
        except Exception:
            total_pages = used
        # 不能写成 used < min(total_pages, MAX_PAGES)：used 的上限本来就是
        # MAX_PAGES，201 页的书 used=200、min(...)=200，恒为 False，后半本
        # 被静默丢掉却仍报 stored。直接与真实总页数比。
        truncated = used < total_pages or sum(map(len, blocks)) >= budget
        return text, "partial" if truncated else "stored"

    # 版面层整篇是乱码：逐页退回 pypdf 的原始抽取，只保留读得出来的页。
    # 旧实现对整篇做一次判定，一页目录点线就能把全书清空。
    try:
        reader2 = PdfReader(str(path), strict=False)
        if reader2.is_encrypted:
            reader2.decrypt("")
        parts: list[str] = []
        for pg in reader2.pages[:MAX_PAGES]:
            raw = _clean_plain(pg.extract_text() or "", set())
            if raw and not looks_garbled(raw):
                parts.append(raw)
            if sum(map(len, parts)) >= budget:
                break
        fallback = "\n\n".join(parts).strip()[:budget]
    except Exception:
        fallback = ""
    if fallback:
        return fallback, "partial"
    return "", "PDF 文字层编码异常，未能还原正文（可先 OCR 或另存为文本版）"
