"""工具的作用范围：命令与相对路径都以启动目录为准，grep 不越过文件工具的排除规则。"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aaa.boundary.policy import MODE_FULL, MODE_READ, MODE_WRITE
from aaa.config.settings import Settings
from aaa.runtime.agent import tools_for_mode
from aaa.tools.builtin import files as files_mod

TOKEN = "NEEDLE_TOKEN_7f3a"


class ToolScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.root / "app.py").write_text(f"{TOKEN} = 1\nsecond line\nthird line\n", encoding="utf-8")
        (self.root / "notes.txt").write_text(f"{TOKEN} in notes\n", encoding="utf-8")
        (self.root / ".env").write_text(f"{TOKEN}=secret\n", encoding="utf-8")
        (self.root / ".env.example").write_text(f"{TOKEN}=placeholder\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text(f"const {TOKEN} = 1;\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "deep.py").write_text(f"{TOKEN} deep\n", encoding="utf-8")

    def tools(self, mode):
        found = {}
        for tool in tools_for_mode(mode, Settings()):
            for name in getattr(tool, "functions", None) or {}:
                found[name] = getattr(tool, name)
        return found

    def test_shell_runs_in_launch_directory_in_every_mode(self):
        for mode in (MODE_WRITE, MODE_FULL):
            with self.subTest(mode=mode):
                self.assertEqual(self.tools(mode)["run_shell_command"](["pwd"]).strip(), str(self.root.resolve()))

    def test_relative_paths_resolve_against_launch_directory_in_full_access(self):
        tools = self.tools(MODE_FULL)
        self.assertIn(TOKEN, tools["read_file"](path="app.py"))
        listing = tools["list_files"]()
        self.assertIn("app.py", listing)
        self.assertIn("notes.txt", listing)

    def test_grep_skips_excluded_paths_in_every_mode(self):
        for mode in (MODE_READ, MODE_FULL):
            with self.subTest(mode=mode):
                grep = self.tools(mode)["grep"]
                out = grep(TOKEN)
                self.assertIn("app.py:1:", out)
                self.assertIn("notes.txt:1:", out)
                self.assertIn(".env.example:1:", out)  # 豁免项照常可搜
                self.assertIsNone(re.search(r"(?m)^\.env:", out))
                self.assertNotIn("node_modules", out)
                refused = grep(TOKEN, path=".env")
                self.assertTrue(refused.startswith("Error:"), refused)
                self.assertIn("excluded", refused)

    def test_grep_stays_inside_the_file_root(self):
        grep = self.tools(MODE_READ)["grep"]
        self.assertTrue(grep(TOKEN, path="/etc").startswith("Error:"))

    def test_grep_context_include_case_and_limit(self):
        grep = self.tools(MODE_READ)["grep"]
        self.assertEqual(
            grep("second", context=1).split("\n"),
            [f"app.py-1-{TOKEN} = 1", "app.py:2:second line", "app.py-3-third line"],
        )
        self.assertEqual(grep(TOKEN, include="*.txt").split("\n"), [f"notes.txt:1:{TOKEN} in notes"])
        self.assertEqual(grep("needle_token"), "No matches found for pattern: needle_token")
        self.assertIn("app.py:1:", grep("needle_token", ignore_case=True))
        self.assertEqual(grep(TOKEN, limit=4).count("\n"), 3)  # 恰好等于上限：不加提示
        limited = grep(TOKEN, limit=3).split("\n")
        self.assertEqual(len(limited), 4)
        self.assertEqual(limited[-1], "[Results limited to 3 matches]")

    def test_grep_ignores_binary_files(self):
        (self.root / "blob.bin").write_bytes(b"\x00\x01" + TOKEN.encode() + b"\x00")
        self.assertNotIn("blob.bin", self.tools(MODE_READ)["grep"](TOKEN))

    def test_grep_deadline_stops_the_walk(self):
        for index in range(20):
            (self.root / f"d{index}").mkdir()
            (self.root / f"d{index}" / "f.py").write_text(f"{TOKEN} = {index}\n", encoding="utf-8")
        visited = []
        real_walk = os.walk

        def counting_walk(top):
            for item in real_walk(top):
                visited.append(item[0])
                yield item

        with mock.patch.object(os, "walk", counting_walk):
            with mock.patch.object(files_mod, "GREP_TIMEOUT_SECONDS", -1):
                out = self.tools(MODE_READ)["grep"](TOKEN)
        # 时限已过就不再往下走：只列了第一个目录就停，不先把整棵树列完。
        self.assertLessEqual(len(visited), 1)
        self.assertIn("[Search stopped", out)

    def test_listed_paths_read_back_in_full_access(self):
        tools = self.tools(MODE_FULL)
        files = json.loads(tools["list_files"](recursive=True))["files"]
        paths = sorted(entry["path"] for entry in files if entry["type"] == "file")
        self.assertEqual(paths, [".env.example", "app.py", "notes.txt", "sub/deep.py"])
        for path in paths:
            self.assertIn(TOKEN, tools["read_file"](path=path))
        outside = json.loads(tools["list_files"]("/etc"))["files"]
        hosts = next(entry["path"] for entry in outside if entry["path"].endswith("/hosts"))
        self.assertIn("localhost", tools["read_file"](path=hosts))


if __name__ == "__main__":
    unittest.main()
