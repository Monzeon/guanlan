#!/usr/bin/env python
"""解剖任意 .lnk 的全字段结构（对照用诊断工具）。"""
import struct
import sys

path = sys.argv[1]
d = open(path, "rb").read()
print("总长", len(d))
hsize, clsid, flags, attr = struct.unpack("<I16sII", d[:28])
t1, t2, t3 = struct.unpack("<QQQ", d[28:52])
fsize, icon_idx, show, hot = struct.unpack("<IIHH", d[52:64])
print(f"HeaderSize {hsize:#x} | Flags {flags:#010x} | Attr {attr:#x}")
print(f"时间戳非零: {t1 != 0}, {t2 != 0}, {t3 != 0} | FileSize {fsize} | IconIndex {icon_idx} | Show {show}")

off = hsize
if flags & 0x01:
    li_size, = struct.unpack("<I", d[off:off + 4])
    hs, lf, vo, lbo, cnrl, cso = struct.unpack("<IIIIII", d[off + 4:off + 28])
    print(f"LinkInfo size={li_size} hdr={hs:#x} flags={lf:#x} vol={vo:#x} lbp={lbo:#x} cnrl={cnrl} cso={cso}")
    if lbo:
        end = d.index(b"\x00", off + lbo)
        print("  LBP:", d[off + lbo:end].decode("mbcs", "replace"))
    if cso:
        end = d.index(b"\x00", off + cso)
        print("  Suffix:", repr(d[off + cso:end]))
    off += li_size

print("StringData 起始:", off)
p = off
names = ["RELATIVE", "WORKDIR", "ICON", "?4", "?5"]
i = 0
while p < len(d) - 4:
    if d[p:p + 4] == b"\x00\x00\x00\x00":
        print("  终止块 @", p, "| 之后 ExtraData:", len(d) - p - 4, "字节")
        break
    n = int.from_bytes(d[p:p + 2], "little")
    if n == 0 or p + 2 + n * 2 > len(d):
        print("  (异常终止)", n)
        break
    s = d[p + 2:p + 2 + n * 2].decode("utf-16-le", "replace").rstrip("\x00")
    tag = names[i] if i < len(names) else "?"
    print(f"  STR[{tag}] @{p}: {s[:80]!r}")
    p += 2 + n * 2
    i += 1
else:
    print("  (无终止块到达文件尾)")
