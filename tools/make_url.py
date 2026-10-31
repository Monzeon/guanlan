"""生成桌面 .url 快捷方式（纯文本 INI，字节级精确）。"""
content = (
    "[InternetShortcut]\r\n"
    "URL=file:///E:/WORKbuddy/2026-10-31-20-29-37/guanlan/start.cmd\r\n"
    "IconIndex=0\r\n"
    "IconFile=E:" + chr(92) + "WORKbuddy" + chr(92) + "2026-10-31-20-29-37"
    + chr(92) + "guanlan" + chr(92) + "app" + chr(92) + "guanlan.ico\r\n"
)
p = "C:/Users/" + chr(37073) + chr(32768) + chr(38451) + "/Desktop/" + chr(35266) + chr(28689) + ".url"
open(p, "w", newline="").write(content)
print(repr(open(p, "rb").read()))
