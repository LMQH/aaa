"""最近工具详情视图：分页翻底、陈旧完成隔离、失败全文、控制字符与 clear。

只断言消费者可见行为（render/toggle/scroll/update/clear），不测内部接线。
"""

import unittest

from aaa.cli.tool_details import ToolDetails
from aaa.cli.ui import display_width


def plain(rows) -> str:
    """拼接渲染行为纯文本，便于内容断言。"""
    return "\n".join(text for _, text in rows)


def body_text(rows) -> str:
    return "\n".join(text for _, text in rows[1:-1])


def walk_pages(view: ToolDetails, width: int, height: int, limit: int = 300) -> list:
    """逐页下翻收集去重后的页面，直到页脚位置不再变化（到底）。"""
    pages = []
    previous = None
    for _ in range(limit):
        rows = view.render(width, height)
        text = plain(rows)
        if text == previous:
            break
        previous = text
        pages.append(rows)
        view.scroll(1)
    return pages


class PaginationTest(unittest.TestCase):
    def test_long_multiline_cjk_content_reaches_tail_by_pagination(self) -> None:
        view = ToolDetails()
        args = {
            "path": "笔记.txt",
            "content": "前言标记\n" + "中文内容一二三四五六七八九十" * 40 + "\n参数尾标记\n",
        }
        view.update("tool_start", tool="write", args=args, call_id="c1")
        view.update(
            "tool_done", tool="write", call_id="c1", duration=1.2,
            result="结果头标记\n" + "结果正文甲乙丙丁戊己庚辛壬癸" * 40 + "\n结果尾标记",
        )
        self.assertTrue(view.toggle())
        width, height = 30, 8
        pages = walk_pages(view, width, height)
        self.assertGreater(len(pages), 1)  # 内容超过一屏，确实分页

        first = plain(pages[0])
        self.assertIn("write", first)
        self.assertIn("完成", pages[0][0][1])
        self.assertIn("1.2s", pages[0][0][1])
        self.assertIn("前言标记", first)
        joined = "\n".join(plain(rows) for rows in pages)
        self.assertIn("参数尾标记", joined)   # 参数全文可翻到
        self.assertIn("结果头标记", joined)   # 结果全文可翻到
        self.assertIn("结果尾标记", joined)

        last_body = body_text(pages[-1])
        self.assertIn("结果尾标记", last_body)
        view.scroll(1)
        self.assertEqual(body_text(view.render(width, height)), last_body)
        view.scroll(-1)
        self.assertNotEqual(body_text(view.render(width, height)), last_body)
        for rows in pages:
            for _, text in rows:
                self.assertLessEqual(display_width(text), width)  # CJK 折行不溢出


class StaleCompletionTest(unittest.TestCase):
    def test_stale_completion_same_name_different_id_cannot_overwrite_newer_call(self) -> None:
        view = ToolDetails()
        view.update("tool_start", tool="read", args={"path": "旧.txt"}, call_id="one")
        view.update("tool_start", tool="read", args={"path": "新.txt"}, call_id="two")
        view.update("tool_done", tool="read", call_id="one", duration=9.9, result="旧结果")
        self.assertTrue(view.toggle())
        text = plain(view.render(80, 10))
        self.assertIn("运行中", text)          # 新调用仍未结束
        self.assertIn("新.txt", text)
        self.assertNotIn("旧结果", text)

        view.update("tool_done", tool="read", call_id="two", duration=0.5, result="新结果")
        text = plain(view.render(80, 10))
        self.assertIn("完成", text)
        self.assertIn("0.5s", text)
        self.assertIn("新结果", text)

    def test_idless_completion_cannot_overwrite_known_id_call(self) -> None:
        view = ToolDetails()
        view.update("tool_start", tool="read", args={"path": "新.txt"}, call_id="two")
        view.update("tool_done", tool="read", duration=3.3, result="无ID旧结果")
        self.assertTrue(view.toggle())
        text = plain(view.render(80, 10))
        self.assertIn("运行中", text)           # 已知 ID 的调用不受无 ID 完成影响
        self.assertNotIn("无ID旧结果", text)
        self.assertNotIn("3.3s", text)

        other = ToolDetails()                   # 双方均无 call_id 时按工具名匹配
        other.update("tool_start", tool="read", args={"path": "a"})
        other.update("tool_done", tool="read", duration=0.2, result="同名结果")
        self.assertTrue(other.toggle())
        text = plain(other.render(80, 10))
        self.assertIn("完成", text)
        self.assertIn("0.2s", text)
        self.assertIn("同名结果", text)


class FailureTest(unittest.TestCase):
    def test_tool_error_renders_full_error_and_result(self) -> None:
        view = ToolDetails()
        error = "拒绝执行：路径越界\n" + "错误详情甲乙丙丁" * 30 + "\n错误尾标记"
        result = "已写入部分输出\n" + "结果正文甲乙丙丁" * 30 + "\n结果尾标记"
        view.update("tool_start", tool="shell", args={"command": "rm ./x"}, call_id="e1")
        view.update(
            "tool_error", tool="shell", call_id="e1", duration=0.4,
            error=error, result=result,
        )
        self.assertTrue(view.toggle())
        joined = "\n".join(plain(rows) for rows in walk_pages(view, 40, 9))
        self.assertIn("失败", joined)
        self.assertIn("0.4s", joined)
        self.assertIn("拒绝执行：路径越界", joined)
        self.assertIn("错误尾标记", joined)   # 错误全文可翻到
        self.assertIn("已写入部分输出", joined)
        self.assertIn("结果尾标记", joined)   # 失败也保留完整结果

    def test_tool_done_with_truthy_error_is_failed(self) -> None:
        view = ToolDetails()
        view.update("tool_start", tool="read", args={"path": "a"}, call_id="d1")
        view.update(
            "tool_done", tool="read", call_id="d1", duration=1.0,
            result="仍有部分输出", error="boom",
        )
        self.assertTrue(view.toggle())
        text = plain(view.render(80, 12))
        self.assertIn("失败", text)
        self.assertIn("boom", text)
        self.assertIn("仍有部分输出", text)


class ControlCharacterTest(unittest.TestCase):
    def test_unsafe_controls_stripped_but_newline_and_tab_alignment_kept(self) -> None:
        view = ToolDetails()
        view.update("tool_start", tool="read", args={"path": "a\x1b[2Jb\x00c"}, call_id="k")
        view.update(
            "tool_done", tool="read", call_id="k", duration=0.1,
            result="列1\t列2\n列3\x1b]0;t\x07!",
        )
        self.assertTrue(view.toggle())
        joined = "\n".join(plain(rows) for rows in walk_pages(view, 80, 12))
        for control in ("\x1b", "\x00", "\x07"):
            self.assertNotIn(control, joined)
        self.assertNotIn("0;t", joined)        # OSC 序列整体消失
        self.assertIn("列1" + " " * 5 + "列2", joined)
        self.assertIn("列3", joined)           # 换行保义
        self.assertIn('"path"', joined)

    def test_tabs_and_combining_marks_fit_actual_terminal_cells(self):
        from prompt_toolkit.layout.screen import Char

        view = ToolDetails()
        view.update("tool_start", tool="read", args={}, call_id="cells")
        view.update(
            "tool_done", tool="read", call_id="cells", duration=0.1,
            result="列1\t列2\n" + "e\u0301" * 20 + "\t尾",
        )
        pages = walk_pages(view, 30, 6)
        bodies = [line for page in pages for _, line in page[1:-1]]
        self.assertTrue(any("e\u0301" * 20 in line for line in bodies))
        self.assertTrue(any("尾" in line for line in bodies))
        for line in bodies:
            self.assertNotIn("\t", line)
            self.assertLessEqual(sum(Char(char, "").width for char in line), 30)


class ClearTest(unittest.TestCase):
    def test_clear_closes_view_and_drops_old_tool(self) -> None:
        view = ToolDetails()
        self.assertFalse(view.toggle())        # 无工具时不可展开
        view.update("tool_start", tool="read", args={"path": "x"}, call_id="c1")
        self.assertTrue(view.toggle())
        self.assertTrue(view.expanded)
        self.assertTrue(view.render(80, 10))

        view.clear()
        self.assertFalse(view.expanded)
        self.assertFalse(view.toggle())
        self.assertEqual(view.render(80, 10), [])

        view.update("tool_done", tool="read", call_id="c1", duration=1.0, result="迟到结果")
        self.assertEqual(view.render(80, 10), [])  # 旧调用完成不能复活

        view.update("tool_start", tool="read", args={"path": "y"}, call_id="c2")
        self.assertTrue(view.toggle())
        text = plain(view.render(80, 10))
        self.assertIn("y", text)
        self.assertNotIn("迟到结果", text)


if __name__ == "__main__":
    unittest.main()
