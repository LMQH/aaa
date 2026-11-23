"""渲染行为：Markdown 块、diff 行/词高亮与纯文本降级。"""

import re
import unittest

from aaa.cli.render import MarkdownStream, render_diff, render_markdown, sanitize

ANSI = re.compile(r"\x1b\[[0-9;]*m")
BOLD_WHITE_ON_RED = "\x1b[1;37;41m"
BOLD_WHITE_ON_GREEN = "\x1b[1;37;42m"


def plain(text: str) -> str:
    return ANSI.sub("", text)


class MarkdownTest(unittest.TestCase):
    def test_heading_renders_text_with_style(self) -> None:
        out = render_markdown("# 标题\n\n正文", color=True)
        self.assertIn("标题", plain(out))
        self.assertIn("正文", plain(out))
        self.assertNotEqual(out, plain(out))  # 有样式

    def test_colored_links_keep_label_and_url_without_osc(self) -> None:
        out = render_markdown("[文档](https://example.com/guide)", color=True)
        self.assertIn("文档 (https://example.com/guide)", plain(out))
        self.assertNotIn("\x1b]", out)

    def test_stream_flushes_blocks_and_keeps_partial(self) -> None:
        stream = MarkdownStream(color=True)
        self.assertEqual(plain(stream.feed("第一段")), "")
        first = plain(stream.feed("\n\n第二"))
        self.assertIn("第一段", first)
        self.assertNotIn("第二", first)
        tail = plain(stream.close())
        self.assertIn("第二", tail)

    def test_code_fence_flushes_on_close(self) -> None:
        stream = MarkdownStream(color=True)
        body = stream.feed("```python\nprint(1)\n```\n")
        self.assertIn("print(1)", plain(body))


class SanitizeTest(unittest.TestCase):
    def test_control_sequences_removed_text_kept(self) -> None:
        raw = "a\x1b[2Jb\x1b]0;title\x07c\x00d"
        self.assertEqual(sanitize(raw), "abcd")
        self.assertEqual(sanitize("行1\n\t行2"), "行1\n\t行2")

    def test_interrupted_escape_cannot_reassemble_terminal_command(self) -> None:
        self.assertEqual(sanitize("before\x1b\x00[2Jafter"), "before[2Jafter")

    def test_only_newline_and_tab_survive_control_filter(self) -> None:
        controls = "".join(chr(value) for value in range(32))
        controls += "".join(chr(value) for value in range(127, 160))
        self.assertEqual(sanitize("X" + controls + "Y"), "X\t\nY")
        text = "中文\t🙂\n正常正文"
        self.assertEqual(sanitize(text), text)

    def test_plain_markdown_has_no_escape(self) -> None:
        out = render_markdown("# 标题", color=False)
        self.assertEqual(out, "# 标题\n")

    def test_blocks_end_with_single_newline(self) -> None:
        stream = MarkdownStream(color=False)
        first = stream.feed("第一段\n\n")
        self.assertTrue(first.endswith("\n"))
        self.assertNotIn("第二", first)
        second = stream.feed("第二段\n\n")
        self.assertTrue(second.endswith("\n"))
        self.assertEqual(first + second, "第一段\n第二段\n")


class DiffTest(unittest.TestCase):
    def test_line_colors_and_word_highlights(self) -> None:
        out = render_diff("--- a/f\n+++ b/f\n@@\n-旧 句子\n+新 句子\n", color=True)
        text = plain(out)
        self.assertIn("旧 句子", text)
        self.assertIn("新 句子", text)
        self.assertIn(BOLD_WHITE_ON_RED, out)
        self.assertIn(BOLD_WHITE_ON_GREEN, out)

    def test_unpaired_blocks_stay_line_level(self) -> None:
        out = render_diff("--- a/f\n+++ b/f\n@@\n-只删一行\n+增一\n+增二\n", color=True)
        self.assertIn("\x1b[31m", out)   # red
        self.assertIn("\x1b[32m", out)   # green
        self.assertNotIn(BOLD_WHITE_ON_RED, out)

    def test_color_off_emits_no_escape(self) -> None:
        out = render_diff("-删\n+增\n", color=False)
        self.assertNotIn("\x1b[31m", out)
        self.assertNotIn("\x1b[1;37;41m", out)

    def test_plain_text_is_preserved(self) -> None:
        diff = "+新增\n-删除\n context\n"
        bodies = [
            re.sub(r"^\s*(?:\d+\s+)+[+-]?", "", line).strip()
            for line in plain(render_diff(diff, color=True)).splitlines()
        ]
        bodies = [line for line in bodies if line]
        self.assertEqual(bodies, ["新增", "删除", "context"])


class ToolStatusTest(unittest.TestCase):
    def test_native_completed_event_with_tool_error_is_not_shown_as_success(self):
        import contextlib
        import io

        from aaa.cli.run import HumanEmitter
        from aaa.cli.ui import GLYPH

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            emitter = HumanEmitter()
            emitter.event("tool_done", tool="read_file", duration=0.1, result="Error: denied", error=True)
            emitter.close()
        self.assertIn(GLYPH["fail"], output.getvalue())
        self.assertNotIn(GLYPH["ok"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
