"""终端表现：命令表、符号、样式、状态条与光标。"""

import os
import sys
from typing import Dict

import questionary
from prompt_toolkit.styles import Style as PromptStyle
from prompt_toolkit.utils import get_cwidth

from . import usage_stats
from .render import sanitize

# 配置向导与菜单的样式：只加粗关键位置，不使用主题色。
UI_STYLE = questionary.Style([
    ("qmark", "bold"),
    ("pointer", "bold"),
    ("highlighted", "bg:default bold"),
    ("selected", "bg:default bold"),
])

COMMANDS = [
    ("/help", "显示所有命令"),
    ("/config", "查看或修改配置；/config start <动画名> 切换启动动画"),
    ("/provide", "编辑渠道与模型"),
    ("/model", "切换模型（<渠道>/<模型>、next、prev）"),
    ("/mode", "切换权限模式（read-only / workspace-write / danger-full-access）"),
    ("/todos", "查看当前会话的任务清单"),
    ("/export", "导出当前会话为 markdown（可跟路径）"),
    ("/image", "发送本地图片给模型并尝试在终端内预览：/image <路径>"),
    ("/plugin", "查看或启停 MCP 插件与技能包（重启生效）：/plugin <名称>"),
    ("/skills", "列出发现的技能（模型可用 get_skill_instructions 读取）"),
    ("/panel", "查看/切换扩展面板：/panel <名>，off 关闭"),
    ("/redo", "重新生成最后一轮回复"),
    ("/edit", "编辑上一条用户消息重发（分叉，原轮保留）：/edit <新消息>"),
    ("/fork", "回到某一轮之前，分叉成新会话：/fork [轮次] [新消息]"),
    ("/compact", "立即压缩当前会话上下文"),
    ("/usage", "查看用量与花费（默认今天，可跟 week / month）"),
    ("/insight", "查看上下文分段占用（只读估算）"),
    ("/trust", "查看或撤销项目代码扩展的信任记录"),
    ("/memory", "查看当前记忆引擎并切换（空闲时弹菜单，重启生效）"),
    ("/session", "列出或切换历史会话"),
    ("/new", "新建会话"),
    ("/exit", "退出"),
]

# 符号表：Windows 控制台默认字体缺字较多，直接退回 ASCII；
# macOS/Linux 使用最简线条符号，不依赖 Nerd Font。
ASCII_UI = os.name == "nt"
GLYPH = {
    "prompt": ">" if ASCII_UI else "❯",
    "ok": "+" if ASCII_UI else "✔",
    "fail": "x" if ASCII_UI else "✘",
    "dot": "-" if ASCII_UI else "·",
    "working": "~" if ASCII_UI else "⎋",
    "bar": "-" if ASCII_UI else "─",
    "mark": "|" if ASCII_UI else "╎",
    "title": ">" if ASCII_UI else "▸",
    "pointer": ">" if ASCII_UI else "❯",
    "mode": ">" if ASCII_UI else "▶",
}
EDITOR_PROMPT = "> " if ASCII_UI else "❯ "

# 命令图标：Windows 不显示图标，其余平台用最简符号。
ICONS = {} if ASCII_UI else {
    "/help": "?",
    "/config": "≡",
    "/provide": "⚙",
    "/model": "◆",
    "/mode": "▣",
    "/redo": "↻",
    "/edit": "✎",
    "/fork": "⑂",
    "/todos": "☑",
    "/export": "⇱",
    "/image": "▤",
    "/plugin": "⌗",
    "/skills": "✦",
    "/panel": "▩",
    "/compact": "⇩",
    "/usage": "Σ",
    "/insight": "◎",
    "/trust": "⚿",
    "/memory": "❖",
    "/session": "▤",
    "/new": "+",
    "/exit": "x",
}

# 主题语义 token：主题只定义这些颜色（THEME_TOKENS），类名到样式的模板三主题共用
# （CLASS_TEMPLATES）。改配色只改 token 一处；新增界面元素只加一行模板。
# 模板里用 `{token}` 占位；`error` 目前没有界面元素使用，为失败态预留。
TOKENS = (
    "fg", "bright", "muted", "meta", "dim", "border", "surface",
    "accent", "prompt", "activity", "success", "warning", "error",
    "c1", "c2", "c3", "c4", "c5", "c6",
)

THEME_TOKENS = {
    "default": {
        "fg": "#e0e5ed", "bright": "#ffffff", "muted": "#8d9bac", "meta": "#8d9bac", "dim": "#73808f",
        "border": "#576270", "surface": "default",
        "accent": "#42c5f5", "prompt": "ansicyan", "activity": "#e0a45b",
        "success": "#7bd88f", "warning": "#e0a45b", "error": "#ff6b6b",
        # c1 上下文条、c2 费用、c3 补全正文、c4 待办标题、c5 进行中、c6 待处理
        "c1": "#42aec9", "c2": "#dec181", "c3": "#cfd6df", "c4": "#8ab4f8", "c5": "#ffd166", "c6": "#9aa0a6",
    },
    # 高对比：亮色文字、更明显的选中态，适配低对比终端或投影。
    "contrast": {
        "fg": "ansiwhite", "bright": "#ffffff", "muted": "ansiwhite", "meta": "#bbbbbb", "dim": "ansiwhite",
        "border": "ansiwhite", "surface": "default",
        "accent": "ansiyellow", "prompt": "ansiwhite", "activity": "ansiyellow",
        "success": "#7bd88f", "warning": "ansiyellow", "error": "ansired",
        "c1": "ansicyan", "c2": "ansiyellow", "c3": "#ffffff", "c4": "#ffffff", "c5": "#ffff00", "c6": "#aaaaaa",
    },
    # 霓虹：品红/青色点缀，与默认同为深底。
    "neon": {
        "fg": "#e1defa", "bright": "#ffffff", "muted": "#a8a6c4", "meta": "#8a8ab0", "dim": "#a8a6c4",
        "border": "#807caa", "surface": "default",
        "accent": "#7ef9ff", "prompt": "#ff6ec7", "activity": "#ff6ec7",
        "success": "#7bd88f", "warning": "#ffd166", "error": "#ff6b6b",
        "c1": "#7ef9ff", "c2": "#f8dc8c", "c3": "#d8d8f0", "c4": "#7ef9ff", "c5": "#ff6ec7", "c6": "#8a8ab0",
    },
}

# 类名 → 样式模板（三主题共用）。
CLASS_TEMPLATES = {
    "prompt": "{prompt} bold",
    "status.logo": "{dim}",
    "status.model": "{accent}",
    "status.path": "{fg}",
    "status.git": "{warning}",
    "status.cost": "{c2}",
    "status.context": "{c1}",
    "status.sep": "{border}",
    "status.hint": "{muted} italic",
    "status.activity": "{activity}",
    "completion-menu": "bg:{surface}",
    "completion-menu.completion": "bg:{surface} {c3}",
    "completion-menu.completion.current": "bg:{surface} bold {accent} noreverse",
    "completion-menu.meta.completion": "bg:{surface} {meta}",
    "completion-menu.meta.completion.current": "bg:{surface} bold {accent}",
    "completion-menu.multi-column-meta": "bg:{surface} {meta}",
    "completion-icon": "{accent}",
    "completion-command": "{bright}",
    "table.header": "bold {meta}",
    "table.total": "bold {fg}",
    "picker.title": "bold {accent}",
    "picker.subtitle": "{muted} italic",
    "picker.pointer": "bold {accent}",
    "picker.selected": "bold {accent}",
    "picker.description": "{muted}",
    "picker.hint": "{muted} italic",
    "picker.query": "{fg}",
    "todo.title": "bold {c4}",
    "todo.phase": "bold {c4}",
    "todo.completed": "fg:{success} strike",
    "todo.in_progress": "bold fg:{c5}",
    "todo.pending": "fg:{c6}",
}

# 个别主题对少数类的取舍与共用模板不同，在这里显式覆盖。
THEME_CLASS_OVERRIDES = {
    # 高对比：强调色加粗给模型名与图标，提示不斜体，表头用亮白、合计行用强调色、选择器标题用白。
    "contrast": {
        "status.model": "{accent} bold",
        "status.hint": "{muted}",
        "status.activity": "{activity} bold",
        "completion-icon": "{accent} bold",
        "completion-command": "{bright} bold",
        "table.header": "bold {muted}",
        "table.total": "bold {accent}",
        "picker.title": "bold {fg}",
    },
    # 霓虹：选择器指针用品红，与提示符同色。
    "neon": {
        "picker.pointer": "bold {prompt}",
    },
}


def _styles_for(name: str) -> Dict[str, str]:
    tokens = THEME_TOKENS[name]
    templates = {**CLASS_TEMPLATES, **THEME_CLASS_OVERRIDES.get(name, {})}
    return {cls: template.format(**tokens) for cls, template in templates.items()}


PT_STYLES = {name: _styles_for(name) for name in THEME_TOKENS}
# 与 config.settings.UI_THEMES 保持一致（config 不 import cli）。
assert set(PT_STYLES) >= {"default", "contrast", "neon"}
PT_STYLE = PromptStyle.from_dict(PT_STYLES["default"])


def style_for(theme: str):
    """按主题名取样式；未知主题回落默认。"""
    return PromptStyle.from_dict(PT_STYLES.get(theme, PT_STYLES["default"]))


def display_width(text: str) -> int:
    """显示列数与 prompt_toolkit 的光标、折行计算保持一致。"""
    return get_cwidth(text)


def pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补齐；`right` 为真时右对齐。"""
    gap = max(0, width - display_width(text))
    return " " * gap + text if right else text + " " * gap


def emit(state, text: str = "", style: str = "") -> None:
    """报告面板挂接时收集 (文本, 样式)；未挂接（非交互/测试）时直接打印。"""
    sink = getattr(state, "report_lines", None)
    if sink is None:
        print(text)
    else:
        sink.append((text, style))


def print_help(state=None) -> None:
    emit(state, "命令", "bold")
    emit(state)
    rank = {name: i for i, name in enumerate(usage_stats.ordered([c for c, _ in COMMANDS]))}
    for command, description in sorted(COMMANDS, key=lambda e: rank[e[0]]):
        icon = ICONS.get(command, "")
        prefix = f"{icon} " if icon else "  "
        emit(state, f"  {prefix}{command:<10}{description}")


def fmt_window(value: int) -> str:
    """把上下文窗口压成 128k / 1M。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def context_bar(used: int, total: int, width: int, compact_at: int = 0) -> str:
    """在给定列宽内标出占用位置、压缩阈值和右端容量。"""
    capacity = fmt_window(total)
    ratio = max(0.0, min(used / total, 1.0)) if total else 0.0
    label = f"{ratio:.0%}"
    room = width - len(capacity) - 1
    if room <= len(label):
        return f"{label} {capacity}"
    chars = [GLYPH["bar"]] * room
    if compact_at and total:
        chars[min(room - 1, int(compact_at / total * room))] = GLYPH["mark"]
    pos = min(int(ratio * room), room - len(label))
    chars[pos:pos + len(label)] = label
    return "".join(chars) + " " + capacity


def editor_status(width, *, logo, model, cwd, git, cost, tokens, window, compact_at, bar_min, bar_max, mode=""):
    """按可见列排版一行状态；先压缩目录，再省略辅助信息。"""
    if width < 1:
        return []
    sep = " > " if ASCII_UI else " › "
    parts = {
        "logo": logo, "model": model, "path": cwd, "git": git.display() if git else "",
        "cost": f"${cost:.4f}" if cost is not None else "",
        "mode": f"{GLYPH['mode']} {mode}" if mode else "",
    }
    for key in parts:
        parts[key] = sanitize(parts[key]).replace("\n", " ").replace("\t", " ")
    context = fmt_window(window) if window else ""
    if window:
        context = f"{max(0.0, min((tokens or 0) / window, 1.0)):.0%} {context}"

    def required():
        lengths = [display_width(value) for value in parts.values() if value]
        return sum(lengths) + len(sep) * max(0, len(lengths) - 1) + (2 + len(context) if context else 0)

    overflow = required() - width
    if overflow > 0 and parts["path"]:
        path_width = display_width(parts["path"])
        parts["path"] = ellipsize(parts["path"], max(min(8, path_width), path_width - overflow))
    if required() > width and git:
        parts["git"] = git.branch
    for key in ("cost", "git", "mode"):
        if required() > width:
            parts[key] = ""
    if required() > width:
        context = ""
    for key in ("path", "logo"):
        if required() > width:
            parts[key] = ""
    parts["model"] = ellipsize(parts["model"], width)

    fragments = []
    for key, text in parts.items():
        if text:
            if fragments:
                fragments.append(("class:status.sep", sep))
            fragments.append((f"class:status.{key}", text))
    if context:
        room = width - sum(display_width(text) for _, text in fragments)
        bar_width = min(room - 2, bar_max)
        if bar_width >= max(bar_min, len(context)):
            context = context_bar(tokens or 0, window, bar_width, compact_at)
        fragments.append(("", " " * (room - len(context))))
        fragments.append(("class:status.context", context))
    return fragments


def completion_rows(completions, index, columns, max_rows=10):
    """补全菜单：指针、对齐的命令列与右侧滚动条；每行是 (style, text) 片段。"""
    total = len(completions)
    if not total or columns < 16:
        return []
    visible = max(1, min(total, max_rows))
    start = 0
    if index is not None and index >= visible:
        start = min(index - visible + 1, total - visible)
    window = completions[start:start + visible]
    names = [completion.display_text or completion.text for completion in window]
    name_width = min(max(display_width(name) for name in names), max(8, columns // 2))
    thumb = max(1, round(visible * visible / total))
    thumb_start = 0 if total <= visible else round(start * (visible - thumb) / (total - visible))
    meta_width = max(1, columns - 5 - name_width)
    rows = []
    for offset, completion in enumerate(window):
        current = index is not None and start + offset == index
        name = names[offset]
        row = []
        if current:
            row.append(("class:completion-menu.completion.current", f"{GLYPH['pointer']} {name}"))
        else:
            row.append(("", "  "))
            row.extend((style or "", text) for style, text in (completion.display or [("", completion.text)]))
        row.append(("", " " * max(1, name_width - display_width(name) + 2)))
        meta = ellipsize(completion.display_meta_text or "", meta_width)
        row.append((
            "class:completion-menu.meta.completion.current" if current
            else "class:completion-menu.meta.completion",
            meta,
        ))
        remainder = columns - 5 - name_width - display_width(meta)
        row.append(("", " " * max(0, remainder)))
        in_thumb = thumb_start <= offset < thumb_start + thumb
        row.append((
            "class:completion-icon" if in_thumb else "class:completion-menu.meta.completion",
            "█" if in_thumb else "│",
        ))
        rows.append(row)
    return rows


class StatusLine:
    """单行状态：用 \\r 原地刷新；输出正文前必须先 clear，避免交错。"""

    def __init__(self) -> None:
        self._text = ""

    def _write(self, text: str) -> None:
        if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
            return
        sys.stdout.write("\r\x1b[K" + text)
        sys.stdout.flush()

    def update(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._write(text)

    def clear(self) -> None:
        if self._text:
            self._text = ""
            self._write("")


def set_cursor_color(color: str) -> None:
    """用 OSC 12 设置光标颜色；不支持的终端或 NO_COLOR 时跳过。"""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        sys.stdout.write(f"\x1b]12;{color}\x07")
        sys.stdout.flush()


def reset_cursor_color() -> None:
    """恢复终端默认光标颜色（OSC 112）。"""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        sys.stdout.write("\x1b]112\x07")
        sys.stdout.flush()


def ellipsize(text: str, width: int) -> str:
    """按显示宽度截断，保留省略号。"""
    if width <= 0 or display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if display_width(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


TODO_STYLES = {
    "completed": "class:todo.completed",
    "in_progress": "class:todo.in_progress",
    "pending": "class:todo.pending",
}
TODO_MARKS = (
    {"completed": "x", "in_progress": ">", "pending": "."} if ASCII_UI
    else {"completed": "✔", "in_progress": "▶", "pending": "·"}
)


def todo_lines(todos, width: int) -> list:
    """TODO 树：阶段分组、完成计数、当前阶段展开、当前任务强调。"""
    if not todos:
        return []
    order: list = []
    grouped: dict = {}
    for index, item in enumerate(todos, 1):
        phase = item.get("phase") or "任务"
        if phase not in grouped:
            grouped[phase] = []
            order.append(phase)
        grouped[phase].append((index, item))

    done = sum(1 for item in todos if item.get("status") == "completed")
    lines = [("class:todo.title", f"TODO {done}/{len(todos)}")]
    active_phase = next(
        (item.get("phase") or "任务" for item in todos if item.get("status") == "in_progress"), None
    )
    if active_phase is None:
        active_phase = next(
            (item.get("phase") or "任务" for item in todos if item.get("status") == "pending"), None
        )
    if active_phase is None:
        active_phase = order[-1]

    for position, phase in enumerate(order):
        items = grouped[phase]
        branch = "└─" if position == len(order) - 1 else "├─"
        count_done = sum(1 for _, item in items if item.get("status") == "completed")
        header = f"{branch} {phase} {count_done}/{len(items)}"
        lines.append(("class:todo.phase", ellipsize(header, width)))
        if phase != active_phase:
            continue
        stem = "   " if position == len(order) - 1 else "│  "
        for order_index, (index, item) in enumerate(items):
            glyph = "└─" if order_index == len(items) - 1 else "├─"
            status = item.get("status", "pending")
            mark = TODO_MARKS.get(status, TODO_MARKS["pending"])
            prefix = ellipsize(f"{stem}{glyph} {mark} {index}. ", max(1, width - 1))
            body_width = max(1, width - display_width(prefix))
            for offset, chunk in enumerate(_wrap(item.get("text", ""), body_width)):
                lead = prefix if offset == 0 else " " * display_width(prefix)
                lines.append((TODO_STYLES.get(status, TODO_STYLES["pending"]), ellipsize(lead + chunk, width)))
    limit = 12
    if len(lines) > limit:
        active = TODO_STYLES["in_progress"]
        current = next((line for line in lines[limit:] if line[0] == active), None)
        kept = lines[:limit] if current is None else lines[:limit - 1] + [current]
        hidden = len(lines) - len(kept)
        lines = kept + [("class:todo.pending", ellipsize(f"… 还有 {hidden} 行；/todos 查看全部", width))]
    return lines


def _wrap(text: str, width: int) -> list:
    """按显示宽度折行；超长词硬切，保证每行不超过 width。"""
    width = max(1, width)
    out: list = []
    current = ""

    def hard_cut(word: str) -> None:
        nonlocal current
        for char in word:
            if display_width(current + char) > width:
                if current:
                    out.append(current)
                current = ""
            current += char

    for word in str(text).split():
        candidate = f"{current} {word}".strip() if current else word
        if display_width(candidate) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
            current = ""
        hard_cut(word)
    if current:
        out.append(current)
    return out or [""]




def report_lines(title, lines, scroll, columns, rows):
    """只读报告面板：边框、标题、滚动正文与页脚；每行是 (style, text) 片段。"""
    width = max(24, columns)
    inner = width - 4
    body_rows = max(1, rows - 4)
    total = len(lines)
    scroll = max(0, min(scroll, max(0, total - body_rows)))
    head = f" {title} "
    top = [
        ("class:status.sep", "╭─"),
        ("class:picker.title", head),
        ("class:status.sep", "─" * max(0, width - 3 - display_width(head)) + "╮"),
    ]
    footer = "↑/↓ 滚动 · Esc 关闭"
    if total > body_rows:
        footer += f" · {scroll + 1}–{min(total, scroll + body_rows)}/{total}"
    out = [top]
    for index in range(body_rows):
        item = lines[scroll + index] if scroll + index < total else ""
        text, style = item if isinstance(item, tuple) else (item, "")
        out.append([
            ("class:status.sep", "│ "),
            (style, pad(ellipsize(text, inner), inner)),
            ("class:status.sep", " │"),
        ])
    out.append([("class:status.sep", "├" + "─" * (width - 2) + "┤")])
    out.append([
        ("class:status.sep", "│ "),
        ("class:picker.hint", pad(footer, inner)),
        ("class:status.sep", " │"),
    ])
    out.append([("class:status.sep", "╰" + "─" * (width - 2) + "╯")])
    return out
