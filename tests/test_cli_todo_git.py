"""TODO 树、Git 与编辑器状态行的显示行为。"""

import unittest

from aaa.cli.git_status import GitStatus, _parse
from aaa.cli.ui import display_width, editor_status, todo_lines


class TodoTreeTest(unittest.TestCase):
    def test_phases_counts_and_current_focus(self) -> None:
        items = [
            {"text": "完成一", "phase": "A", "status": "completed"},
            {"text": "进行中", "phase": "A", "status": "in_progress"},
            {"text": "待办二", "phase": "B", "status": "pending"},
        ]
        lines = todo_lines(items, 80)
        texts = [text for _, text in lines]
        self.assertEqual(texts[0], "TODO 1/3")
        self.assertEqual(texts[1], "├─ A 1/2")
        self.assertIn("▶ 2. 进行中", "\n".join(texts))
        # 非当前阶段折叠：只有标题
        self.assertIn("└─ B 0/1", texts[-1])
        self.assertFalse(any("待办二" in text for text in texts))

    def test_no_in_progress_does_not_fake_one(self) -> None:
        items = [{"text": "待办", "phase": "A", "status": "pending"}]
        texts = [text for _, text in todo_lines(items, 80)]
        self.assertIn("· 1. 待办", texts[-1])
        self.assertNotIn("▶", "".join(texts))

    def test_height_budget_keeps_current_task(self) -> None:
        items = [
            {"text": f"A{i}", "status": "completed", "phase": "阶段A"} for i in range(1, 4)
        ] + [
            {"text": f"B{i}", "status": "pending", "phase": "阶段B"} for i in range(1, 16)
        ]
        items[17]["status"] = "in_progress"  # 当前任务 B15，位于预算之外
        texts = [text for _, text in todo_lines(items, 60)]
        self.assertLessEqual(len(texts), 13)
        self.assertTrue(any("▶" in text and "B15" in text for text in texts))
        self.assertTrue(texts[-1].startswith("… 还有"))

    def test_width_truncation_never_overflows(self) -> None:
        items = [{"text": "很长的任务" * 20, "phase": "阶段" * 10, "status": "pending"}]
        for width in (12, 30, 80):
            for _, text in todo_lines(items, width):
                self.assertLessEqual(display_width(text), width)


class GitParseTest(unittest.TestCase):
    def test_branch_and_file_counts(self) -> None:
        raw = b"## main...origin/main\x00 M a.py\x00?? b.py\x00?? c.txt\x00"
        status = _parse(raw)
        self.assertEqual((status.branch, status.changed, status.untracked), ("main", 1, 2))
        self.assertEqual(status.display(), "⑂ main *1 ?2")

    def test_rename_counts_once_and_skips_original_path(self) -> None:
        raw = b"## main\x00R  new.py\x00old.py\x00"
        status = _parse(raw)
        self.assertEqual((status.changed, status.untracked), (1, 0))

    def test_non_repo_and_headless(self) -> None:
        self.assertIsNone(_parse(b""))
        detached = _parse(b"## HEAD (no branch)\x00 M a\x00")
        self.assertEqual(detached.branch, "detached")


class EditorStatusTest(unittest.TestCase):
    def test_status_keeps_primary_fields_and_only_shows_real_measurements(self) -> None:
        common = dict(
            logo="α", model="Flash (Offline)", cwd="~/工作区/很长的目录/项目",
            git=GitStatus("release", 4, 9), window=128000, compact_at=96000,
            bar_min=24, bar_max=60,
        )

        def row(width, cost, tokens):
            return "".join(text for _, text in editor_status(width, cost=cost, tokens=tokens, **common))

        wide = row(130, 0.0234, 32000)
        self.assertEqual(display_width(wide), 130)
        self.assertLess(wide.index("工作区"), wide.index("release *4 ?9"))
        self.assertIn("$0.0234", wide)
        self.assertIn("25%", wide)
        self.assertTrue(wide.endswith("128k"))
        unknown = row(130, None, None)
        self.assertTrue(unknown.endswith("128k"))
        self.assertNotIn("%", unknown)
        self.assertNotIn("$", unknown)
        narrow = row(38, 0.0234, 32000)
        self.assertLessEqual(display_width(narrow), 38)
        self.assertIn("Flash (Offline)", narrow)
        self.assertNotIn("*4 ?9", narrow)


if __name__ == "__main__":
    unittest.main()


class TodoViewTest(unittest.TestCase):
    def test_theme_styles_parse_and_todo_classes_defined(self) -> None:
        from prompt_toolkit.styles import Style

        from aaa.cli.ui import PT_STYLES, TODO_STYLES

        for name, theme in PT_STYLES.items():
            Style.from_dict(theme)  # 非法样式关键字会在这里抛错
            for style in TODO_STYLES.values():
                key = style.split("class:", 1)[1]
                self.assertIn(key, theme, (name, key))

    def test_todos_command_groups_non_contiguous_phase_once(self) -> None:
        import contextlib
        import io
        from types import SimpleNamespace

        from aaa.cli.commands import _todos_command

        state = SimpleNamespace(
            session_id="s",
            runnable=SimpleNamespace(get_session_state=lambda session_id=None: {
                "todos": [
                    {"text": "A1", "phase": "A", "status": "completed"},
                    {"text": "B1", "phase": "B", "status": "pending"},
                    {"text": "A2", "phase": "A", "status": "in_progress"},
                ]
            }),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _todos_command(state)
        text = out.getvalue()
        self.assertEqual(text.count("  A "), 1)
        self.assertEqual(text.count("  B "), 1)
        self.assertIn("[x] 1. A1", text)
        self.assertIn("[>] 3. A2", text)
