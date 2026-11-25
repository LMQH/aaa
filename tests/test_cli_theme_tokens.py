"""主题语义 token：三主题最终样式串与重构前的字面量表逐项相等（观感零变化）。"""

import re
import unittest

from aaa.cli.ui import (
    CLASS_TEMPLATES,
    PT_STYLES,
    THEME_CLASS_OVERRIDES,
    THEME_TOKENS,
    TOKENS,
)

# 重构前 ui.py 里的字面量样式表，原样保留作为回归基准。
GOLDEN = {
    "default": {
        "prompt": "ansicyan bold",
        "status.logo": "#73808f",
        "status.model": "#42c5f5",
        "status.path": "#e0e5ed",
        "status.git": "#e0a45b",
        "status.cost": "#dec181",
        "status.context": "#42aec9",
        "status.sep": "#576270",
        "status.hint": "#8d9bac italic",
        "status.activity": "#e0a45b",
        "completion-menu": "bg:default",
        "completion-menu.completion": "bg:default #cfd6df",
        "completion-menu.completion.current": "bg:default bold #42c5f5 noreverse",
        "completion-menu.meta.completion": "bg:default #8d9bac",
        "completion-menu.meta.completion.current": "bg:default bold #42c5f5",
        "completion-menu.multi-column-meta": "bg:default #8d9bac",
        "completion-icon": "#42c5f5",
        "completion-command": "#ffffff",
        "table.header": "bold #8d9bac",
        "table.total": "bold #e0e5ed",
        "picker.title": "bold #42c5f5",
        "picker.subtitle": "#8d9bac italic",
        "picker.pointer": "bold #42c5f5",
        "picker.selected": "bold #42c5f5",
        "picker.description": "#8d9bac",
        "picker.hint": "#8d9bac italic",
        "picker.query": "#e0e5ed",
        "todo.title": "bold #8ab4f8",
        "todo.phase": "bold #8ab4f8",
        "todo.completed": "fg:#7bd88f strike",
        "todo.in_progress": "bold fg:#ffd166",
        "todo.pending": "fg:#9aa0a6",
    },
    # 高对比：亮色文字、更明显的选中态，适配低对比终端或投影。
    "contrast": {
        "prompt": "ansiwhite bold",
        "status.logo": "ansiwhite",
        "status.model": "ansiyellow bold",
        "status.path": "ansiwhite",
        "status.git": "ansiyellow",
        "status.cost": "ansiyellow",
        "status.context": "ansicyan",
        "status.sep": "ansiwhite",
        "status.hint": "ansiwhite",
        "status.activity": "ansiyellow bold",
        "completion-menu": "bg:default",
        "completion-menu.completion": "bg:default #ffffff",
        "completion-menu.completion.current": "bg:default bold ansiyellow noreverse",
        "completion-menu.meta.completion": "bg:default #bbbbbb",
        "completion-menu.meta.completion.current": "bg:default bold ansiyellow",
        "completion-menu.multi-column-meta": "bg:default #bbbbbb",
        "completion-icon": "ansiyellow bold",
        "completion-command": "#ffffff bold",
        "table.header": "bold ansiwhite",
        "table.total": "bold ansiyellow",
        "picker.title": "bold ansiwhite",
        "picker.subtitle": "ansiwhite italic",
        "picker.pointer": "bold ansiyellow",
        "picker.selected": "bold ansiyellow",
        "picker.description": "ansiwhite",
        "picker.hint": "ansiwhite italic",
        "picker.query": "ansiwhite",
        "todo.title": "bold #ffffff",
        "todo.phase": "bold #ffffff",
        "todo.completed": "fg:#7bd88f strike",
        "todo.in_progress": "bold fg:#ffff00",
        "todo.pending": "fg:#aaaaaa",
    },
    # 霓虹：品红/青色点缀，与默认同为深底。
    "neon": {
        "prompt": "#ff6ec7 bold",
        "status.logo": "#a8a6c4",
        "status.model": "#7ef9ff",
        "status.path": "#e1defa",
        "status.git": "#ffd166",
        "status.cost": "#f8dc8c",
        "status.context": "#7ef9ff",
        "status.sep": "#807caa",
        "status.hint": "#a8a6c4 italic",
        "status.activity": "#ff6ec7",
        "completion-menu": "bg:default",
        "completion-menu.completion": "bg:default #d8d8f0",
        "completion-menu.completion.current": "bg:default bold #7ef9ff noreverse",
        "completion-menu.meta.completion": "bg:default #8a8ab0",
        "completion-menu.meta.completion.current": "bg:default bold #7ef9ff",
        "completion-menu.multi-column-meta": "bg:default #8a8ab0",
        "completion-icon": "#7ef9ff",
        "completion-command": "#ffffff",
        "table.header": "bold #8a8ab0",
        "table.total": "bold #e1defa",
        "picker.title": "bold #7ef9ff",
        "picker.subtitle": "#a8a6c4 italic",
        "picker.pointer": "bold #ff6ec7",
        "picker.selected": "bold #7ef9ff",
        "picker.description": "#a8a6c4",
        "picker.hint": "#a8a6c4 italic",
        "picker.query": "#e1defa",
        "todo.title": "bold #7ef9ff",
        "todo.phase": "bold #7ef9ff",
        "todo.completed": "fg:#7bd88f strike",
        "todo.in_progress": "bold fg:#ff6ec7",
        "todo.pending": "fg:#8a8ab0",
    },
}


class ThemeTokenTest(unittest.TestCase):
    def test_styles_match_pre_refactor_literals(self) -> None:
        self.assertEqual(PT_STYLES, GOLDEN)

    def test_every_theme_defines_every_token(self) -> None:
        for name, tokens in THEME_TOKENS.items():
            self.assertEqual(set(tokens), set(TOKENS), name)

    def test_every_template_placeholder_is_a_known_token(self) -> None:
        templates = dict(CLASS_TEMPLATES)
        for overrides in THEME_CLASS_OVERRIDES.values():
            self.assertTrue(set(overrides) <= set(CLASS_TEMPLATES), "覆盖了不存在的类名")
            templates.update(overrides)
        for cls, template in templates.items():
            for token in re.findall(r"\{(\w+)\}", template):
                self.assertIn(token, TOKENS, f"{cls} 用了未定义的 token {token}")


if __name__ == "__main__":
    unittest.main()
