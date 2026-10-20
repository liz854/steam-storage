# -*- coding: utf-8 -*-
"""扫描器单元测试：python -m unittest discover -s test -p "test_*.py" """

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan  # noqa: E402


class TestVdf(unittest.TestCase):
    def test_flat(self):
        txt = '"AppState"\n{\n\t"appid"\t\t"730"\n\t"name"\t\t"Counter-Strike 2"\n}'
        self.assertEqual(scan.parse_vdf(txt)["AppState"]["name"], "Counter-Strike 2")

    def test_nested(self):
        txt = '''
        "libraryfolders"
        {
            "0"
            {
                "path" "D:\\\\steam"
                "apps" { "730" "73918888730" }
            }
        }
        '''
        root = scan.parse_vdf(txt)["libraryfolders"]["0"]
        self.assertEqual(root["path"], "D:\\steam")
        self.assertEqual(root["apps"]["730"], "73918888730")

    def test_comment_and_escapes(self):
        txt = '"root" { "a" "x//not-comment" // 真注释\n "b" "q\\"q" }'
        root = scan.parse_vdf(txt)["root"]
        self.assertEqual(root["a"], "x//not-comment")
        self.assertEqual(root["b"], 'q"q')

    def test_malformed_does_not_raise(self):
        scan.parse_vdf('"a" { "b" "c"')  # 缺右括号也应安全返回


class TestHuman(unittest.TestCase):
    def test_units(self):
        self.assertEqual(scan.human(1024 ** 3), "1.0 GB")
        self.assertEqual(scan.human(1024 ** 4), "1.0 TB")
        self.assertEqual(scan.human(1024 ** 2 * 512), "512.0 MB")
        self.assertEqual(scan.human(900), "900 B")
        self.assertEqual(scan.human(None), "-")


class TestScanLibrary(unittest.TestCase):
    def _make_lib(self, tmp, appid, name, size, played=0):
        steamapps = Path(tmp) / "steamapps"
        steamapps.mkdir(parents=True, exist_ok=True)
        (steamapps / f"appmanifest_{appid}.acf").write_text(f'''
"AppState"
{{
    "appid"      "{appid}"
    "name"       "{name}"
    "SizeOnDisk" "{size}"
    "LastPlayed" "{played}"
    "installdir" "{name}"
}}
''', encoding="utf-8")
        return Path(tmp)

    def test_reads_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = self._make_lib(tmp, 730, "Counter-Strike 2", 73918888730, 1787664875)
            games, info = scan.scan_library(lib)
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["name"], "Counter-Strike 2")
            self.assertEqual(games[0]["size"], 73918888730)
            self.assertEqual(games[0]["lastPlayed"], 1787664875)
            self.assertEqual(info["gameCount"], 1)
            self.assertEqual(info["gameBytes"], 73918888730)

    def test_skips_zero_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = self._make_lib(tmp, 1, "Broken", 0)
            games, _ = scan.scan_library(lib)
            self.assertEqual(games, [])

    def test_skips_corrupt_acf(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = self._make_lib(tmp, 2, "OK", 1024)
            (lib / "steamapps" / "appmanifest_3.acf").write_text("garbage", encoding="utf-8")
            games, _ = scan.scan_library(lib)
            self.assertEqual(len(games), 1)

    def test_multi_library_discovery(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            root = self._make_lib(a, 10, "A", 1024)
            lib2 = self._make_lib(b, 20, "B", 2048)
            vdf = root / "steamapps" / "libraryfolders.vdf"
            vdf.write_text(
                '"libraryfolders"\n{\n "0"\n {\n  "path" "%s"\n }\n'
                ' "1"\n {\n  "path" "%s"\n }\n}\n'
                % (str(root).replace("\\", "\\\\"), str(lib2).replace("\\", "\\\\")),
                encoding="utf-8")
            libs = scan.collect_library_paths(root)
            self.assertEqual(len(libs), 2)


class TestDiscovery(unittest.TestCase):
    """自动定位 Steam 目录的判定逻辑。"""

    def test_looks_like_steam(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(scan._looks_like_steam(Path(tmp)))
            (Path(tmp) / "steamapps").mkdir()
            self.assertTrue(scan._looks_like_steam(Path(tmp)))

    def test_primary_library_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            sa = Path(tmp) / "steamapps"
            sa.mkdir()
            self.assertFalse(scan._is_primary_library(Path(tmp)))
            (sa / "libraryfolders.vdf").write_text('"libraryfolders"{}', encoding="utf-8")
            self.assertTrue(scan._is_primary_library(Path(tmp)))

    def test_pick_root_prefers_primary_library(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as primary, tf.TemporaryDirectory() as extra:
            for d, name in ((primary, "steamapps"), (extra, "steamapps")):
                (Path(d) / name).mkdir()
            (Path(primary) / "steamapps" / "libraryfolders.vdf").write_text(
                '"libraryfolders"{}', encoding="utf-8")
            # 主库路径更长，但仍应优先
            picked = scan._pick_root([Path(extra), Path(primary)])
            self.assertEqual(str(picked), str(Path(primary)))

    def test_pick_root_shortest_when_no_primary(self):
        self.assertIsNone(scan._pick_root([]))

    def test_norm_root(self):
        p = scan._norm_root(Path("d:/steam"))
        s = str(p)
        if sys.platform == "win32":
            self.assertTrue(s.startswith("D:"), s)
        else:
            self.assertIn("steam", s)


class TestCover(unittest.TestCase):
    """封面来自 Steam 客户端自己的缓存：appcache/librarycache/<appid>/"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _put(self, appid, name, size=4096):
        d = self.root / "appcache" / "librarycache" / str(appid)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(b"\xff\xd8\xff" + b"x" * max(0, size - 3))
        return p

    def test_finds_header(self):
        self._put(730, "header.jpg")
        self.assertIsNotNone(scan.find_cover(self.root, 730))

    def test_missing_appid(self):
        self.assertIsNone(scan.find_cover(self.root, 999999))
        self.assertIsNone(scan.find_cover(None, 730))

    def test_prefers_plain_header(self):
        self._put(730, "library_600x900.jpg")
        self._put(730, "header_schinese.jpg")
        self._put(730, "header.jpg")
        self.assertEqual(scan.find_cover(self.root, 730).name, "header.jpg")

    def test_falls_back_to_localized(self):
        self._put(730, "header_schinese.jpg")
        self.assertEqual(scan.find_cover(self.root, 730).name, "header_schinese.jpg")

    def test_skips_tiny_placeholder(self):
        """低于 1 KB 的多半是占位图，铺上去比纯色还难看。"""
        self._put(730, "header.jpg", size=600)
        self.assertIsNone(scan.find_cover(self.root, 730))

    def test_skips_oversized(self):
        self._put(730, "header.jpg", size=scan.MAX_COVER_BYTES + 1)
        self.assertIsNone(scan.find_cover(self.root, 730))

    def test_ignores_hero_image(self):
        """library_hero 是 1920x620 的横幅，塞进卡片里全是模糊色块。"""
        self._put(730, "library_hero.jpg")
        self.assertIsNone(scan.find_cover(self.root, 730))

    def _payload(self):
        return {"games": [
            {"appid": 730, "name": "CS2", "size": 100},
            {"appid": 570, "name": "Dota 2", "size": 200},
        ]}

    def test_attach_inline(self):
        self._put(730, "header.jpg")
        payload = self._payload()
        hits = scan.attach_covers(payload, self.root, "inline", allow_cdn=False)

        self.assertEqual(hits, 1)
        self.assertTrue(payload["games"][0]["cover"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("cover", payload["games"][1])

    def test_attach_url(self):
        self._put(570, "header.jpg")
        payload = self._payload()
        hits = scan.attach_covers(payload, self.root, "url", allow_cdn=False)

        self.assertEqual(hits, 1)
        self.assertEqual(payload["games"][1]["cover"], "/api/cover/570")
        self.assertNotIn("cover", payload["games"][0])

    def test_cdn_fills_missing_local_cover(self):
        """本地缓存没命中（appcache 被清过、或选了附加库）时用官方 CDN 兜底，
        否则别人机器上整页都没有封面。"""
        self._put(730, "header.jpg")
        payload = self._payload()
        hits = scan.attach_covers(payload, self.root, "inline")

        self.assertEqual(hits, 1, "命中数只统计本地，CDN 不算")
        self.assertTrue(payload["games"][0]["cover"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(payload["games"][1]["cover"],
                         scan.STEAM_CDN + "/570/header.jpg")

    def test_cdn_can_be_disabled(self):
        payload = self._payload()
        scan.attach_covers(payload, self.root / "nope", "inline", allow_cdn=False)
        self.assertTrue(all("cover" not in g for g in payload["games"]))

    def test_cdn_url_is_public_static(self):
        """CDN 地址必须指向 Steam 的公开静态资源，不能带 key 或私有域名。"""
        self.assertTrue(scan.STEAM_CDN.startswith("https://"))
        self.assertIn("steamstatic.com", scan.STEAM_CDN)
        payload = self._payload()
        scan.attach_covers(payload, None, "url")
        self.assertEqual(payload["games"][0]["cover"],
                         scan.STEAM_CDN + "/730/header.jpg")

    def test_attach_ignores_unknown_mode(self):
        self._put(730, "header.jpg")
        payload = self._payload()
        self.assertEqual(scan.attach_covers(payload, self.root, None), 0)
        self.assertNotIn("cover", payload["games"][0])

    def test_attach_without_cache(self):
        """选的是附加库时（没有 appcache），不该报错，本地命中数为 0。"""
        payload = self._payload()
        self.assertEqual(
            scan.attach_covers(payload, self.root / "nope", "inline", allow_cdn=False), 0)

    def test_inline_is_decodable(self):
        import base64
        self._put(730, "header.jpg")
        payload = self._payload()
        scan.attach_covers(payload, self.root, "inline")
        uri = payload["games"][0]["cover"]
        raw = base64.b64decode(uri.split(",", 1)[1])
        self.assertTrue(raw.startswith(b"\xff\xd8\xff"), "解出来确实是 JPEG")


class TestBuildHtml(unittest.TestCase):
    def test_inlines_everything(self):
        payload = {
            "generatedAt": "2026-01-01 00:00:00",
            "steamRoot": "D:\\Steam",
            "libraries": [{"path": "D:\\Steam", "total": None, "free": None,
                           "gameCount": 1, "gameBytes": 1024}],
            "games": [{"appid": 1, "name": 'Test "Game" </script><b>', "size": 1024,
                       "lastPlayed": None, "lastUpdated": None,
                       "installdir": "x", "lib": "D:\\Steam"}],
            "totals": {"bytes": 1024, "count": 1, "libraryCount": 1},
        }
        html = scan.build_html(payload)
        self.assertIn("const DATA =", html)
        self.assertIn("--bg:#0a0d13", html)          # CSS 已内联
        self.assertIn("function layoutFlow", html)    # JS 已内联
        self.assertNotIn("/*__STYLE__*/", html)
        self.assertNotIn("/*__APP__*/", html)
        self.assertNotIn("/*__DATA__*/", html)
        # 游戏名里的 </script> 必须被转义，不能提前闭合标签
        self.assertEqual(html.count("</script>"), 3)  # 模板自身的 3 个闭合标签
        self.assertIn("<\\/script>", html)


if __name__ == "__main__":
    unittest.main()
