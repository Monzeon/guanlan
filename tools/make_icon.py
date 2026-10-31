#!/usr/bin/env python
"""
观澜 GUANLAN 图标生成器 —— 纯标准库（与 ATLAS make_icon 同管线）。

设计语言：「月下澜」
    夜海深蓝底 + 金色主澜/次澜 + 右上明月 + 月映水点。
    语义：澜生于月之引力——你的判断同样被信息潮汐扰动，
    观澜即观测扰动源。月 = 观测者，波 = 被扰动的事实之水。

实现：逐像素距离场光栅化（波浪用解析正弦中心线 + 竖直距离近似，
    视觉等效）+ smoothstep 抗锯齿 + 手写 PNG 编码 + ICO 打包。

    python tools/make_icon.py            生成 app/guanlan.ico
    python tools/make_icon.py --png      额外导出 256px PNG 预览
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── 调色 ─────────────────────────────────────────────
BG_IN = (0x14, 0x23, 0x3C)     # 渐变中心：夜海
BG_OUT = (0x07, 0x0D, 0x18)    # 渐变边缘
WAVE_MAIN = (0xC9, 0x97, 0x3B) # 主澜：金
WAVE_SUB = (0x7A, 0x62, 0x30)  # 次澜：暗金
MOON = (0xEF, 0xCB, 0x84)      # 月：亮金
GLINT = (0xF7, 0xF3, 0xE9)     # 月映水点：纸白

# ── 几何（画布中心为原点，范围 [-0.5, 0.5]，y 向下）──
CORNER = 0.225
BG_GRAD_C = (0.0, 0.0)

WAVE1_Y, WAVE1_AMP, WAVE1_K, WAVE1_PH, WAVE1_W = 0.10, 0.045, math.tau * 1.35, 0.0, 0.034
WAVE2_Y, WAVE2_AMP, WAVE2_K, WAVE2_PH, WAVE2_W = 0.245, 0.030, math.tau * 1.10, 1.8, 0.024

MOON_C = (0.20, -0.235)
MOON_R = 0.072
MOON_HALO = 0.135
GLINT_C = (0.20, 0.10 + 0.045 * math.sin(math.tau * 1.35 * 0.20))
GLINT_R = 0.017


def sd_rounded_rect(px: float, py: float, half: float, r: float) -> float:
    qx, qy = abs(px) - (half - r), abs(py) - (half - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def wave_dist(px: float, py: float, base: float, amp: float,
              k: float, ph: float, halfw: float) -> float:
    """正弦波中心线的竖直距离场（视觉等效 SDF）。"""
    return abs(py - (base + amp * math.sin(k * px + ph))) - halfw


def _alpha(d: float, aa: float) -> float:
    return max(0.0, min(1.0, 0.5 - d / (2 * aa)))


def _over(r: float, g: float, b: float, cr: tuple, a: float) -> tuple:
    """全 0-255 量纲 alpha 混合。"""
    if a <= 0:
        return r, g, b
    return (r * (1 - a) + cr[0] * a,
            g * (1 - a) + cr[1] * a,
            b * (1 - a) + cr[2] * a)


def render(size: int) -> bytes:
    aa = 1.35 / size
    rows = []
    for iy in range(size):
        row = bytearray()
        py = (iy + 0.5) / size - 0.5
        for ix in range(size):
            px = (ix + 0.5) / size - 0.5

            d_bg = sd_rounded_rect(px, py, 0.5, CORNER)
            if d_bg > aa:
                row += b"\x00\x00\x00\x00"
                continue
            g = max(0.0, min(1.0, math.hypot(px - BG_GRAD_C[0], py - BG_GRAD_C[1]) / 0.72))
            r0, g0, b0 = (BG_IN[i] * (1 - g) + BG_OUT[i] * g for i in range(3))

            d_moon = math.hypot(px - MOON_C[0], py - MOON_C[1]) - MOON_R
            d_halo = max(math.hypot(px - MOON_C[0], py - MOON_C[1]) - MOON_HALO, d_bg)
            d_w1 = wave_dist(px, py, WAVE1_Y, WAVE1_AMP, WAVE1_K, WAVE1_PH, WAVE1_W)
            d_w2 = wave_dist(px, py, WAVE2_Y, WAVE2_AMP, WAVE2_K, WAVE2_PH, WAVE2_W)
            d_glint = max(math.hypot(px - GLINT_C[0], py - GLINT_C[1]) - GLINT_R, d_bg)
            # 月映点只落在主澜上方一点，制造"照在浪尖"的感觉
            near_crest = abs(py - (WAVE1_Y + WAVE1_AMP * math.sin(WAVE1_K * GLINT_C[0]))) < 0.10
            if not near_crest:
                d_glint = max(d_glint, d_bg + 1)  # 隐去

            r, g_, b = r0, g0, b0
            r, g_, b = _over(r, g_, b, WAVE_SUB, _alpha(d_w2, aa))
            r, g_, b = _over(r, g_, b, MOON, _alpha(d_halo, aa) * 0.22)
            r, g_, b = _over(r, g_, b, WAVE_MAIN, _alpha(d_w1, aa))
            r, g_, b = _over(r, g_, b, MOON, _alpha(d_moon, aa))
            r, g_, b = _over(r, g_, b, GLINT, _alpha(d_glint, aa) * 0.92)

            a_bg = _alpha(d_bg, aa)
            row += bytes(max(0, min(255, int(c + 0.5)))
                         for c in (r, g_, b, a_bg * 255))
        rows.append(bytes(row))
    return b"".join(rows)


def png_encode(rgba: bytes, size: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgba[i * size * 4:(i + 1) * size * 4] for i in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico_encode(sizes: list[int]) -> bytes:
    frames = [(s, png_encode(render(s), s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, body = b"", b""
    for s, png in frames:
        w = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset)
        body += png
        offset += len(png)
    return out + entries + body


def main() -> int:
    ap = argparse.ArgumentParser(prog="make_icon")
    ap.add_argument("--out", default=str(ROOT / "app" / "guanlan.ico"))
    ap.add_argument("--png", action="store_true")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    data = ico_encode([256, 64, 48, 32, 16])
    Path(args.out).write_bytes(data)
    print(f"已生成 {args.out}  ({len(data):,} bytes, 256/64/48/32/16)")

    if args.png:
        p = Path(args.out).with_suffix(".preview.png")
        p.write_bytes(png_encode(render(256), 256))
        print(f"预览    {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
