"""自建选择器：对齐 omp 的交互与视觉，不再使用 questionary 的列表。

选定项的呈现是 `❯` 前缀 + 主题强调色，不使用背景色块；`↑/↓` 移动、
输入即筛选、`Enter` 确认、`Esc` 返回上一级、`Ctrl-C` 放弃整个命令。
菜单在独立循环运行，与 questionary 一样不嵌套主 REPL 的事件循环。
"""

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app, get_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_bindings import merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VerticalAlign, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .ui import GLYPH, display_width, ellipsize, style_for


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


BACK = _Sentinel("BACK")
ABORT = _Sentinel("ABORT")

MAX_ROWS = 10


@dataclass(frozen=True)
class Item:
    """一个可选项；`description` 只做说明，不参与回车结果。"""

    value: Any
    label: str
    description: str = ""


def _same(left: Any, right: Any) -> bool:
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


def _score(text: str, query: str) -> Optional[tuple]:
    """子串命中优先，其次按字符顺序的稀疏命中；都不中返回 None。"""
    low = text.lower()
    at = low.find(query)
    if at >= 0:
        return (0, at)
    position = -1
    for char in query:
        position = low.find(char, position + 1)
        if position < 0:
            return None
    return (1, position)


def row_fragments(item: Item, selected: bool, width: int, label_width: int) -> list:
    """一行菜单的片段；在可用宽度内渲染，描述只在不挤爆时出现。"""
    pointer = GLYPH["pointer"] + " "
    pointer_width = display_width(pointer)
    label_width = max(1, min(label_width, width - pointer_width - 4))
    marker = pointer if selected else " " * pointer_width
    label = ellipsize(item.label, label_width)
    padding = " " * max(2, label_width - display_width(label) + 2)
    room = width - pointer_width - label_width - 2
    fragments = [
        ("class:picker.pointer" if selected else "", marker),
        ("class:picker.selected" if selected else "", label),
        ("class:picker.description", padding),
    ]
    if item.description and room > 4:
        fragments.append(("class:picker.description", ellipsize(item.description, room)))
    return fragments


def _match(item: Item, query: str) -> Optional[tuple]:
    if not query:
        return (0, 0)
    label = _score(item.label, query)
    description = _score(item.description, query) if item.description else None
    if label is not None and description is not None:
        return min(label, description)
    return label if label is not None else description


class _Chooser:
    def __init__(self, title: str, items: List[Item], default: Any, subtitle: str) -> None:
        self.title = title
        self.subtitle = subtitle
        self.items = items
        self.query = ""
        self.filtered: List[Item] = list(items)
        self.index = 0
        if default is not None:
            for position, item in enumerate(items):
                if _same(item.value, default):
                    self.index = position
                    break

    # -- 状态 -----------------------------------------------------------
    def refilter(self) -> None:
        previous = self.current()
        query = self.query.lower()
        if not query:
            self.filtered = list(self.items)
        else:
            scored = []
            for order, item in enumerate(self.items):
                score = _match(item, query)
                if score is not None:
                    scored.append((score, order, item))
            scored.sort(key=lambda entry: (entry[0], entry[1]))
            self.filtered = [entry[2] for entry in scored]
        if previous is not None and any(item is previous for item in self.filtered):
            self.index = next(i for i, item in enumerate(self.filtered) if item is previous)
        else:
            self.index = 0

    def current(self) -> Optional[Item]:
        return self.filtered[self.index] if self.filtered else None

    def move(self, delta: int) -> None:
        if not self.filtered:
            return
        self.index = (self.index + delta) % len(self.filtered)

    def type_text(self, text: str) -> None:
        self.query += text
        self.refilter()

    def backspace(self) -> None:
        if self.query:
            self.query = self.query[:-1]
            self.refilter()

    # -- 渲染 -----------------------------------------------------------
    def rows(self) -> FormattedText:
        width = max(1, get_app().output.get_size().columns)
        if not self.filtered:
            return FormattedText([("class:picker.description", "  无匹配项")])
        budget = self.visible_rows()
        start = max(0, min(len(self.filtered) - budget, self.index - budget // 2))
        shown = self.filtered[start:start + budget]
        label_width = min(
            max(display_width(item.label) for item in shown),
            max(8, width // 2),
        )
        fragments = []
        for offset, item in enumerate(shown):
            selected = start + offset == self.index
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(row_fragments(item, selected, width, label_width))
        return FormattedText(fragments)

    def visible_rows(self) -> int:
        screen = max(4, get_app().output.get_size().rows)
        return max(1, min(MAX_ROWS, screen - 4, len(self.filtered)))

    def footer(self) -> FormattedText:
        parts = []
        if self.query:
            parts.append(("class:picker.query", f"筛选：{self.query}"))
            parts.append(("class:picker.description", "   "))
        parts.append(("class:picker.hint", "↑↓ 选择 · Enter 确认 · Esc 返回 · 输入即筛选"))
        count = f"{self.index + 1}/{len(self.filtered)}" if self.filtered else "0/0"
        parts.append(("class:picker.description", f"  {count}"))
        return FormattedText(parts)


def choose(
    title: str,
    items: Iterable[Item],
    *,
    default: Any = None,
    subtitle: str = "",
    theme: str = "default",
) -> Any:
    """弹出一个选择器；返回选中值、`BACK`（Esc）或 `ABORT`（Ctrl-C）。"""
    entries = list(items)
    if not entries:
        return BACK
    chooser = _Chooser(title, entries, default, subtitle)
    session = get_app_session()
    kb = KeyBindings()

    def done(event, result) -> None:
        event.app.exit(result=result)

    @kb.add("up")
    def _(event) -> None:
        chooser.move(-1)

    @kb.add("down")
    def _(event) -> None:
        chooser.move(1)

    @kb.add("pageup")
    def _(event) -> None:
        chooser.move(-chooser.visible_rows())

    @kb.add("pagedown")
    def _(event) -> None:
        chooser.move(chooser.visible_rows())

    @kb.add("home")
    def _(event) -> None:
        chooser.index = 0

    @kb.add("end")
    def _(event) -> None:
        if chooser.filtered:
            chooser.index = len(chooser.filtered) - 1

    @kb.add("enter")
    @kb.add("tab")
    def _(event) -> None:
        item = chooser.current()
        if item is not None:
            done(event, item.value)

    @kb.add("escape", eager=True)
    def _(event) -> None:
        done(event, BACK)

    @kb.add("c-c", eager=True)
    def _(event) -> None:
        done(event, ABORT)

    @kb.add("backspace")
    def _(event) -> None:
        chooser.backspace()

    @kb.add(Keys.Any)
    def _(event) -> None:
        data = event.data or ""
        if data and data.isprintable():
            chooser.type_text(data)

    def title_height() -> int:
        return 2 if chooser.subtitle else 1

    def title_text() -> FormattedText:
        fragments = [("class:picker.title", chooser.title)]
        if chooser.subtitle:
            fragments.append(("", "\n"))
            fragments.append(("class:picker.subtitle", chooser.subtitle))
        return FormattedText(fragments)

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(title_text), height=title_height, wrap_lines=False),
                Window(FormattedTextControl(chooser.rows), height=lambda: chooser.visible_rows()),
                Window(FormattedTextControl(chooser.footer), height=1, wrap_lines=False),
            ],
            align=VerticalAlign.TOP,
        )
    )
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style_for(theme),
        input=session.input,
        output=session.output,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )
    return app.run(in_thread=True)


def ask_text(
    title: str,
    *,
    default: str = "",
    hint: str = "Enter 确认 · Esc 返回",
    theme: str = "default",
) -> Any:
    """单行文本输入；返回值、`BACK`（Esc）或 `ABORT`（Ctrl-C）。"""
    app_session = get_app_session()
    prompt = PromptSession(
        message=FormattedText(
            [
                ("class:picker.title", title + "\n"),
                ("class:picker.pointer", GLYPH["pointer"] + " "),
            ]
        ),
        style=style_for(theme),
        input=app_session.input,
        output=app_session.output,
        bottom_toolbar=lambda: FormattedText([("class:picker.hint", hint)]),
        complete_while_typing=False,
        enable_history_search=False,
        multiline=False,
    )
    prompt.default_buffer.reset(Document(default))
    extra = KeyBindings()

    @extra.add("escape", eager=True)
    def _(event) -> None:
        event.app.exit(result=BACK)

    prompt.app.key_bindings = (
        merge_key_bindings([prompt.app.key_bindings, extra])
        if prompt.app.key_bindings is not None
        else extra
    )
    try:
        return prompt.app.run(in_thread=True)
    except (KeyboardInterrupt, EOFError):
        return ABORT


def confirm(title: str, *, default: bool = False, theme: str = "default") -> Any:
    """是/否选择；返回 bool、`BACK` 或 `ABORT`。"""
    return choose(
        title,
        [Item(True, "是"), Item(False, "否")],
        default=default,
        theme=theme,
    )
