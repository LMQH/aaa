"""终端表现：命令表、符号、样式、状态条与光标。"""

import os
import sys

import questionary
from prompt_toolkit.styles import Style as PromptStyle
from prompt_toolkit.utils import get_cwidth

from .render import sanitize
from . import usage_stats

# 配置向导与菜单的样式：只加粗关键位置，不使用主题色。
UI_STYLE = questionary.Style([
    ("qmark", "bold"),
    ("pointer", "bold"),
    ("highlighted", "bold"),
    ("selected", "bold"),
])

COMMANDS = [
    ("/help", "显示所有命令"),
    ("/config", "查看或修改配置；/config start <动画名> 切换启动动画"),
    ("/provide", "编辑渠道与模型"),
    ("/model", "切换模型（<渠道>/<模型>、next、prev）"),
    ("/mode", "切换权限模式（read-only / workspace-write / danger-full-access）"),
    ("/todos", "查看当前会话的任务清单"),
    ("/export", "导出当前会话为 markdown（可跟路径）"),
    ("/image", "发送本地图片给模型：/image <路径>"),
    ("/plugin", "查看或启停 MCP 插件（重启生效）：/plugin <名称>"),
    ("/skills", "列出发现的技能（模型可用 load_skill 读取）"),
    ("/redo", "重新生成最后一轮回复"),
    ("/edit", "编辑上一条用户消息重发（分叉，原轮保留）：/edit <新消息>"),
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
}
EDITOR_PROMPT = "> " if ASCII_UI else "╰─ "

# 命令图标：Windows 不显示图标，其余平台用最简符号。
ICONS = {} if ASCII_UI else {
    "/help": "?",
    "/config": "≡",
    "/provide": "⚙",
    "/model": "◆",
    "/mode": "▣",
    "/redo": "↻",
    "/edit": "✎",
    "/todos": "☑",
    "/export": "⇱",
    "/image": "▤",
    "/plugin": "⌗",
    "/skills": "✦",
    "/compact": "⇩",
    "/usage": "Σ",
    "/memory": "❖",
    "/session": "▤",
    "/new": "+",
    "/exit": "x",
}

# 输入区与补全菜单的样式：深色菜单、青色提示符，其余保持低对比。
PT_STYLES = {
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
        "completion-menu.completion": "bg:#1e1e1e #cccccc",
        "completion-menu.completion.current": "bg:#333333 #ffffff",
        "completion-menu.meta.completion": "bg:#1e1e1e #888888",
        "completion-menu.meta.completion.current": "bg:#333333 #dddddd",
        "completion-icon": "#7fd1ff",
        "completion-command": "#ffffff",
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
        "completion-menu.completion": "bg:#000000 #ffffff",
        "completion-menu.completion.current": "bg:#ffffff #000000",
        "completion-menu.meta.completion": "bg:#000000 #bbbbbb",
        "completion-menu.meta.completion.current": "bg:#bbbbbb #000000",
        "completion-icon": "#00ffff bold",
        "completion-command": "#ffffff bold",
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
        "completion-menu.completion": "bg:#1b1b2c #d8d8f0",
        "completion-menu.completion.current": "bg:#ff6ec7 #1b1b2c",
        "completion-menu.meta.completion": "bg:#1b1b2c #8a8ab0",
        "completion-menu.meta.completion.current": "bg:#3a3a5c #ffffff",
        "completion-icon": "#7ef9ff",
        "completion-command": "#ffffff",
        "todo.title": "bold #7ef9ff",
        "todo.phase": "bold #7ef9ff",
        "todo.completed": "fg:#7bd88f strike",
        "todo.in_progress": "bold fg:#ff6ec7",
        "todo.pending": "fg:#8a8ab0",
    },
}
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


def print_help() -> None:
    print()
    rank = {name: i for i, name in enumerate(usage_stats.ordered([c for c, _ in COMMANDS]))}
    for command, description in sorted(COMMANDS, key=lambda e: rank[e[0]]):
        print(f"  {command:<10}{description}")
    print()


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


def editor_status(width, *, logo, model, cwd, git, cost, tokens, window, compact_at, bar_min, bar_max):
    """按可见列排版一行状态；先压缩目录，再省略辅助信息。"""
    if width < 1:
        return []
    sep = " > " if ASCII_UI else " › "
    parts = {
        "logo": logo, "model": model, "path": cwd, "git": git.display() if git else "",
        "cost": f"${cost:.4f}" if cost is not None else "",
    }
    for key in parts:
        parts[key] = sanitize(parts[key]).replace("\n", " ").replace("\t", " ")
    context = fmt_window(window) if window else ""
    if window and tokens is not None:
        context = f"{max(0.0, min(tokens / window, 1.0)):.0%} {context}"

    def required():
        lengths = [display_width(value) for value in parts.values() if value]
        return sum(lengths) + len(sep) * max(0, len(lengths) - 1) + (2 + len(context) if context else 0)

    overflow = required() - width
    if overflow > 0 and parts["path"]:
        path_width = display_width(parts["path"])
        parts["path"] = ellipsize(parts["path"], max(min(8, path_width), path_width - overflow))
    if required() > width and git:
        parts["git"] = git.branch
    for key in ("cost", "git"):
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
        if tokens is not None and bar_width >= max(bar_min, len(context)):
            context = context_bar(tokens, window, bar_width, compact_at)
        fragments.append(("", " " * (room - len(context))))
        fragments.append(("class:status.context", context))
    return fragments


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


