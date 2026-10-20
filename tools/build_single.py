#!/usr/bin/env python3
"""把 web/ 下的 HTML / CSS / JS 压成一个可以双击打开的单文件页面。

产物 dist/steam-storage.html 不依赖网络、不依赖 Python 运行时，
拷贝到任何一台装了 Chrome / Edge 的机器上双击就能用。
"""
from __future__ import annotations

import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUT = ROOT / "dist" / "steam-storage.html"


def build() -> pathlib.Path:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")

    # 数据或字符串里的 </script> 会提前闭合标签
    js = js.replace("</script", "<\\/script")

    link_tag = '<link rel="stylesheet" href="style.css">'
    if link_tag not in html:
        raise SystemExit("index.html 里找不到 style.css 的引用，构建中止")
    html = html.replace(link_tag, "<style>\n" + css + "\n</style>")

    script_tag = '<script src="app.js"></script>'
    if script_tag not in html:
        raise SystemExit("index.html 里找不到 app.js 的引用，构建中止")
    html = html.replace(script_tag, "<script>\n" + js + "\n</script>")

    leftover = re.findall(r'(?:src|href)="(?!data:|#|https?:)[^"]+\.(?:js|css)(?:\?[^"]*)?"', html)
    if leftover:
        raise SystemExit("仍有外部资源引用，产物不是自包含的: %s" % leftover)

    if "<style>" not in html or html.count("<script>") < 1:
        raise SystemExit("内联结果异常，构建中止")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 固定用 LF：Windows 上默认会写成 CRLF，和 CI（Linux）产出的字节不同，
    # 会让「产物是否同步」的检查误报。
    with open(OUT, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(html)
    return OUT


def main() -> int:
    path = build()
    size = path.stat().st_size
    print("已生成 %s（%.1f KB）" % (path.relative_to(ROOT).as_posix(), size / 1024))
    print("双击打开即可使用，无需安装 Python")
    return 0


if __name__ == "__main__":
    sys.exit(main())
