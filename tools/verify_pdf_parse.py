#!/usr/bin/env python
"""校验 PDF 正文抽取（版面还原）。

    python tools/verify_pdf_parse.py                  # 造样张，逐条验收
    python tools/verify_pdf_parse.py --pdf 某文件.pdf # 只看一份真实文件的效果
    python tools/verify_pdf_parse.py --keep           # 保留样张，方便肉眼看

样张在系统临时目录里现造现删，不往仓库里塞二进制。六条验收对应六类真实
毛病：中文单栏、英文断词、双栏串栏、双语对照粘连、扫描件、加密件。

只用标准库 + reportlab（造样张用；没装就跳过造样张那几条，不影响对真实
文件跑）。
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 应用自己带了一份 pypdf（vendor/），验收要用用户实际会跑的那一份，
# 不然测的是本机 site-packages 的版本，行为可能对不上。
VENDOR = ROOT / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

import pdf_text  # noqa: E402


# ── 造样张 ──────────────────────────────────────────────────
def build_samples(out_dir: Path) -> dict[str, Path]:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    W, H = A4
    made: dict[str, Path] = {}
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    # 1. 中文单栏：标题 + 两段首行缩进
    c = canvas.Canvas(str(out_dir / "cn-flow.pdf"), pagesize=A4)
    c.setFont("STSong-Light", 18)
    c.drawCentredString(W / 2, H - 90, "第一章　观澜的由来")
    c.setFont("STSong-Light", 11)
    y = H - 130
    for line in ["　　观澜这个词出自孟子，原意是看水面的波纹，后来引申为观察事物的变化。",
                 "写一个能长期用下去的工具，第一件事不是功能多，而是每一条记录都能被找回来，",
                 "找回来之后还要看得懂。"]:
        c.drawString(70, y, line)
        y -= 18
    y -= 6
    for line in ["　　第二段讲的是取舍：不做的事比做的事更重要，因为每加一个功能，就多一处要维护",
                 "的地方，也多一处会出错的地方。"]:
        c.drawString(70, y, line)
        y -= 18
    c.showPage()
    c.save()
    made["cn-flow"] = out_dir / "cn-flow.pdf"

    # 2. 英文：断词 + 缩进分段
    c = canvas.Canvas(str(out_dir / "hyphen-wrap.pdf"), pagesize=A4)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(70, H - 80, "On the Extraction of Text")
    c.setFont("Helvetica", 10)
    for i, line in enumerate([
            "This paragraph demonstrates a hyphen-",
            "ated word that must be rejoined, and continues with normal wrap-",
            "ping behaviour across several lines so that the paragraph is long enough.",
            "A second sentence follows in the same paragraph, ending here."]):
        c.drawString(70, H - 120 - i * 13, line)
    c.drawString(96, H - 190, "This is a new paragraph introduced by a four-space indent, and it runs long")
    c.drawString(70, H - 203, "enough to show that the continuation line is not a fresh paragraph.")
    c.showPage()
    c.save()
    made["hyphen-wrap"] = out_dir / "hyphen-wrap.pdf"

    # 3. 双栏 + 页眉 + 页码
    c = canvas.Canvas(str(out_dir / "two-col.pdf"), pagesize=A4)
    body = ("The quick brown fox jumps over the lazy dog while the committee considers "
            "whether the proposal should be accepted in its present form or returned")
    for page in range(3):
        c.setFont("Helvetica", 8)
        c.drawString(40, H - 30, "Journal of Testing Vol 12")
        c.drawCentredString(W / 2, 20, str(page + 1))
        c.setFont("Helvetica", 9)
        c.drawString(40, H - 70, "Abstract: this line spans both columns and must stay in one piece.")
        for col in (0, 1):
            x = 40 if col == 0 else W / 2 + 14
            for i in range(18):
                c.drawString(x, H - 100 - i * 12, (f"C{col}L{i:02d} " + body)[:52])
        c.showPage()
    c.save()
    made["two-col"] = out_dir / "two-col.pdf"

    # 4. 双语对照：EN / 中 交替，与《独立宣言》那种排法一致
    c = canvas.Canvas(str(out_dir / "bilingual.pdf"), pagesize=A4)
    pairs = [("EN", "In Congress, July 4, 1776.", "中",
              "大陆会议，1776 年 7 月 4 日。"),
             ("EN", "The unanimous Declaration of the thirteen united States of America.", "中",
              "美利坚合众国十三州一致通过的《独立宣言》。"),
             ("EN", "When in the Course of human events, it becomes necessary for one people "
                    "to dissolve the political bands which have connected them with another.",
              "中", "在人类事务发展的过程中，当一个民族必须解除同另一个民族的政治联系时。")]
    y = H - 90
    for en_tag, en, cn_tag, cn in pairs:
        c.setFont("Times-Roman", 10)
        c.drawString(51, y, en_tag)
        c.setFont("Times-Roman", 13)
        c.drawString(73, y, en)
        y -= 20
        c.setFont("STSong-Light", 10)
        c.drawString(62, y, cn_tag)
        c.setFont("STSong-Light", 13.5)
        c.drawString(80, y, cn)
        y -= 46
    c.showPage()
    c.save()
    made["bilingual"] = out_dir / "bilingual.pdf"

    # 5. 无文字层（整页一张白图）
    from reportlab.lib.utils import ImageReader
    from PIL import Image
    blank = out_dir / "_blank.png"
    Image.new("RGB", (600, 800), "white").save(blank)
    c = canvas.Canvas(str(out_dir / "scanned.pdf"), pagesize=A4)
    c.drawImage(ImageReader(str(blank)), 40, 100, width=500, height=650)
    c.showPage()
    c.save()
    made["scanned"] = out_dir / "scanned.pdf"

    # 6. 加密件
    c = canvas.Canvas(str(out_dir / "locked.pdf"), pagesize=A4, encrypt="secret")
    c.setFont("Helvetica", 12)
    c.drawString(60, 700, "Body text that needs a password to read.")
    c.showPage()
    c.save()
    made["locked"] = out_dir / "locked.pdf"

    # 7. 页首正文：第 2 页第一行紧贴页边且很长，不能当页眉删掉
    c = canvas.Canvas(str(out_dir / "page-top.pdf"), pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(60, H - 60, "A short opening paragraph that ends the first page cleanly here.")
    c.showPage()
    c.drawString(60, H - 36, "TOP-MARKER this body line sits at the very top margin of page two and is far too long to be a header")
    c.drawString(60, H - 52, "and it continues on the following line without any heading above it at all.")
    c.showPage()
    c.save()
    made["page-top"] = out_dir / "page-top.pdf"
    return made


# ── 验收 ────────────────────────────────────────────────────
def paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n{2,}", text) if p.strip()]


def _coverage(path: Path, text: str) -> float | None:
    """版面抽取掉的字够不够全：拿 pypdf 裸抽取当底本比二元组。

    页眉页脚与页码会被有意删掉，所以正常值不是 100%，低于 90% 才值得查。
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        plain = "".join((pg.extract_text() or "") for pg in reader.pages[:200])
    except Exception:
        return None
    a = re.sub(r"\s+", "", plain)
    b = re.sub(r"\s+", "", text)
    if len(a) < 400 or not b:
        return None
    step = max(1, len(a) // 400)
    idx = range(0, len(a) - 1, step)
    hits = sum(1 for i in idx if a[i:i + 2] in b)
    return hits / len(list(idx))


def check_samples(samples: dict[str, Path]) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def case(name: str, ok: bool, detail: str) -> None:
        out.append((name, bool(ok), detail))

    text, status = pdf_text.extract_pdf_text(samples["cn-flow"])
    paras = paragraphs(text)
    case("中文单栏：标题独立成段", paras and paras[0].startswith("第一章"), repr(paras[0] if paras else ""))
    case("中文单栏：两段分开", len(paras) == 3, f"{len(paras)} 段")
    case("中文单栏：汉字之间不留空格",
         "观澜这个词" in text and "观的 变化" not in text, repr(text[:40]))

    text, status = pdf_text.extract_pdf_text(samples["hyphen-wrap"])
    case("英文断词：hyphen- + ated 合回 hyphenated", "hyphenated" in text,
         repr(re.search(r".{12}hyphenated.{4}", text).group(0) if "hyphenated" in text else ""))
    case("英文断词：wrap- + ping 合回 wrapping", "wrapping" in text, "")
    case("英文缩进：四空格缩进另起一段", len(paragraphs(text)) == 3,
         f"{len(paragraphs(text))} 段")

    text, status = pdf_text.extract_pdf_text(samples["two-col"])
    order_ok = text.find("C0L00") < text.find("C0L17") < text.find("C1L00")
    case("双栏：左栏读完再读右栏", order_ok,
         f"C0L00@{text.find('C0L00')} C0L17@{text.find('C0L17')} C1L00@{text.find('C1L00')}")
    case("双栏：页眉与页码不进正文",
         "Journal of Testing" not in text and not re.search(r"^\d+$", text, re.M), "")

    text, status = pdf_text.extract_pdf_text(samples["bilingual"])
    paras = paragraphs(text)
    case("双语：EN 与译文各成一段", len(paras) == 6, f"{len(paras)} 段")
    case("双语：原文末句不粘译文", "1776.中" not in text and "America.中" not in text,
         repr([p for p in paras if p.startswith("EN")][:1]))

    text, status = pdf_text.extract_pdf_text(samples["scanned"])
    case("扫描件：明确说没有文字层", not text and "扫描件" in status, status)

    text, status = pdf_text.extract_pdf_text(samples["locked"])
    case("加密件：明确要密码", not text and "密码" in status, status)

    text, status = pdf_text.extract_pdf_text(samples["page-top"])
    case("页首长正文不被当成页眉删掉", "TOP-MARKER" in text,
         repr(text[:80]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 PDF 正文抽取")
    ap.add_argument("--pdf", help="直接看一份真实 PDF 抽出来的前几段")
    ap.add_argument("--keep", action="store_true", help="保留样张目录")
    ap.add_argument("--paras", type=int, default=6, help="--pdf 模式打印几段")
    args = ap.parse_args()

    if args.pdf:
        path = Path(args.pdf)
        text, status = pdf_text.extract_pdf_text(path)
        print(f"{path.name}  status={status}  {len(text)} 字")
        cover = _coverage(path, text)
        if cover is not None:
            print(f"字面覆盖率：{cover:.0%}（对照 pypdf 裸抽取，掉字说明有正文被漏掉）")
        for i, para in enumerate(paragraphs(text)[: args.paras], 1):
            print(f"\n[{i}] {para[:220]}")
        return 0

    try:
        import reportlab  # noqa: F401
    except ImportError:
        print("没装 reportlab，跳过样张验收；可以先用 --pdf 看真实文件。")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="guanlan-pdf-"))
    try:
        samples = build_samples(tmp)
        results = check_samples(samples)
    finally:
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    width = max(len(name) for name, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name:<{width}}  {detail if not ok else ''}".rstrip())
        failed += 0 if ok else 1
    print(f"\n{len(results) - failed}/{len(results)} 通过"
          + ("" if not failed else f"，{failed} 条未通过"))
    if args.keep:
        print(f"样张留在：{tmp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
