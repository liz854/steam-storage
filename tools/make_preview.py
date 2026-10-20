#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 README 用的 SVG 预览图。

    python tools/make_preview.py                       # 用 test/fixture.json 的合成数据
    python tools/make_preview.py --data data.json      # 用自己的扫描结果
    python tools/make_preview.py --no-covers           # 不联网，只画纯色块
"""

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUES = [212, 276, 160, 28, 340, 196]
GB = 1024 ** 3

sys.path.insert(0, str(ROOT))
from scan import find_cover  # noqa: E402  复用同一套封面查找逻辑

# 与 web/app.js 保持一致：小于这个尺寸的卡片铺图只会糊成一片
MIN_COVER_W, MIN_COVER_H = 68, 44
MAX_COVER_BYTES = 320 * 1024
# 卡片朝 Steam 横向封面（header.jpg）的比例去排，封面就能正好填满
TARGET_AR = 460 / 215

# 本地缓存里常常只有竖版封面，预览图又不能像网页那样按需联网加载，
# 所以在构建时补一张 CDN 上的横版 header 内联进来（失败就退回纯色块）。
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"


def cdn_cover(appid, timeout=5.0):
    try:
        req = urllib.request.Request(
            f"{STEAM_CDN}/{appid}/header.jpg",
            headers={"User-Agent": "steam-storage-visualizer"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_COVER_BYTES + 1)
    except Exception:
        return None
    if raw[:2] != b"\xff\xd8" or len(raw) > MAX_COVER_BYTES:
        return None
    return raw


def layout_flow(items, W, H):
    """与 web/app.js 的 layoutFlow 一致：卡片流。

    卡片长宽比锁死封面比例，宽 ∝ sqrt(占用)，按行往下排，行尾自然留空；
    行内垂直居中，画布高度跟着内容走。返回 (rects, 实际高度)。
    """
    if not items or W <= 0:
        return [], 0.0
    total = sum(g["size"] for g in items)
    if total <= 0:
        return [], 0.0

    nodes = [{"g": g, "a": g["size"] / total} for g in items]
    min_w = 96.0                      # 再窄就放不下名字
    max_w = max(min_w, W * 0.55)      # 再宽就只剩它一块

    def run(c):
        out, x, y, row_h, row = [], 0.0, 0.0, 0.0, []
        for n in nodes:
            w = c * (n["a"] ** 0.5)
            w = min(max_w, max(min_w, w))
            w = float(int(w + 0.5))   # 和 JS 的 Math.round 对齐
            h = w / TARGET_AR
            if x > 0 and x + w > W + 0.5:
                for t in row:
                    t["y"] = y + (row_h - t["h"]) / 2
                    out.append(t)
                y += row_h
                x, row_h, row = 0.0, 0.0, []
            row.append({"g": n["g"], "x": x, "y": 0.0, "w": w, "h": h})
            x += w
            row_h = max(row_h, h)
        if row:
            for t in row:
                t["y"] = y + (row_h - t["h"]) / 2
                out.append(t)
            y += row_h
        return out, y

    lo, hi, best = 1.0, W * 2.0, None
    for _ in range(34):
        mid = (lo + hi) / 2
        rects, hh = run(mid)
        if hh > H:
            hi = mid
        else:
            lo = mid
            best = (rects, hh)
    return best if best else run(lo)


def human(n):
    if n >= GB:
        return f"{n / GB:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024:.0f} KB"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_svg(data, W=1200, H=620, steam_root=None):
    games = sorted(data["games"], key=lambda g: -g["size"])
    libs = [l["path"] for l in data["libraries"]]
    lib_hue = {p: HUES[i % len(HUES)] for i, p in enumerate(libs)}
    lib_total = {}
    for g in games:
        lib_total[g["lib"]] = lib_total.get(g["lib"], 0) + 1

    gap = 3.0  # 卡片之间的间隙；太大会把极小的游戏挤没
    rects, used_h = layout_flow(games, W, H)
    # 画布高度跟着内容走，底部不留空
    H = max(160, int(round(used_h))) if rects else H

    # 封面按需内联：只收够大的卡片会用到的那些，避免把 SVG 撑到几 MB
    covers = {}
    if steam_root:
        for g in games:
            path = find_cover(steam_root, g["appid"])
            raw = None
            if path:
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = None
            if raw is None:
                raw = cdn_cover(g["appid"])
            if raw and len(raw) <= MAX_COVER_BYTES:
                covers[g["appid"]] = base64.b64encode(raw).decode("ascii")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="Segoe UI,PingFang SC,Microsoft YaHei,sans-serif">',
        '<defs><linearGradient id="shade" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#000" stop-opacity="0"/>'
        '<stop offset="0.45" stop-color="#000" stop-opacity="0.12"/>'
        '<stop offset="1" stop-color="#000" stop-opacity="0.86"/>'
        '</linearGradient></defs>',
        f'<rect width="{W}" height="{H}" fill="#0a0d13"/>',
    ]

    counter = {}
    for r in rects:
        g = r["g"]
        x, y = r["x"] + gap / 2, r["y"] + gap / 2
        w, h = max(0.0, r["w"] - gap), max(0.0, r["h"] - gap)
        if w < 1.5 or h < 1.5:
            continue

        idx = counter.get(g["lib"], 0)
        counter[g["lib"]] = idx + 1
        n = lib_total.get(g["lib"], 1)
        t = idx / (n - 1) if n > 1 else 0
        hue = lib_hue.get(g["lib"], 212)
        color = f"hsl({hue},{62 - t * 16:.0f}%,{52 - t * 15:.0f}%)"

        b64 = covers.get(g["appid"]) if (w >= MIN_COVER_W and h >= MIN_COVER_H) else None

        if b64:
            cid = f"cv{g['appid']}"
            # 底色压暗一档：封面等比缩放后剩的那点空隙露出它，不跟图片抢视线，
            # 图片挂掉时也还能看清卡片边界。
            dark = f"hsl({hue},{62 - t * 16:.0f}%,{max(10, 27 - t * 12):.0f}%)"
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                f'rx="4" fill="{dark}"/>')
            parts.append(
                f'<clipPath id="{cid}"><rect x="{x:.1f}" y="{y:.1f}" '
                f'width="{w:.1f}" height="{h:.1f}" rx="4"/></clipPath>')
            # 主体封面：meet = 等比缩放把整张图塞进方格，不裁切也不拉伸。
            # （早先用的是 slice，等于 cover，会把 460×215 的封面两头切掉）
            parts.append(
                f'<image x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                f'preserveAspectRatio="xMidYMid meet" clip-path="url(#{cid})" '
                f'href="data:image/jpeg;base64,{b64}"/>')
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                f'rx="4" fill="url(#shade)"/>')
        else:
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                f'rx="4" fill="{color}"/>')

        if w > 66 and h > 34:
            fs = max(11, min(15, (w * h) ** 0.5 / 6.5))
            name = g["name"]
            limit = max(6, int(w / (fs * 0.62)))
            if len(name) > limit:
                name = name[: max(3, limit - 1)] + "…"

            # 有封面时文字压到底部，别挡住图片主体
            ty_name = (y + h - 24) if b64 else (y + 9 + fs)
            parts.append(
                f'<text x="{x + 9:.1f}" y="{ty_name:.1f}" fill="#fff" '
                f'font-size="{fs:.1f}" font-weight="600">{esc(name)}</text>')
            if w > 58 and h > 52:
                parts.append(
                    f'<text x="{x + 9:.1f}" y="{y + h - 10:.1f}" '
                    f'fill="rgba(255,255,255,.75)" font-size="11" '
                    f'font-family="Consolas,monospace">{human(g["size"])}</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    # 默认用仓库里的合成数据，保证预览图里不会混进任何人真实装了哪些游戏、占多大。
    # 想看自己库的实际效果就显式传 --data data.json。
    ap.add_argument("--data", default=str(ROOT / "test" / "fixture.json"),
                    help="扫描结果 JSON，默认用 test/fixture.json 的合成数据")
    ap.add_argument("-o", "--output", default=str(ROOT / "docs" / "preview.svg"))
    ap.add_argument("--no-covers", action="store_true",
                    help="只画纯色块，不嵌封面（不联网）")
    args = ap.parse_args()

    data_path = Path(args.data)
    if not data_path.is_file():
        print(f"[错误] 找不到 {data_path}。先运行: python scan.py --json data.json",
              file=sys.stderr)
        return 1

    data = json.loads(data_path.read_text(encoding="utf-8"))
    svg = build_svg(data, steam_root=None if args.no_covers else data.get("steamRoot"))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(f"预览图: {out}  ({len(data['games'])} 个游戏)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
