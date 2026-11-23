"""最近一次工具调用的详情视图：Ctrl-O 展开、按页滚动查看完整参数与结果。

只保留最近一次调用及其结束状态，不建第二套历史库；折行按「内容版本 + 宽度」
缓存，重绘与滚动不重复折行大输出。文本一律经 render.sanitize，换行与制表
保义保留。输入焦点始终留在编辑器，本组件不持有任何可聚焦部件。
"""

from __future__ import annotations

import json

from prompt_toolkit.utils import get_cwidth

from .render import sanitize
from .ui import GLYPH, ellipsize

_STATUS = {"running": "运行中", "ok": "完成", "failed": "失败"}
_TAB_STOP = 8


def _as_text(value) -> str:
    """参数/结果/错误的展示文本：dict、list 转 JSON，其余转字符串。"""
    if value is None or isinstance(value, str):
        return value or ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _wrap(text: str, width: int) -> list[str]:
    """按终端列宽折行；制表按标准列展开，组合字符不额外占列。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        current = ""
        used = column = 0
        for value in raw:
            content = " " * (_TAB_STOP - column % _TAB_STOP) if value == "\t" else value
            for ch in content:
                size = get_cwidth(ch)
                column += size
                if current and used + size > width:
                    lines.append(current)
                    current, used = ch, size
                else:
                    current += ch
                    used += size
        lines.append(current)
    return lines


class ToolDetails:
    """最近一次工具调用详情：记录事件、展开开关、按页滚动与渲染。"""

    def __init__(self) -> None:
        self.expanded = False
        self._record: dict | None = None
        self._revision = 0
        self._scroll = 0
        self._viewport = 0
        self._cache_key: tuple | None = None
        self._cache_lines: list[str] = []

    def update(self, kind: str, **data) -> None:
        """消费私有 tool_start/tool_done/tool_error 事件；其他事件忽略。"""
        if kind == "tool_start":
            self._record = {
                "call_id": data.get("call_id"),
                "tool": str(data.get("tool") or "tool"),
                "args": data.get("args"),
                "duration": None,
                "result": None,
                "error": None,
                "failed": False,
                "ended": False,
            }
            self._revision += 1
            self._scroll = 0
            return
        if kind not in ("tool_done", "tool_error") or self._record is None:
            return
        record = self._record
        call_id = data.get("call_id")
        known = record["call_id"]
        if call_id is not None or known is not None:
            if call_id != known:
                return  # 任一侧有 ID 就要求精确相等；无 ID 的迟到完成不得覆盖
        elif str(data.get("tool") or "") != record["tool"]:
            return  # 双方均无 call_id 时才按工具名匹配
        error = data.get("error")
        record["failed"] = bool(error) or kind == "tool_error"
        record["error"] = error
        record["result"] = data.get("result")
        record["duration"] = data.get("duration")
        record["ended"] = True
        self._revision += 1

    def clear(self) -> None:
        """丢弃保留的工具，收起视图并复位滚动与折行缓存。"""
        self._record = None
        self.expanded = False
        self._scroll = 0
        self._revision += 1

    def toggle(self) -> bool:
        """无工具时返回 False；否则翻转展开状态并返回 True。"""
        if self._record is None:
            return False
        self.expanded = not self.expanded
        return True

    def scroll(self, pages: int) -> None:
        """按正文视口页数滚动；越界在渲染时按实际内容收敛。"""
        self._scroll += max(1, self._viewport) * int(pages)

    def render(self, width: int, height: int) -> list[tuple[str, str]]:
        """渲染为 (样式, 文本) 行：夹取的标题、分页正文、按键与位置脚注。"""
        width = max(1, int(width))
        height = max(1, int(height))
        if self._record is None:
            return []
        record = self._record
        body = self._body(width)
        body_height = max(1, height - 2)
        self._viewport = body_height
        total = len(body)
        self._scroll = min(max(self._scroll, 0), max(0, total - body_height))
        start = self._scroll
        end = min(total, start + body_height)

        status = "failed" if record["failed"] else ("ok" if record["ended"] else "running")
        heading = f"{GLYPH['title']} {sanitize(record['tool'])} · {_STATUS[status]}"
        duration = record["duration"]
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            heading += f" · {duration:.1f}s"
        rows = [("", ellipsize(heading, width))]
        rows.extend(("", line) for line in body[start:end])
        position = f"行 {start + 1}-{end}/{total}" if total else "行 0/0"
        footer = f"{position} · Ctrl-O 关闭 · PgUp/PgDn"
        rows.append(("", ellipsize(footer, width)))
        return rows

    def _body(self, width: int) -> list[str]:
        """折行后的完整正文；按内容版本与宽度缓存。"""
        key = (self._revision, width)
        if self._cache_key == key:
            return self._cache_lines
        record = self._record
        lines: list[str] = []
        if record is not None:
            args_text = _as_text(record["args"])
            if args_text:
                lines.append("参数")
                lines.extend(_wrap(sanitize(args_text), width))
                lines.append("")
            if record["error"]:
                lines.append("错误")
                lines.extend(_wrap(sanitize(_as_text(record["error"])), width))
                lines.append("")
            result_text = _as_text(record["result"])
            if result_text:
                lines.append("结果")
                lines.extend(_wrap(sanitize(result_text), width))
        while lines and lines[-1] == "":
            lines.pop()
        self._cache_key, self._cache_lines = key, lines
        return lines
