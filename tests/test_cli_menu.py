"""Native menu input remains usable inside the already-running REPL loop."""

import unittest

import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import Completion
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import merge_styles
from prompt_toolkit.styles.defaults import default_ui_style

from aaa.cli import picker
from aaa.cli.commands import _animation_items, _ask_menu
from aaa.cli.ui import (
    COMMANDS,
    GLYPH,
    ICONS,
    completion_rows,
    display_width,
    report_lines,
    style_for,
)


class PickerTests(unittest.TestCase):
    def choose(self, keys: str, **kwargs):
        with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
            source.send_text(keys)
            return picker.choose(
                "选择",
                [
                    picker.Item("alpha", "alpha", "第一项"),
                    picker.Item("beta", "beta", "第二项"),
                    picker.Item("gamma", "gamma"),
                ],
                **kwargs,
            )

    def test_down_moves_selection_and_enter_confirms(self):
        self.assertEqual(self.choose("\x1b[B\r"), "beta")
        self.assertEqual(self.choose("\x1b[B\x1b[A\x1b[A\r"), "gamma")

    def test_typing_filters_and_escape_returns_back(self):
        self.assertEqual(self.choose("ga\r"), "gamma")
        self.assertEqual(self.choose("\x1b"), picker.BACK)
        self.assertEqual(self.choose("\x03"), picker.ABORT)

    def test_text_prompt_returns_default_edit_and_back(self):
        def ask(keys: str, default: str = ""):
            with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
                source.send_text(keys)
                return picker.ask_text("值", default=default)

        self.assertEqual(ask("hello\r"), "hello")
        self.assertEqual(ask("\r", "d"), "d")
        self.assertEqual(ask("\x1b"), picker.BACK)
        self.assertEqual(ask("\x03"), picker.ABORT)

    def test_animation_menu_lists_discovered_animations(self):
        names = [item.value for item in _animation_items()]
        self.assertIn("default", names)
        self.assertIn("sharingan", names)
        self.assertEqual(names, sorted(names))

    def test_row_fragments_never_exceed_the_terminal_width(self):
        items = [
            picker.Item("approval", "boundary.command_approval = always", "命令类工具的审批策略（always=每条都问，dangerous=仅危险命令）"),
            picker.Item("logo", "α", "另一段明显更长的说明文字，用来覆盖短标签补齐到长标签列宽的情况"),
        ]
        for width in (12, 34, 40, 60):
            label_width = min(
                max(display_width(item.label) for item in items),
                max(8, width // 2),
            )
            for item in items:
                for selected in (False, True):
                    with self.subTest(width=width, label=item.label, selected=selected):
                        fragments = picker.row_fragments(item, selected, width, label_width)
                        line = "".join(text for _, text in fragments)
                        self.assertLessEqual(display_width(line), width)

    def test_empty_choice_list_returns_back_without_opening_an_app(self):
        self.assertEqual(picker.choose("空", []), picker.BACK)

    def test_page_down_moves_past_a_full_screen(self):
        with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
            source.send_text("\x1b[6~\r")
            result = picker.choose(
                "翻页",
                [picker.Item(index, f"item-{index}") for index in range(20)],
            )
        self.assertEqual(result, 10)


class AsyncMenuTest(unittest.IsolatedAsyncioTestCase):
    async def test_picker_runs_inside_a_running_loop(self):
        with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
            source.send_text("\x1b[B\r")
            result = picker.choose(
                "选择",
                [picker.Item("first", "first"), picker.Item("second", "second")],
            )
        self.assertEqual(result, "second")

    async def test_selects_nondefault_choice_inside_running_loop(self):
        with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
            question = questionary.select("选择", choices=["first", "second"])
            source.send_text("\x1b[B\r")
            self.assertEqual(_ask_menu(question), "second")

    async def test_escape_and_interrupt_cancel_without_leaving_repl_loop(self):
        for key in ("\x1b", "\x03"):
            with self.subTest(key=key), create_pipe_input() as source, create_app_session(
                input=source, output=DummyOutput()
            ):
                question = questionary.select("选择", choices=["first", "second"])
                source.send_text(key)
                self.assertIsNone(_ask_menu(question))


class CompletionRowsTest(unittest.TestCase):
    """补全菜单：指针、对齐列与滚动条。"""

    def _items(self, count=3):
        return [
            Completion(f"/cmd{index}", start_position=0, display_meta=f"说明{index}")
            for index in range(count)
        ]

    def _plain(self, rows):
        return ["".join(text for _, text in row) for row in rows]

    def test_pointer_marks_current_row_and_meta_column_lines_up(self):
        rows = completion_rows(self._items(), 1, 60, 10)
        plain = self._plain(rows)
        self.assertTrue(plain[1].startswith(f"{GLYPH['pointer']} "))
        self.assertFalse(plain[0].startswith(GLYPH["pointer"]))
        self.assertEqual(len({line.index("说明") for line in plain}), 1)

    def test_rows_fill_the_width_and_carry_a_scrollbar(self):
        rows = completion_rows(self._items(30), 25, 80, 10)
        self.assertEqual(len(rows), 10)
        for row in rows:
            self.assertEqual(display_width("".join(text for _, text in row)), 80)
        self.assertTrue(self._plain(rows)[-1].startswith(f"{GLYPH['pointer']} "))
        self.assertIn("█", [row[-1][1] for row in rows])

    def test_without_a_selection_no_row_gets_the_pointer(self):
        rows = completion_rows(self._items(), None, 60, 10)
        self.assertFalse(any(text.startswith(GLYPH["pointer"]) for _, text in rows[0]))

    def test_selected_row_uses_color_not_reverse_video(self):
        # prompt_toolkit 自带样式给 completion.current 加了 reverse；必须显式关掉。
        for theme in ("default", "contrast", "neon"):
            with self.subTest(theme=theme):
                merged = merge_styles([default_ui_style(), style_for(theme)])
                attrs = merged.get_attrs_for_style_str("class:completion-menu.completion.current")
                self.assertFalse(attrs.reverse)
                self.assertEqual(attrs.bgcolor, "default")
                self.assertTrue(attrs.bold)

class CommandIconTest(unittest.TestCase):
    def test_every_command_has_an_icon_outside_ascii_mode(self):
        if not ICONS:
            self.skipTest("ASCII 模式不显示图标")
        missing = [name for name, _ in COMMANDS if name not in ICONS]
        self.assertEqual(missing, [])
        self.assertTrue(all(len(icon.strip()) > 0 for icon in ICONS.values()))

class ReportLinesTest(unittest.TestCase):
    def _plain(self, rows):
        return ["".join(text for _, text in row) for row in rows]

    def test_panel_has_title_borders_and_footer_at_exact_width(self):
        rows = self._plain(report_lines("/trust", ["一", "二"], 0, 60, 10))
        self.assertEqual(len(rows), 10)
        for line in rows:
            self.assertEqual(display_width(line), 60)
        self.assertIn("/trust", rows[0])
        self.assertTrue(rows[0].startswith("╭"))
        self.assertTrue(rows[-1].startswith("╰"))
        self.assertIn("Esc 关闭", rows[-2])

    def test_scroll_clamps_and_footer_shows_range(self):
        lines = [f"第 {index} 行" for index in range(40)]
        rows = self._plain(report_lines("/usage", lines, 0, 80, 12))
        self.assertIn("1–8/40", rows[-2])
        rows = self._plain(report_lines("/usage", lines, 35, 80, 12))
        self.assertIn("33–40/40", rows[-2])
        rows = self._plain(report_lines("/usage", lines, 999, 80, 12))
        self.assertIn("33–40/40", rows[-2])

    def test_empty_lines_keep_the_frame(self):
        rows = self._plain(report_lines("/x", [], 0, 40, 7))
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(display_width(line) == 40 for line in rows))
