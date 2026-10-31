#!/usr/bin/env python
"""
观澜 · 桌面快捷方式生成器。

手写 MS-SHLLINK (.lnk) 二进制——ShellLinkHeader + LinkInfo(本地路径)
+ StringData(相对路径/工作目录/图标, UTF-16)。不依赖 COM/WScript。

    python tools/make_shortcut.py            写到当前用户桌面「观澜.lnk」
    python tools/make_shortcut.py --out X    指定输出路径
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "start.cmd"
WORKDIR = ROOT
ICON = ROOT / "app" / "guanlan.ico"
DESC = "观澜 · 个人认知平台"

LINK_CLSID = bytes.fromhex("0102140000000000C000000000000046")
# 00021401-0000-0000-C000-000000000046 的逐字节序列（mixed-endian GUID）


def u16(s: str) -> bytes:
    """UTF-16LE 字符串 + 结尾 null，前置字符数（含 null）。"""
    data = s.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<H", len(data) // 2) + data


def build_lnk() -> bytes:
    target = str(TARGET)
    workdir = str(WORKDIR)
    icon = str(ICON) + ",0"
    rel = TARGET.name  # 相对工作目录

    flags = 0x01 | 0x04 | 0x08 | 0x20 | 0x80  # LinkInfo|RelativePath|WorkDir|Icon|Unicode

    header = struct.pack(
        "<I16sIIQQQIHHII",
        0x0000004C, LINK_CLSID, flags,
        0x20,               # FileAttributes: ARCHIVE
        0, 0, 0,            # creation / access / write times
        0,                  # file size
        0,                  # icon index
        7,                  # ShowCommand: SW_SHOWMINNOACTIVE
        0, 0,               # hotkey, reserved1
    ) + struct.pack("<II", 0, 0)  # reserved2, reserved3
    assert len(header) == 76, len(header)

    # ── LinkInfo：六字段头 + VolumeID + LocalBasePath + CommonSuffix ──
    # 头部严格 0x1C = 24 字节（六个 u32），偏移全部相对 LinkInfo 起始
    local_path = target.encode("mbcs", "replace") + b"\x00"
    volume = struct.pack("<IIII", 0x11, 3, 0, 0x10) + b"\x00"
    # VolumeIDSize=0x11(17B：头16B + 空label含null), DriveType=3 FIXED

    vol_off = 0x1C
    lbp_off = vol_off + len(volume)
    suffix_off = lbp_off + len(local_path)   # CommonPathSuffix 必须有有效偏移
    li_body = (struct.pack("<IIIIII",
                           0x1C,    # LinkInfoHeaderSize
                           0x01,    # Flags: VolumeIDAndLocalBasePath
                           vol_off,
                           lbp_off,
                           0,       # CommonNetworkRelativeLinkOffset：无
                           suffix_off)
               + volume + local_path + b"\x00")
    link_info = struct.pack("<I", len(li_body) + 4) + li_body

    # ── StringData ──
    strings = u16(rel) + u16(workdir) + u16(icon)

    out = header + link_info + strings
    out += struct.pack("<I", 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="make_shortcut")
    ap.add_argument("--out", default=str(
        Path.home() / "Desktop" / "观澜.lnk"))
    args = ap.parse_args()

    data = build_lnk()
    Path(args.out).write_bytes(data)
    print(f"已创建快捷方式 {args.out}  ({len(data)} bytes)")
    print(f"  目标   {TARGET}")
    print(f"  图标   {ICON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
