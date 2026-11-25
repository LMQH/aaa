"""受控 ANSI 渲染：Markdown 分块与 diff 行/词高亮。

只做「文本 → ANSI 字符串」，不直接写终端、不持光标：输出由 emitter 统一提交，
避免 Rich 与 prompt_toolkit 抢屏。颜色固定走 truecolor；调用方在无颜色时使用纯文本。
"""

import difflib
import io
import re
from typing import Callable, List, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from .renderers import split_block

RESET = "\x1b[0m"
# C0（保留换行与制表）与 C1、CSI、OSC：模型/工具文本不得驱动终端。
_CONTROL = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(text: str) -> str:
    """移除终端控制序列，保留换行与制表符。"""
    return _CONTROL.sub("", text)


def _console(width: Optional[int] = None, color: bool = True) -> Console:
    return Console(
        file=io.StringIO(), force_terminal=True, color_system="truecolor",
        width=width, soft_wrap=True, no_color=not color,
    )


def render_markdown(text: str, width: Optional[int] = None, color: bool = True) -> str:
    """整段 Markdown → ANSI；保留可读文本，样式交由 Rich。"""
    if not color:
        return text.rstrip("\n") + "\n"
    console = _console(width, color)
    # The terminal compositor reads SGR styles, not OSC hyperlinks.
    console.print(Markdown(text, hyperlinks=False), end="")
    return console.file.getvalue().rstrip("\n") + "\n"


def _word_spans(old: str, new: str) -> List[Text]:
    """单删/单增配对行的词级高亮；变化片段反显，公共部分按增删底色。"""
    matcher = difflib.SequenceMatcher(a=old, b=new)
    rem, add = Text(), Text()
    rem.append("-", style="bold red")
    add.append("+", style="bold green")
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_chunk, new_chunk = old[i1:i2], new[j1:j2]
        if tag == "equal":
            rem.append(old_chunk, style="red")
            add.append(new_chunk, style="green")
        else:
            rem.append(old_chunk, style="bold white on red")
            add.append(new_chunk, style="bold white on green")
    return [rem, add]


def _diff_rows(text: str):
    """解析统一 diff，产出 (kind, payload)；配对行带行号与两侧文本。"""
    rows = []
    lines = text.splitlines()
    old_no = new_no = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(("--- ", "+++ ")):
            rows.append(("meta", line))
            index += 1
            continue
        if line.startswith("@@"):
            rows.append(("hunk", line))
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_no, new_no = int(match.group(1)), int(match.group(2))
            index += 1
            continue
        if line.startswith("-") and not line.startswith("--- "):
            removed = []
            while index < len(lines) and lines[index].startswith("-") and not lines[index].startswith("--- "):
                removed.append(lines[index][1:])
                index += 1
            added = []
            while index < len(lines) and lines[index].startswith("+") and not lines[index].startswith("+++ "):
                added.append(lines[index][1:])
                index += 1
            if len(removed) == len(added) and removed:
                for old, new in zip(removed, added):
                    rows.append(("pair", (old_no, new_no, old, new)))
                    old_no += 1
                    new_no += 1
            else:
                for old in removed:
                    rows.append(("removed", (old_no, old)))
                    old_no += 1
                for new in added:
                    rows.append(("added", (new_no, new)))
                    new_no += 1
            continue
        if line.startswith("+"):
            rows.append(("added", (new_no, line[1:])))
            new_no += 1
        elif line.startswith(" "):
            rows.append(("context", (old_no, new_no, line[1:])))
            old_no += 1
            new_no += 1
        else:
            rows.append(("meta", line))
        index += 1
    return rows


def render_diff(text: str, width: Optional[int] = None, color: bool = True) -> str:
    """统一 diff → ANSI（或纯文本）：行号、红绿行与配对行词级高亮。"""
    rows = _diff_rows(text)
    if not color:
        out = []
        for kind, payload in rows:
            if kind == "pair":
                old_no, new_no, old, new = payload
                out.append(f"{old_no:>4} -{old}")
                out.append(f"{new_no:>4} +{new}")
            elif kind == "removed":
                out.append(f"{payload[0]:>4} -{payload[1]}")
            elif kind == "added":
                out.append(f"{payload[0]:>4} +{payload[1]}")
            elif kind == "context":
                out.append(f"{payload[0]:>4} {payload[1]:>4} {payload[2]}")
            else:
                out.append(payload)
        return "\n".join(out) + "\n"
    console = _console(width, color)
    for kind, payload in rows:
        if kind == "pair":
            old_no, new_no, old, new = payload
            pair = _word_spans(old, new)
            numbered = []
            for number, rendered in zip((old_no, new_no), pair):
                line = Text(f"{number:>4} ")
                line.append(rendered)
                numbered.append(line)
            for rendered in numbered:
                console.print(rendered, soft_wrap=True)
        elif kind == "removed":
            console.print(Text(f"{payload[0]:>4} -", style="red").append(payload[1], style="red"))
        elif kind == "added":
            console.print(Text(f"{payload[0]:>4} +", style="green").append(payload[1], style="green"))
        elif kind == "context":
            console.print(Text(f"{payload[0]:>4} {payload[1]:>4} {payload[2]}", style="dim"))
        elif kind == "hunk":
            console.print(Text(payload, style="cyan"))
        else:
            console.print(Text(payload, style="bold cyan") if payload.startswith(("--- ", "+++ ")) else Text(payload, style="dim"))
    return console.file.getvalue()


class MarkdownStream:
    """流式 Markdown：按块落地，未完成的块留在缓冲里。

    块边界是空行；代码围栏闭合时立即落地。这样标题/列表/代码块由 Rich 正确
    渲染，又不需要每来一个 token 就重排整段。
    """

    def __init__(self, width: Optional[int] = None, color: bool = True) -> None:
        self.width = width
        self.color = color
        self._lines: List[str] = []
        self._pending = ""
        self._in_code = False
        # 可选钩子：(围栏语言, 源码) -> 是否已接管展示；返回 True 时该块不再按 Markdown 渲染。
        self.on_code_block: Optional[Callable[[str, str], bool]] = None

    def feed(self, delta: str) -> str:
        """返回本次可落地的 ANSI 文本；没有块边界时返回空串。"""
        self._pending += delta
        *complete, self._pending = self._pending.split("\n")
        out: List[str] = []
        for line in complete:
            self._lines.append(line)
            if line.strip().startswith("```"):
                self._in_code = not self._in_code
                if not self._in_code:
                    out.append(self._flush())
            elif not self._in_code and not line.strip():
                out.append(self._flush())
        return "".join(out)

    def close(self) -> str:
        if self._pending:
            self._lines.append(self._pending)
            self._pending = ""
        return self._flush()

    def _flush(self) -> str:
        block = "\n".join(self._lines).strip("\n")
        self._lines = []
        if not block.strip():
            return ""
        if self.on_code_block is not None:
            split = split_block(block)
            if split is not None and self.on_code_block(*split):
                return ""
        return render_markdown(block, self.width, self.color)
