"""重建 观澜.url：先删旧（图标缓存与旧文件绑定），写全新文件。
指向 launch.vbs 而不是 start.cmd —— .cmd 会先弹一个黑框，.vbs 由 wscript
静默执行，双击只有应用窗口。
IconFile 用正斜杠形式（.url 的 IconFile 接受正斜杠，避开转义歧义）。

路径全部按本脚本所在位置推算：程序目录搬到哪个盘、改成什么名字，
重建出来的快捷方式都指向当前这份程序，不会再写死旧机器的绝对路径。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VBS = ROOT / "launch.vbs"
ICO = ROOT / "app" / "guanlan.ico"

for item in (VBS, ICO):
    if not item.is_file():
        raise SystemExit(f"缺少 {item}，快捷方式没有重建")

home = Path.home()
desktop = next(
    (d for d in (home / "Desktop", home / "OneDrive" / "Desktop",
                 home / "OneDrive" / "桌面", home / "桌面") if d.is_dir()),
    home / "Desktop",
)
NAME = desktop / "观澜.url"

if NAME.exists():
    NAME.unlink()
    print("旧 观澜.url 已删除")

content = (
    "[InternetShortcut]\r\n"
    f"URL=file:///{VBS.as_posix()}\r\n"
    "IconIndex=0\r\n"
    f"IconFile={ICO.as_posix()}\r\n"
)
# 不指定编码，跟系统 .url 解析保持一致；内容本身是纯 ASCII，搬去哪都不会乱码
with NAME.open("w", newline="") as fh:
    fh.write(content)
print("已重建:", NAME, NAME.stat().st_size, "bytes")
print(repr(NAME.read_bytes()))
