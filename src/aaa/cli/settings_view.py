"""设置面板：页签 / 分组 / 键值三级，就地改值，输入即搜索。

与选择器和报告面板同构：全屏 Application 在独立线程运行，关闭后回到输入区。
写入走 `commands.set_setting`（校验 + 落盘 + 按 apply 语义生效）。
"""

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .ui import display_width, editor_status, ellipsize, pad, style_for

# 页签：组 → (中文标签, 图标)。`project` 并入「扩展」。
TABS = (
    ("ui", "外观", "◈"),
    ("model", "模型", "◆"),
    ("context", "上下文", "◎"),
    ("memory", "记忆", "❖"),
    ("tools", "工具", "⚒"),
    ("sessions", "会话", "▤"),
    ("boundary", "权限", "▶"),
    ("delegation", "委派", "⑂"),
    ("log", "日志", "≡"),
    ("extensions", "扩展", "⌗"),
)
TAB_LABEL = {name: label for name, label, _icon in TABS}
# 页签按域分三段显示：交互 / 上下文与记忆 / 权限与扩展。
TAB_SEGMENTS = ((0, 1, 2), (3, 4, 5), (6, 7, 8, 9))
TAB_ICON = {name: icon for name, _label, icon in TABS}
GROUP_KEYS = {name: (name,) for name, _label, _icon in TABS}
GROUP_KEYS["extensions"] = ("extensions", "project")


def _ui_section(path: str) -> str:
    if path in ("ui.theme", "ui.logo"):
        return "主题与标识"
    if path.startswith("ui.banner"):
        return "横幅"
    if path.startswith("ui.git") or path.startswith("ui.bar") or path == "ui.toolbar_width":
        return "状态条"
    return "输入"


def _memory_section(path: str) -> str:
    if path in ("memory.user_id", "memory.data_root", "memory.list_limit"):
        return "数据"
    if path.startswith("memory.recall"):
        return "召回"
    if path in ("memory.delete_memories", "memory.clear_memories"):
        return "清理"
    return "超时"


SECTIONS = {"ui": _ui_section, "memory": _memory_section}
SECTION_ORDER = {
    "ui": ("主题与标识", "横幅", "状态条", "输入"),
    "memory": ("数据", "召回", "清理", "超时"),
}


@dataclass
class View:
    tab: str = TABS[0][0]
    selected: Optional[str] = None
    query: str = ""
    scroll: int = 0
    editing: Optional[dict] = None
    message: str = ""
    matches: int = 0


@dataclass
class Row:
    kind: str  # header / item
    label: str
    path: Optional[str] = None
    key: object = None


def _keys(settings, group: str) -> List[object]:
    out: List[object] = []
    for name in GROUP_KEYS.get(group, (group,)):
        out.extend(settings.keys_of(name))
    return out


def sections(settings, group: str) -> List[tuple]:
    """(分组名, 键列表)；无分组规则时返回单个空名分组。"""
    rule = SECTIONS.get(group)
    keys = _keys(settings, group)
    if rule is None:
        grouped: dict = {}
        if group == "extensions":
            for key in keys:
                name = "项目" if key.path.startswith("project.") else "MCP"
                grouped.setdefault(name, []).append(key)
            return list(grouped.items())
        return [("", keys)]
    order: List[str] = []
    buckets: dict = {}
    for key in keys:
        name = rule(key.path)
        if name not in buckets:
            order.append(name)
            buckets[name] = []
        buckets[name].append(key)
    wanted = SECTION_ORDER.get(group)
    if wanted:
        order = [name for name in wanted if name in buckets] + [
            name for name in order if name not in wanted
        ]
    return [(name, buckets[name]) for name in order]


def left_sections(settings, view: View) -> List[tuple]:
    """需要左栏时的分组列表；搜索或无分组时为空。"""
    if view.query:
        return []
    secs = sections(settings, view.tab)
    if len(secs) > 1 and any(name for name, _keys in secs):
        return secs
    return []


def build_rows(settings, view: View) -> List[Row]:
    rows: List[Row] = []
    query = view.query.strip().lower()
    if query:
        for group, _label, _icon in TABS:
            matched = [
                key for key in _keys(settings, group)
                if query in key.path.lower() or query in key.label.lower()
            ]
            if matched:
                rows.append(Row("header", TAB_LABEL[group]))
                rows.extend(Row("item", key.short, key.path, key) for key in matched)
        return rows
    left = left_sections(settings, view)
    for index, (name, keys) in enumerate(sections(settings, view.tab)):
        if index and name and left:
            rows.append(Row("divider", ""))
        rows.extend(Row("item", key.short, key.path, key) for key in keys)
    return rows


def _items(rows: List[Row]) -> List[int]:
    return [index for index, row in enumerate(rows) if row.kind == "item"]


def ensure_selection(rows: List[Row], view: View) -> None:
    indexes = _items(rows)
    if not indexes:
        view.selected = None
        return
    if view.selected is None or not any(rows[i].path == view.selected for i in indexes):
        view.selected = rows[indexes[0]].path


def move(rows: List[Row], view: View, delta: int) -> None:
    indexes = _items(rows)
    if not indexes:
        return
    ensure_selection(rows, view)
    current = next((i for i in indexes if rows[i].path == view.selected), indexes[0])
    position = indexes.index(current)
    view.selected = rows[indexes[(position + delta) % len(indexes)]].path


def _section_starts(rows: List[Row]) -> List[int]:
    """每个分组的第一个条目行号：首段 + 每条分隔线之后。"""
    starts: List[int] = []
    expect = True
    for index, row in enumerate(rows):
        if row.kind == "divider":
            expect = True
        elif row.kind == "item" and expect:
            starts.append(index)
            expect = False
    return starts


def jump_section(rows: List[Row], view: View) -> None:
    starts = _section_starts(rows)
    if len(starts) <= 1:
        return
    ensure_selection(rows, view)
    current = next((i for i, row in enumerate(rows) if row.path == view.selected), 0)
    nxt = next((start for start in starts if start > current), starts[0])
    view.selected = rows[nxt].path


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _tab_line(view: View, columns: int) -> List[tuple]:
    if view.query:
        text = f" 搜索 · {view.query} · {view.matches} 项"
        return [("class:picker.hint", ellipsize(text, columns))]
    fragments: List[tuple] = [("", " ")]
    used = 1
    for segment_index, segment in enumerate(TAB_SEGMENTS):
        if segment_index:
            if used + 3 > columns:
                break
            fragments.append(("class:status.sep", " │ "))
            used += 3
        for position, index in enumerate(segment):
            group, label, icon = TABS[index]
            text = f"{icon}{label}"
            width = display_width(text) + (2 if position else 0)
            if used + width > columns:
                return fragments
            if position:
                fragments.append(("class:status.sep", " · "))
                used += 3
            active = group == view.tab
            fragments.append(("class:picker.title" if active else "class:status.sep", text))
            used += display_width(text)
    return fragments


def _body(settings, view: View, rows: List[Row], width: int, height: int) -> List[List[tuple]]:
    """列表主体：左侧分组列（有分组时）+ 条目 + 右侧滚动条。"""
    names = [row.label for row in rows if row.kind == "item"]
    left = left_sections(settings, view)
    left_width = 0
    if left:
        left_width = min(max(display_width(name) for name, _keys in left) + 3, 16)
    list_width = width - left_width - (2 if left_width else 0)
    inner_list = list_width - 1  # 留一列滚动条
    name_width = min(max(8, max((display_width(n) for n in names), default=8)), max(10, inner_list // 2))
    value_width = max(6, min(28, inner_list - name_width - 4))

    indexes = _items(rows)
    ensure_selection(rows, view)
    current = next((i for i in indexes if rows[i].path == view.selected), indexes[0] if indexes else 0)
    view.scroll = max(0, min(view.scroll, max(0, len(rows) - height)))
    if current < view.scroll:
        view.scroll = current
    elif current >= view.scroll + height:
        view.scroll = current - height + 1
    visible = rows[view.scroll:view.scroll + height]

    # 当前条目属于第几个分组：数它前面出现过多少条分隔线。
    section_index = sum(1 for row in rows[:current + 1] if row.kind == "divider")
    starts = _section_starts(rows)
    labels = {start: left[i][0] for i, start in enumerate(starts) if i < len(left)}

    thumb = max(1, round(height * height / max(1, len(rows))))
    thumb_start = 0 if len(rows) <= height else round(view.scroll * (height - thumb) / (len(rows) - height))

    out: List[List[tuple]] = []
    for offset, row in enumerate(visible):
        fragments: List[tuple] = []
        if left_width:
            absolute = view.scroll + offset
            if row.kind == "divider":
                fragments.append(("", " " * left_width))
            else:
                label = labels.get(absolute)
                section = sum(1 for r in rows[:absolute] if r.kind == "divider")
                style = "class:picker.title" if section == section_index else "class:status.sep"
                if label:
                    fragments.append((style, "▍ " + pad(ellipsize(label, left_width - 2), left_width - 2)))
                else:
                    fragments.append((style, "▍" + " " * (left_width - 1)))
            fragments.append(("class:status.sep", "│ "))
        if row.kind == "divider":
            fragments.append(("", " " * inner_list))
        elif row.kind == "header":
            fragments.append(("class:table.header", pad(ellipsize(row.label, inner_list), inner_list)))
        elif row.kind == "item":
            selected = row.path == view.selected
            marker = "❯ " if selected else "  "
            name_style = "class:picker.title" if selected else "class:status.sep"
            fragments.append((name_style, marker + pad(ellipsize(row.label, name_width), name_width)))
            value = ellipsize(format_value(settings.get(row.path)), value_width)
            value_style = "class:picker.title" if selected else ""
            fragments.append((value_style, "  " + pad(value, value_width)))
            gap = inner_list - 2 - name_width - 2 - value_width
            fragments.append(("", " " * max(0, gap)))
        fragments.append((
            "class:completion-icon" if len(rows) > height and thumb_start <= offset < thumb_start + thumb else "class:status.sep",
            " " if len(rows) <= height else ("█" if thumb_start <= offset < thumb_start + thumb else "│"),
        ))
        out.append(fragments)
    while len(out) < height:
        out.append([
            ("", " " * (left_width + (2 if left_width else 0))),
            ("", " " * inner_list),
            ("", " " if len(rows) <= height else "│"),
        ])
    return out


def description(settings, view: View, rows: List[Row]) -> tuple:
    """(正文, 右侧生效标记)。"""
    if view.editing is not None:
        hint = f"输入 {view.editing['path']}：{view.editing['text']}"
        if view.message:
            hint += f" · {view.message}"
        return hint, "Enter 保存 · Esc 取消"
    row = next((row for row in rows if row.path == view.selected), None)
    if row is None:
        return view.message, ""
    key = row.key
    tag = "重启生效" if str(getattr(key, "apply", "")) == "restart" else "立即生效"
    label = key.label
    if key.kind in ("int", "float") and (key.minimum is not None or key.maximum is not None):
        low = "-" if key.minimum is None else str(key.minimum)
        high = "-" if key.maximum is None else str(key.maximum)
        label += f"（{low} ~ {high}）"
    text = f"{key.path} · {label}"
    if view.message:
        text += f" · {view.message}"
    return text, tag


def preview(settings, state, width: int) -> str:
    """外观页的状态行预览；其他页留空。"""
    if not width:
        return ""
    try:
        channel, model = state.selection()
        cwd = os.getcwd()
        return "预览  " + "".join(text for _style, text in editor_status(
            max(20, width),
            logo=str(settings.get("ui.logo") or "α"),
            model=f"{model.id} ({channel.name or channel.id})",
            cwd=cwd,
            git=None,
            cost=None,
            tokens=None,
            window=state.window(),
            compact_at=0,
            bar_min=int(settings.get("ui.bar_min_width")),
            bar_max=int(settings.get("ui.bar_max_width")),
            mode=state.mode,
        ))
    except Exception:
        return ""


def render(settings, state, view: View, columns: int, rows_total: int) -> List[List[tuple]]:
    width = max(40, columns)
    inner = width - 4
    rows = build_rows(settings, view)
    view.matches = sum(1 for row in rows if row.kind == "item")
    body_height = max(3, rows_total - 9)
    head = " 设置 "
    out: List[List[tuple]] = [[
        ("class:status.sep", "╭─"),
        ("class:picker.title", head),
        ("class:status.sep", "─" * max(0, width - 3 - display_width(head)) + "╮"),
    ]]
    tabs = _tab_line(view, inner)
    used = sum(display_width(text) for _style, text in tabs)
    tabs.append(("", " " * max(0, inner - used)))
    out.append([("class:status.sep", "│ "), *tabs, ("class:status.sep", " │")])
    out.append([("class:status.sep", "├" + "─" * (width - 2) + "┤")])
    for fragments in _body(settings, view, rows, inner, body_height):
        out.append([("class:status.sep", "│ "), *fragments, ("class:status.sep", " │")])
    out.append([("class:status.sep", "├" + "─" * (width - 2) + "┤")])
    desc, tag = description(settings, view, rows)
    tag_width = display_width(tag)
    desc_width = max(1, inner - tag_width)
    out.append([
        ("class:status.sep", "│ "),
        ("class:picker.hint", pad(ellipsize(desc, desc_width), desc_width)),
        ("class:picker.title", tag),
        ("class:status.sep", " │"),
    ])
    shown = preview(settings, state, inner) if view.tab == "ui" and view.editing is None else ""
    out.append([
        ("class:status.sep", "│ "),
        ("", pad(ellipsize(shown, inner), inner)),
        ("class:status.sep", " │"),
    ])
    out.append([("class:status.sep", "├" + "─" * (width - 2) + "┤")])
    footer = "⏎/␣ 修改 · ⇥ 跳分组 · ←/→ 切页 · 直接输入搜索 · Esc 关闭"
    out.append([("class:status.sep", "│ "), ("class:picker.hint", pad(footer, inner)), ("class:status.sep", " │")])
    out.append([("class:status.sep", "╰" + "─" * (width - 2) + "╯")])
    return out


def run(state, apply: Callable, theme: str = "default") -> None:
    """打开设置面板；`apply(state, path, raw) -> (ok, message)` 负责校验与落盘。"""
    view = View()
    rows_cache: List[Row] = []

    def current_rows() -> List[Row]:
        nonlocal rows_cache
        rows_cache = build_rows(state.settings, view)
        ensure_selection(rows_cache, view)
        return rows_cache

    def change(path: str, raw: str) -> None:
        ok, message = apply(state, path, raw)
        view.message = ("" if ok else "错误：") + message

    def edit(row: Row) -> None:
        key = row.key
        if key.kind == "bool":
            change(row.path, "false" if state.settings.get(row.path) else "true")
            return
        choices = list(key.choices()) if key.choices is not None else []
        if choices:
            current = format_value(state.settings.get(row.path))
            index = choices.index(current) if current in choices else -1
            change(row.path, choices[(index + 1) % len(choices)])
            return
        view.editing = {"path": row.path, "text": format_value(state.settings.get(row.path))}
        view.message = ""

    kb = KeyBindings()

    def refresh() -> None:
        get_app().invalidate()

    @kb.add("escape", eager=True)
    def _(event) -> None:
        if view.editing is not None:
            view.editing = None
        elif view.query:
            view.query = ""
        else:
            event.app.exit()
        refresh()

    @kb.add("c-c", eager=True)
    def _(event) -> None:
        event.app.exit()

    @kb.add("left", eager=True)
    @kb.add("right", eager=True)
    def _(event) -> None:
        if view.editing is not None:
            return
        view.query = ""
        order = [group for group, _label, _icon in TABS]
        step = -1 if event.key_sequence[-1].key == "left" else 1
        index = order.index(view.tab)
        view.tab = order[(index + step) % len(order)]
        view.selected = None
        view.scroll = 0
        refresh()

    @kb.add("up", eager=True)
    @kb.add("down", eager=True)
    def _(event) -> None:
        if view.editing is not None:
            return
        move(current_rows(), view, -1 if event.key_sequence[-1].key == "up" else 1)
        refresh()

    @kb.add("tab", eager=True)
    def _(event) -> None:
        if view.editing is not None or view.query:
            return
        jump_section(current_rows(), view)
        refresh()

    @kb.add("enter", eager=True)
    @kb.add(" ", filter=Condition(lambda: view.editing is None and not view.query))
    def _(event) -> None:
        if view.editing is not None:
            path = view.editing["path"]
            text = view.editing["text"]
            ok, message = apply(state, path, text)
            view.message = ("" if ok else "错误：") + message
            if ok:
                view.editing = None
            refresh()
            return
        rows = current_rows()
        row = next((row for row in rows if row.path == view.selected), None)
        if row is not None:
            edit(row)
        refresh()

    @kb.add("backspace", eager=True)
    def _(event) -> None:
        if view.editing is not None:
            view.editing["text"] = view.editing["text"][:-1]
        elif view.query:
            view.query = view.query[:-1]
        refresh()

    @kb.add(Keys.Any)
    def _(event) -> None:
        data = event.data
        if not data or not data.isprintable():
            return
        if view.editing is not None:
            view.editing["text"] += data
        else:
            view.query += data
        refresh()

    def text() -> FormattedText:
        size = get_app().output.get_size()
        rows = render(state.settings, state, view, size.columns, size.rows)
        fragments: List[tuple] = []
        for index, row in enumerate(rows):
            fragments.extend(row)
            if index < len(rows) - 1:
                fragments.append(("", "\n"))
        return FormattedText(fragments)

    current_rows()
    Application(
        layout=Layout(Window(FormattedTextControl(text), wrap_lines=False)),
        key_bindings=kb,
        style=style_for(theme),
        full_screen=True,
        mouse_support=False,
    ).run(in_thread=True)
