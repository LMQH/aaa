"""设置面板：页签、分组、搜索、导航与渲染。"""

import unittest
from types import SimpleNamespace

from aaa.cli import settings_view as sv
from aaa.cli.ui import display_width
from aaa.config.settings import Settings


def _state(settings):
    channel = SimpleNamespace(name="local", id="local")
    model = SimpleNamespace(id="m")
    return SimpleNamespace(
        settings=settings,
        selection=lambda: (channel, model),
        window=lambda: 128_000,
        mode="read-only",
    )


def _plain(rows):
    return ["".join(text for _style, text in row) for row in rows]


class RowsTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_ui_tab_uses_left_sections_and_dividers(self):
        view = sv.View(tab="ui")
        rows = sv.build_rows(self.settings, view)
        self.assertEqual([name for name, _keys in sv.left_sections(self.settings, view)],
                         ["主题与标识", "横幅", "状态条", "输入"])
        self.assertFalse(any(row.kind == "header" for row in rows))
        self.assertEqual(sum(1 for row in rows if row.kind == "divider"), 3)
        paths = [row.path for row in rows if row.kind == "item"]
        self.assertIn("ui.theme", paths)
        self.assertIn("ui.banner_duration", paths)

    def test_memory_tab_sections_follow_key_order(self):
        names = [name for name, _keys in sv.sections(self.settings, "memory")]
        self.assertEqual(names, ["数据", "召回", "清理", "超时"])

    def test_extensions_tab_merges_project_keys(self):
        rows = sv.build_rows(self.settings, sv.View(tab="extensions"))
        paths = [row.path for row in rows if row.kind == "item"]
        self.assertIn("extensions.mcp_timeout", paths)
        self.assertIn("project.enabled", paths)

    def test_search_spans_tabs_and_groups_by_tab_label(self):
        rows = sv.build_rows(self.settings, sv.View(query="banner"))
        headers = [row.label for row in rows if row.kind == "header"]
        self.assertEqual(headers, ["外观"])
        self.assertTrue(all("banner" in row.path for row in rows if row.kind == "item"))
        self.assertFalse(any(row.kind == "divider" for row in rows))

    def test_move_skips_headers_and_wraps(self):
        view = sv.View(tab="ui")
        rows = sv.build_rows(self.settings, view)
        sv.ensure_selection(rows, view)
        first = view.selected
        sv.move(rows, view, 1)
        self.assertNotEqual(view.selected, first)
        sv.move(rows, view, -1)
        self.assertEqual(view.selected, first)
        sv.move(rows, view, -1)
        self.assertNotEqual(view.selected, first)

    def test_jump_section_moves_to_next_group(self):
        view = sv.View(tab="ui")
        rows = sv.build_rows(self.settings, view)
        sv.ensure_selection(rows, view)
        sv.jump_section(rows, view)
        self.assertEqual(view.selected, "ui.banner_enabled")

    def test_tab_line_groups_segments(self):
        view = sv.View(tab="ui")
        line = "".join(text for _style, text in sv._tab_line(view, 100))
        self.assertIn("·", line)
        self.assertIn("│", line)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def _lines(self, view, columns=100, rows=30):
        return _plain(sv.render(self.settings, _state(self.settings), view, columns, rows))

    def test_panel_is_exactly_terminal_width_and_has_footer(self):
        lines = self._lines(sv.View(tab="ui"))
        self.assertEqual(len(lines), 30)
        for line in lines:
            self.assertEqual(display_width(line), 100)
        self.assertIn("设置", lines[0])
        self.assertIn("修改", lines[-2])

    def test_selection_marker_and_value_column(self):
        lines = self._lines(sv.View(tab="ui"))
        self.assertTrue(any("❯ theme" in line for line in lines))
        self.assertTrue(any("default" in line for line in lines))

    def test_active_section_bar_is_accent_for_every_row(self):
        rows = sv.render(self.settings, _state(self.settings), sv.View(tab="ui"), 100, 30)
        bars = [
            (row[1][0], "".join(text for _style, text in row))
            for row in rows if "▍" in "".join(text for _style, text in row)
        ]
        self.assertGreaterEqual(len(bars), 4)
        self.assertEqual([style for style, _line in bars[:2]],
                         ["class:picker.title", "class:picker.title"])
        self.assertEqual(bars[2][0], "class:status.sep")

    def test_section_range_bars_mark_each_group(self):
        lines = self._lines(sv.View(tab="ui"))
        body = lines[3:-6]
        self.assertTrue(any("▍ 主题与标识" in line for line in body))
        self.assertTrue(any("▍ 横幅" in line for line in body))
        self.assertGreaterEqual(sum(line.count("▍") for line in body), 10)
        self.assertFalse(any("───" in line for line in body))

    def test_search_line_shows_query_and_matches(self):
        lines = self._lines(sv.View(query="banner"))
        self.assertIn("搜索", lines[1])
        self.assertIn("banner", lines[1])
        self.assertIn("项", lines[1])

    def test_preview_only_on_appearance_tab(self):
        ui = self._lines(sv.View(tab="ui"))
        model = self._lines(sv.View(tab="model"))
        self.assertTrue(any(line.startswith("│ 预览") for line in ui))
        self.assertFalse(any(line.startswith("│ 预览") for line in model))

    def test_description_mentions_path_and_apply(self):
        lines = self._lines(sv.View(tab="ui"))
        self.assertTrue(any("ui.theme" in line and "生效" in line for line in lines))
