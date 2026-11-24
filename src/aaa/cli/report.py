"""只读报告面板：边框、标题、滚动与 Esc 关闭。

与 `picker` 同构：在独立线程里运行全屏 Application，关闭后回到输入区，
报告本身不写进滚动历史。
"""

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .ui import report_lines, style_for


def show(title: str, lines, theme: str = "default") -> None:
    """全屏显示一段只读文本；`Esc` 或 `Ctrl-C` 关闭。"""
    if not lines:
        return
    scroll = [0]

    def body_rows() -> int:
        return max(1, get_app().output.get_size().rows - 4)

    def move(delta: int) -> None:
        limit = max(0, len(lines) - body_rows())
        scroll[0] = max(0, min(scroll[0] + delta, limit))
        get_app().invalidate()

    kb = KeyBindings()

    @kb.add("escape", eager=True)
    @kb.add("c-c", eager=True)
    def _(event) -> None:
        event.app.exit()

    @kb.add("up", eager=True)
    def _(event) -> None:
        move(-1)

    @kb.add("down", eager=True)
    def _(event) -> None:
        move(1)

    @kb.add("pageup", eager=True)
    def _(event) -> None:
        move(-body_rows())

    @kb.add("pagedown", eager=True)
    def _(event) -> None:
        move(body_rows())

    def text() -> FormattedText:
        size = get_app().output.get_size()
        rows = report_lines(title, lines, scroll[0], size.columns, size.rows)
        fragments = []
        for index, row in enumerate(rows):
            fragments.extend(row)
            if index < len(rows) - 1:
                fragments.append(("", "\n"))
        return FormattedText(fragments)

    Application(
        layout=Layout(Window(FormattedTextControl(text), wrap_lines=False)),
        key_bindings=kb,
        style=style_for(theme),
        full_screen=True,
        mouse_support=False,
    ).run(in_thread=True)
