#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam Storage Explorer —— 扫描 Steam 游戏库，统计磁盘占用与游玩时长。

三种用法:

    python scan.py                 生成单文件 HTML 报告
    python scan.py --serve         启动本地服务，浏览器里自动检测 + 交互查看
    python scan.py --json a.json   导出原始数据

参数:
    --steam PATH   手动指定 Steam 目录（自动检测失败时用）
    -o FILE        报告输出路径
    --json FILE    导出 JSON
    --open         生成报告后自动打开浏览器
    --serve        启动本地 HTTP 服务（默认 http://127.0.0.1:8765）
    --port N       配合 --serve 指定端口

数据来源（全部只读，不启动 Steam，不联网）:
    <库>/steamapps/appmanifest_*.acf        每个游戏的 name / SizeOnDisk / LastPlayed
    <库>/steamapps/libraryfolders.vdf       所有游戏库的路径
    <Steam>/userdata/<账号>/config/localconfig.vdf   每个游戏的累计游玩时长（分钟）
    <Steam>/appcache/librarycache/<appid>/  游戏封面（header.jpg，客户端已有的缓存）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GB = 1024 ** 3
VERSION = "1.1.0"


# ---------------------------------------------------------------------------
# VDF (Valve Data Format) 解析 —— 只处理 key/value 与嵌套块
# ---------------------------------------------------------------------------
def _tokenize(text):
    """产出 'str' / '{' / '}' 标记，跳过 // 注释。"""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
            yield ("str", "".join(buf))
        else:
            start = i
            while i < n and text[i] not in " \t\r\n{}":
                i += 1
            yield ("str", text[start:i])


def parse_vdf(text):
    """VDF 文本 -> 嵌套 dict。"""
    tokens = list(_tokenize(text))

    def block(pos):
        out = {}
        key = None
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                return out, pos + 1
            if tok == "{":
                if key is not None:
                    out[key], pos = block(pos + 1)
                    key = None
                else:
                    pos += 1
            else:
                if key is None:
                    key = tok[1]
                else:
                    out[key] = tok[1]
                    key = None
                pos += 1
        return out, pos

    root, _ = block(0)
    return root


# ---------------------------------------------------------------------------
# 定位 Steam 目录
# ---------------------------------------------------------------------------
DEFAULT_CANDIDATES = [
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"D:\Steam",
    r"D:\SteamLibrary",
    r"E:\Steam",
    r"E:\SteamLibrary",
    r"F:\SteamLibrary",
    "~/Library/Application Support/Steam",                    # macOS
    "~/.steam/steam",                                         # Linux
    "~/.steam/root",                                          # Linux
    "~/.local/share/Steam",                                   # Linux / Steam Deck
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",  # Linux Flatpak
]

DISK_LETTERS = "CDEFGHIJKLMNOPQRSTUVWXYZ"

# 盘符根目录下这些文件夹值得再往里看一层（Steam 常被装进 Games 之类）
NESTED_PARENTS = ("games", "program files", "program files (x86)", "apps")


def _looks_like_steam(p: Path) -> bool:
    """有 steamapps 子目录就算一个 Steam 库。"""
    try:
        return (p / "steamapps").is_dir()
    except OSError:
        return False


def _has_manifests(p: Path) -> bool:
    """steamapps 下真的有游戏清单 —— 用来区分真库和 steamCMD 这类空壳。"""
    try:
        return any((p / "steamapps").glob("appmanifest_*.acf"))
    except OSError:
        return False


def _norm_root(p: Path) -> Path:
    """注册表里存的可能是 d:/steam，统一成 D:\\steam 的显示形式。"""
    s = str(p)
    if sys.platform == "win32" and len(s) > 1 and s[1] == ":":
        s = s[0].upper() + s[1:]
    return Path(s)


def _is_primary_library(p: Path) -> bool:
    """主库的 steamapps 下有 libraryfolders.vdf，附加库通常没有。"""
    try:
        return (p / "steamapps" / "libraryfolders.vdf").is_file()
    except OSError:
        return False


def _pick_root(found):
    """多个候选取一个：主库优先，其次路径短的（更像安装根目录）。"""
    if not found:
        return None
    return sorted(found, key=lambda x: (not _is_primary_library(x), len(str(x))))[0]


def _steam_from_registry():
    """从 Windows 注册表读安装路径。

    比扫盘可靠得多，而且直接走 winreg API，不依赖被安全策略限制的 reg.exe。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    targets = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
    ]
    for hive, key in targets:
        try:
            with winreg.OpenKey(hive, key) as k:
                for name in ("SteamPath", "InstallPath"):
                    try:
                        val = winreg.QueryValueEx(k, name)[0]
                    except OSError:
                        continue
                    if not val:
                        continue
                    p = _norm_root(Path(str(val)))
                    if _looks_like_steam(p):
                        return p
        except OSError:
            continue
    return None


def _candidates_from_drives():
    """扫盘：盘符根目录下名字含 steam 的文件夹，外加 Games / Program Files 往下一层。"""
    found = []
    for letter in DISK_LETTERS:
        root = Path(f"{letter}:/")
        try:
            if not root.is_dir():
                continue
            entries = list(root.iterdir())
        except OSError:
            continue

        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue

            low = entry.name.lower()
            if "steam" in low:
                if _looks_like_steam(entry):
                    found.append(entry)
            elif low in NESTED_PARENTS:
                try:
                    subs = list(entry.iterdir())
                except OSError:
                    continue
                for sub in subs:
                    try:
                        if sub.is_dir() and "steam" in sub.name.lower() and _looks_like_steam(sub):
                            found.append(sub)
                    except OSError:
                        continue
    return found


def find_steam_root():
    """按可靠性依次尝试：环境变量 → 注册表 → 常见路径 → 扫盘。

    返回 (Path 或 None, 来源说明字符串)。
    """
    for env in ("STEAM_PATH", "SteamPath", "STEAM_ROOT"):
        val = os.environ.get(env)
        if not val:
            continue
        p = _norm_root(Path(os.path.expanduser(val)))
        if _looks_like_steam(p):
            return p, f"环境变量 {env}"

    p = _steam_from_registry()
    if p:
        return p, "注册表"

    for cand in DEFAULT_CANDIDATES:
        p = _norm_root(Path(os.path.expanduser(cand)))
        if _looks_like_steam(p):
            return p, "常见安装路径"

    p = _pick_root(_candidates_from_drives())
    if p:
        return p, "扫盘"

    return None, None


# ---------------------------------------------------------------------------
# 读取游戏清单
# ---------------------------------------------------------------------------
def collect_library_paths(steam_root: Path):
    """主库 + libraryfolders.vdf 里登记的所有库，去重。"""
    libs, seen = [], set()

    def add(path_str):
        try:
            p = Path(path_str)
        except (OSError, ValueError):
            return
        key = os.path.normcase(os.path.abspath(str(p)).rstrip("\\/"))
        if key in seen or not (p / "steamapps").is_dir():
            return
        seen.add(key)
        libs.append(p)

    add(str(steam_root))

    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        try:
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for key, val in folders.items():
                if isinstance(val, dict):
                    if val.get("path"):
                        add(val["path"])
                elif isinstance(val, str) and key.isdigit():
                    add(val)  # 旧版格式: "1" "D:\\SteamLibrary"
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] libraryfolders.vdf 解析失败: {exc}", file=sys.stderr)

    return libs


def read_playtimes(steam_root: Path):
    """读取每个游戏的累计游玩时长（分钟）。

    数据在 userdata/<账号>/config/localconfig.vdf 的
    Software > Valve > Steam > apps > <appid> > Playtime 里。
    一个机器上可能有多个账号目录，同一游戏取最大值。
    """
    result = {}
    userdata = steam_root / "userdata"
    try:
        accounts = [d for d in userdata.iterdir() if d.is_dir()]
    except OSError:
        return result

    for account in accounts:
        cfg = account / "config" / "localconfig.vdf"
        if not cfg.is_file():
            continue
        try:
            data = parse_vdf(cfg.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue

        apps = (data.get("UserLocalConfigStore", {})
                    .get("Software", {})
                    .get("Valve", {})
                    .get("Steam", {})
                    .get("apps", {}))
        if not isinstance(apps, dict):
            continue

        for appid, info in apps.items():
            if not isinstance(info, dict):
                continue
            try:
                minutes = int(info.get("Playtime") or 0)
            except (TypeError, ValueError):
                continue
            try:
                key = int(appid)
            except (TypeError, ValueError):
                continue
            if minutes > result.get(key, 0):
                result[key] = minutes

    return result


def scan_library(lib_root: Path):
    """返回 (games, lib_info)。"""
    steamapps = lib_root / "steamapps"
    games = []

    for acf in sorted(steamapps.glob("appmanifest_*.acf")):
        try:
            raw = acf.read_text(encoding="utf-8", errors="replace")
            state = parse_vdf(raw).get("AppState")
        except Exception:  # noqa: BLE001 损坏/半写入的清单直接跳过
            continue
        if not isinstance(state, dict):
            continue

        try:
            appid = int(state.get("appid") or 0)
            size = int(state.get("SizeOnDisk") or 0)
        except (TypeError, ValueError):
            continue
        if appid <= 0 or size <= 0:
            continue

        def num(key):
            try:
                return int(state.get(key) or 0) or None
            except (TypeError, ValueError):
                return None

        games.append({
            "appid": appid,
            "name": (state.get("name") or "").strip() or f"App {appid}",
            "size": size,
            "playtime": 0,  # 分钟，稍后由 read_playtimes 回填
            "lastPlayed": num("LastPlayed"),
            "lastUpdated": num("LastUpdated"),
            "installdir": state.get("installdir") or "",
            "lib": str(lib_root),
        })

    total = free = None
    try:
        du = shutil.disk_usage(str(lib_root))
        total, free = du.total, du.free
    except OSError:
        pass

    return games, {
        "path": str(lib_root),
        "total": total,
        "free": free,
        "gameCount": len(games),
        "gameBytes": sum(g["size"] for g in games),
    }


# ---------------------------------------------------------------------------
# 组装数据
# ---------------------------------------------------------------------------
def build_payload(steam_root: Path):
    """扫描一个 Steam 根目录下的全部库，返回前端用的数据结构。"""
    libs = collect_library_paths(steam_root)
    if not libs:
        raise RuntimeError(f"{steam_root} 下没有找到任何游戏库")

    all_games, lib_infos = [], []
    for lib in libs:
        games, info = scan_library(lib)
        all_games.extend(games)
        lib_infos.append(info)

    if not all_games:
        raise RuntimeError("没有扫到任何游戏清单（appmanifest_*.acf）")

    playtimes = read_playtimes(steam_root)
    for g in all_games:
        g["playtime"] = playtimes.get(g["appid"], 0)

    total_bytes = sum(g["size"] for g in all_games)
    total_minutes = sum(g["playtime"] for g in all_games)
    all_games.sort(key=lambda g: -g["size"])

    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": VERSION,
        "steamRoot": str(steam_root),
        "libraries": lib_infos,
        "games": all_games,
        "totals": {
            "bytes": total_bytes,
            "playtimeMinutes": total_minutes,
            "count": len(all_games),
            "libraryCount": len(lib_infos),
        },
    }


# ---------------------------------------------------------------------------
# 游戏封面：优先取 Steam 客户端已经缓存好的图，本地没有才退到官方 CDN
# ---------------------------------------------------------------------------
# 卡片墙里的卡片是横的，只认横版 header。竖版封面（library_600x900，300x450）放进横块里
# 等比缩放后左右会空出一大片，看着像贴错了，所以只当最后的兜底。
LANDSCAPE_PATTERNS = ("header.jpg", "header_*.jpg")
PORTRAIT_PATTERNS = ("library_600x900.jpg", "library_600x900_*.jpg")
COVER_PATTERNS = LANDSCAPE_PATTERNS + PORTRAIT_PATTERNS
MAX_COVER_BYTES = 320 * 1024

# Steam 官方静态资源，公开、无需 key、带 Access-Control-Allow-Origin: *
# 本地 appcache 被清过、或者只选了附加库的时候靠它兜底，不至于整页没有封面
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"


def find_cover(steam_root, appid, landscape_only=True):
    """在 appcache/librarycache/<appid>/ 下找一张封面，找不到返回 None。

    landscape_only=True 时只看横版 header；本地缓存里常常只有竖版封面，
    那就交给 CDN 去取横版，免得横块里塞一张竖图。
    """
    if not steam_root:
        return None
    cache = Path(steam_root) / "appcache" / "librarycache" / str(appid)
    try:
        if not cache.is_dir():
            return None
    except OSError:
        return None

    patterns = LANDSCAPE_PATTERNS if landscape_only else COVER_PATTERNS
    for pattern in patterns:
        for p in sorted(cache.glob(pattern)):
            try:
                if p.is_file() and 1024 < p.stat().st_size <= MAX_COVER_BYTES:
                    return p
            except OSError:
                continue
    return None


def attach_covers(payload, steam_root, mode, allow_cdn=True):
    """把封面挂到每个游戏上。

    mode='inline' 把本地横版 header 塞成 base64，报告离线也能显示；
    mode='url'    交给本地服务的 /api/cover/<appid>，页面按需加载。

    本地缓存里常常只有竖版封面（library_600x900，300x450），塞进横着的卡片里
    等比缩放后左右会空一大片，所以不内联竖版，改指向 CDN 上的横版 header。
    返回本地内联成功的游戏数。
    """
    if mode not in ("inline", "url"):
        return 0

    hits = 0
    for game in payload.get("games", []):
        appid = game["appid"]
        path = find_cover(steam_root, appid)   # 只看横版

        if not path:
            if allow_cdn:
                game["cover"] = STEAM_CDN + f"/{appid}/header.jpg"
            continue

        if mode == "url":
            game["cover"] = "/api/cover/%d" % appid
            hits += 1
            continue

        try:
            raw = path.read_bytes()
        except OSError:
            continue
        game["cover"] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        hits += 1

    return hits


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def human(n):
    if n is None:
        return "-"
    for label, base in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= base:
            return f"{n / base:.1f} {label}"
    return f"{n} B"


def human_minutes(m):
    if not m:
        return "-"
    h = m / 60
    if h < 1:
        return f"{m} 分钟"
    if h < 10:
        return f"{h:.1f} 小时"
    return f"{h:.0f} 小时"


# ---------------------------------------------------------------------------
# 生成单文件报告
# ---------------------------------------------------------------------------
def build_html(payload) -> str:
    """把 CSS / JS / 数据内联进报告骨架，产出单文件 HTML。"""
    tpl_path = ROOT / "templates" / "report.html"
    css_path = ROOT / "web" / "style.css"
    js_path = ROOT / "web" / "app.js"
    for p in (tpl_path, css_path, js_path):
        if not p.is_file():
            raise FileNotFoundError(f"缺少文件: {p}")

    tpl = tpl_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")
    js = js_path.read_text(encoding="utf-8")

    # 防止 JSON / JS 里的 </script> 提前闭合标签
    data_js = "const DATA = " + json.dumps(payload, ensure_ascii=False) + ";"
    data_js = data_js.replace("</", "<\\/")
    js = js.replace("</script", "<\\/script")

    out = tpl.replace("/*__STYLE__*/", css)
    out = out.replace("/*__DATA__*/", data_js)
    out = out.replace("/*__APP__*/", js)
    return out


# ---------------------------------------------------------------------------
# 本地服务模式：页面打开就能自动检测
# ---------------------------------------------------------------------------
def serve(port=8765, host="127.0.0.1", open_browser=True, steam_hint=None,
          allow_cdn=True):
    """起一个只监听本机的 HTTP 服务，供 web/index.html 调用。

    网页本身无权读磁盘，自动检测必须由这个本地进程代劳。
    """
    import http.server
    import urllib.parse

    web_dir = ROOT / "web"

    # 封面只存在于主 Steam 目录的 appcache 下，附加库没有。
    # 所以判据是「这个目录里有没有 appcache」，而不是「有没有被指定过」——
    # 否则 --steam 指到 SteamLibrary 时会一直返回附加库路径，永远找不到封面。
    cover_state = {"path": None}

    def cover_root(preferred=None):
        for cand in (preferred, cover_state["path"]):
            if not cand:
                continue
            try:
                if (Path(cand) / "appcache").is_dir():
                    cover_state["path"] = Path(cand)
                    return cover_state["path"]
            except OSError:
                pass
        found, _ = find_steam_root()
        cover_state["path"] = found
        return found

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(web_dir), **kw)

        def log_message(self, fmt, *args):
            if "/api/" in self.path:
                sys.stderr.write("  %s\n" % (fmt % args))

        # ---- 工具 ----
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ---- 业务 ----
        def _detect(self):
            root, source = (Path(steam_hint), "--steam 指定") if steam_hint else find_steam_root()
            if not root:
                return {"ok": False, "error": "没能自动定位 Steam 目录"}
            try:
                payload = build_payload(root)
                attach_covers(payload, cover_root(root), "url", allow_cdn)
                return {"ok": True, "root": str(root), "source": source, "data": payload}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "root": str(root), "error": str(exc)}

        def _scan(self, raw_path):
            if not raw_path:
                return {"ok": False, "error": "缺少 path 参数"}
            p = Path(raw_path)
            if not p.is_dir():
                return {"ok": False, "error": f"不是一个存在的目录：{raw_path}"}
            try:
                payload = build_payload(p)
                attach_covers(payload, cover_root(p), "url", allow_cdn)
                return {"ok": True, "root": str(p), "data": payload}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

        def _cover(self, appid):
            """按 appid 回一张本地缓存的封面，避免把这 1.5 MB 塞进 JSON。"""
            path = find_cover(cover_root(), appid)
            if not path:
                return self._json({"ok": False, "error": "本地没有这张封面"}, 404)
            try:
                raw = path.read_bytes()
            except OSError as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(raw)
            return None

        def _browse(self, raw_path):
            """列出目录，供页面里的路径选择器使用。"""
            if not raw_path:
                roots = []
                if sys.platform == "win32":
                    for letter in DISK_LETTERS:
                        d = Path(f"{letter}:/")
                        try:
                            if d.is_dir():
                                roots.append({"name": f"{letter}:\\", "path": str(d),
                                              "isSteam": _looks_like_steam(d)})
                        except OSError:
                            continue
                else:
                    roots.append({"name": "/", "path": "/", "isSteam": False})
                return {"ok": True, "path": "", "parent": None, "dirs": roots}

            p = Path(raw_path)
            if not p.is_dir():
                return {"ok": False, "error": f"不是一个存在的目录：{raw_path}"}

            dirs = []
            try:
                for entry in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                    try:
                        if not entry.is_dir() or entry.name.startswith("$"):
                            continue
                    except OSError:
                        continue
                    dirs.append({
                        "name": entry.name,
                        "path": str(entry),
                        # 只把真的有游戏清单的目录标成 Steam 库（steamCMD 之类也有 steamapps）
                        "isSteam": _looks_like_steam(entry) and _has_manifests(entry),
                    })
            except OSError as exc:
                return {"ok": False, "error": str(exc)}

            dirs.sort(key=lambda d: (not d["isSteam"], d["name"].lower()))
            parent = str(p.parent) if p.parent != p else None
            return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs}

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            route, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            try:
                if route == "/api/detect":
                    return self._json(self._detect())
                if route == "/api/scan":
                    return self._json(self._scan(qs.get("path", [""])[0]))
                if route == "/api/browse":
                    return self._json(self._browse(qs.get("path", [""])[0]))
                if route == "/api/version":
                    return self._json({"ok": True, "version": VERSION})
                if route.startswith("/api/cover/"):
                    appid = route[len("/api/cover/"):]
                    if not appid.isdigit():
                        return self._json({"ok": False, "error": "appid 不合法"}, 400)
                    return self._cover(int(appid))
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc)}, 500)
            return super().do_GET()

    server = None
    for candidate in range(port, port + 12):
        try:
            server = http.server.ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print(f"[错误] {host}:{port} 附近的端口都占用了，用 --port 换一个。", file=sys.stderr)
        return 2

    url = f"http://{host}:{port}/"
    print(f"Steam Storage Explorer 已启动: {url}")
    print("页面打开会自动检测 Steam 目录；按 Ctrl+C 停止。\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="扫描 Steam 游戏库，可视化磁盘占用与游玩时长",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--steam", help=r"Steam 根目录，例如 D:\Steam")
    ap.add_argument("-o", "--output", default=str(ROOT / "steam_storage_report.html"),
                    help="报告输出路径")
    ap.add_argument("--json", help="导出原始 JSON 到该路径")
    ap.add_argument("--open", action="store_true", help="生成报告后自动打开浏览器")
    ap.add_argument("--no-cdn", action="store_true",
                    help="本地没有封面时不去官方 CDN 取，只显示底色")
    ap.add_argument("--no-covers", action="store_true",
                    help="报告里不内联游戏封面（体积可减约 1.5 MB）")
    ap.add_argument("--serve", action="store_true", help="启动本地服务，在浏览器里交互查看")
    ap.add_argument("--port", type=int, default=8765, help="配合 --serve 指定端口")
    ap.add_argument("--no-browser", action="store_true", help="配合 --serve 不自动弹浏览器")
    args = ap.parse_args(argv)

    if args.serve:
        return serve(port=args.port, open_browser=not args.no_browser,
                 steam_hint=args.steam, allow_cdn=not args.no_cdn)

    if args.steam:
        steam_root, source = Path(args.steam), "--steam 指定"
    else:
        steam_root, source = find_steam_root()

    if not steam_root or not _looks_like_steam(steam_root):
        print("[错误] 没找到 Steam 目录。", file=sys.stderr)
        print("已尝试: 环境变量 STEAM_PATH → Windows 注册表 → 常见安装路径 → 扫盘符", file=sys.stderr)
        print("手动指定一下，例如：", file=sys.stderr)
        print(r'  python scan.py --steam "D:\Steam"', file=sys.stderr)
        print("路径在 Steam 客户端里查：设置 → 存储 → 点齿轮 → 本地文件路径", file=sys.stderr)
        return 2

    payload = build_payload(steam_root)
    print(f"Steam 目录: {steam_root}   （来源: {source}）")
    print(f"发现 {len(payload['libraries'])} 个游戏库\n")
    for lib in payload["libraries"]:
        print(f"  {lib['path']:<34} {lib['gameCount']:>3} 个游戏   {human(lib['gameBytes']):>9}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # JSON 先写，此时还没有内联封面，导出的数据文件不会膨胀
    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")

    cover_hits = 0
    if not args.no_covers:
        cover_hits = attach_covers(payload, steam_root, "inline", allow_cdn=not args.no_cdn)

    out_path.write_text(build_html(payload), encoding="utf-8")

    totals = payload["totals"]
    print(f"\n共 {totals['count']} 个游戏，合计 {human(totals['bytes'])}，"
          f"累计游玩 {human_minutes(totals['playtimeMinutes'])}")
    print("占用 Top 5:")
    for g in payload["games"][:5]:
        print(f"  {human(g['size']):>10}  {human_minutes(g['playtime']):>10}  {g['name']}")

    if not args.no_covers:
        print(f"封面: {cover_hits}/{totals['count']} 个游戏已内联")
    print(f"\n报告: {out_path}")
    if args.open:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
