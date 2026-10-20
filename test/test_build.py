"""单文件构建产物的自包含性检查。

真正的验收标准只有一条：产物拷到没装 Python、也断网的机器上，
双击必须完整可用。任何外部 .js / .css 引用都会让这条失效。
"""
from __future__ import annotations

import re
import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_single  # noqa: E402


class TestSingleFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = build_single.build().read_text(encoding="utf-8")

    def test_no_external_assets(self):
        """不能残留任何外部 js / css 引用，否则脱离仓库就打不开。"""
        refs = re.findall(
            r'(?:src|href)="(?!data:|#|https?:)[^"]+\.(?:js|css)(?:\?[^"]*)?"',
            self.html,
        )
        self.assertEqual(refs, [], "产物仍引用外部文件: %s" % refs)

    def test_css_inlined(self):
        self.assertIn("<style>", self.html)
        self.assertIn("--accent", self.html, "样式变量没内联进来")

    def test_js_inlined(self):
        self.assertIn("function renderReport", self.html)
        self.assertIn("function layoutFlow", self.html)

    def test_no_premature_script_close(self):
        """内联 JS 里的 </script 必须转义，否则页面会被截断。"""
        body = self.html.split("<script>", 1)[-1].rsplit("</script>", 1)[0]
        self.assertNotIn("</script", body.lower())

    def test_offline_capable(self):
        """双击打开时页面不能依赖 fetch('/api/...') 成功。"""
        self.assertIn("probeApi", self.html)
        self.assertIn("catch", self.html)

    def test_fallback_picker_present(self):
        """没有 showDirectoryPicker 时要有兜底，否则部分浏览器彻底用不了。"""
        self.assertIn("webkitdirectory", self.html)
        self.assertIn("pickWithInput", self.html)

    def test_export_present(self):
        self.assertIn("window.__REPORT_DATA__", self.html)
        self.assertIn("function exportReport", self.html)

    def test_single_html_tag(self):
        # 源码里 exportReport 也拼了 DOCTYPE 字符串，所以只校验文档开头
        self.assertTrue(self.html.lstrip().lower().startswith("<!doctype html>"))
        self.assertEqual(self.html.lower().count("<body>"), 1)


if __name__ == "__main__":
    unittest.main()
