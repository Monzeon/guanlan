"""重建 观澜.url：先删旧（图标缓存与旧文件绑定），写全新文件。
IconFile 用正斜杠形式（.url 的 IconFile 接受正斜杠，避开转义歧义）。"""
import os

DESKTOP = "C:/Users/" + chr(37073) + chr(32768) + chr(38451) + "/Desktop"
NAME = DESKTOP + "/" + chr(35266) + chr(28572) + ".url"

if os.path.exists(NAME):
    os.remove(NAME)
    print("旧 观澜.url 已删除")

ICO = "E:/WORKbuddy/2026-10-31-20-29-37/guanlan/app/guanlan.ico"
content = (
    "[InternetShortcut]\r\n"
    "URL=file:///E:/WORKbuddy/2026-10-31-20-29-37/guanlan/start.cmd\r\n"
    "IconIndex=0\r\n"
    "IconFile=" + ICO + "\r\n"
)
open(NAME, "w", newline="").write(content)
print("已重建:", NAME, os.path.getsize(NAME), "bytes")
print(repr(open(NAME, "rb").read()))
