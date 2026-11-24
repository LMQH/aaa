"""只读采集上下文十层构成；不装配执行体、不实例化记忆引擎、不写库。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..memory.host import MemoryHost
from ..runtime.agent import (
    skills_for_mode,
    system_instructions,
    tool_names,
    tools_for_mode,
)
from .tokens import (
    CountResult,
    count_text,
    count_tool_schemas,
    tokenizer_label,
    tool_schema_payloads,
)


@dataclass
class Segment:
    number: str
    name: str
    count: Optional[CountResult]  # None 表示指针段（不重复计数）
    source: str
    note: str = ""


@dataclass
class Insight:
    window: Optional[int]
    usage: dict
    segments: List[Segment]
    tokenizer: str
    warnings: List[str] = field(default_factory=list)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
    return "\n".join(parts)


def _tool_calls_text(calls: Any) -> str:
    lines: list[str] = []
    for call in calls or ():
        if isinstance(call, dict):
            fn = call.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None
            name = name or call.get("name") or "?"
        else:
            fn = getattr(call, "function", None)
            name = getattr(fn, "name", None) or getattr(call, "name", "?")
            args = getattr(fn, "arguments", None) or getattr(call, "arguments", None)
        rendered = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
        lines.append(f"{name} {rendered}")
    return "\n".join(lines)


def _usage_of(run: Any) -> dict:
    metrics = _field(run, "metrics")
    if metrics is None:
        return {}

    def read(key: str) -> int:
        value = _field(metrics, key, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "input": read("input_tokens"),
        "output": read("output_tokens"),
        "cached": read("cached_tokens"),
    }


def _window_of(state: Any) -> Optional[int]:
    catalog = getattr(state, "catalog", None)
    if catalog is None:
        return None
    try:
        value = int(catalog.window(state.provider, state.model_id))
    except Exception:
        return None
    return value if value > 0 else None


def collect(state: Any) -> Insight:
    """采集当前会话的上下文构成；任何一段失败只影响该段（降级为空），不阻断。"""
    warnings: list[str] = []
    agent = state.runnable
    tools = list(getattr(agent, "tools", None) or ())
    memory = getattr(state, "memory", None)

    instructions = system_instructions(
        state.mode, tools, memory,
        project_max_chars=int(state.settings.get("project.instructions_max_chars")),
    )
    skills_bundle = skills_for_mode(getattr(state, "plugin_skills", None), state.mode)
    snippet_fn = getattr(skills_bundle, "get_system_prompt_snippet", None)
    skill_text = snippet_fn() if callable(snippet_fn) else ""

    try:
        builtin_names = set(tool_names(tools_for_mode(state.mode, state.settings)))
    except Exception:
        builtin_names = set()
        warnings.append("内置工具清单读取失败，工具未按内置/自定义分栏")
    payloads = tool_schema_payloads(tools)
    skills_tools_fn = getattr(skills_bundle, "get_tools", None)
    if callable(skills_tools_fn):
        # Agno 的技能读取工具按轮动态追加，不在 agent.tools 上；单独计入自定义栏。
        payloads += tool_schema_payloads(skills_tools_fn())
    builtin_payloads = [item for item in payloads if item.get("name") in builtin_names]
    custom_payloads = [item for item in payloads if item.get("name") not in builtin_names]

    user_text = assistant_text = reasoning_text = calls_text = results_text = summary_text = ""
    runs: list = []
    try:
        runs = list(agent.db.get_runs(
            session_id=state.session_id, sort_by="created_at", sort_order="asc",
        ) or ())
    except Exception:
        warnings.append("会话历史读取失败，历史段为空")
    for run in runs:
        for message in getattr(run, "messages", None) or ():
            role = _field(message, "role")
            content = _field(message, "content")
            if role == "user" and _field(message, "from_history", False):
                summary_text += _text_of(content) + "\n"
            elif role == "user":
                user_text += _text_of(content) + "\n"
            elif role == "assistant":
                assistant_text += _text_of(content) + "\n"
                reasoning_text += str(_field(message, "reasoning_content") or "") + "\n"
                calls_text += _tool_calls_text(_field(message, "tool_calls")) + "\n"
            elif role == "tool":
                results_text += _text_of(content) + "\n"

    memory_count: Optional[CountResult] = None
    memory_note = "已并入基础提示词，不重复计数"
    if isinstance(memory, MemoryHost):
        memory_note += "；召回块为运行期注入，不落历史，未计入"
        warnings.append("记忆召回为运行期注入，不落历史")
    else:
        memory_note = "记忆未启用"

    segments = [
        Segment("1", "基础提示词", count_text(instructions, state.model_id),
                "SYSTEM.md 内置/覆盖 + APPEND_SYSTEM + 项目 AGENTS.md"),
        Segment("2", "内置工具", count_tool_schemas(builtin_payloads, state.model_id),
                f"{len(builtin_payloads)} 个 schema"),
        Segment("3", "自定义工具", count_tool_schemas(custom_payloads, state.model_id),
                f"{len(custom_payloads)} 个 schema"),
        Segment("4", "技能索引", count_text(skill_text, state.model_id),
                "Agno <skills_system>，随系统消息发送"),
        Segment("5a", "记忆注入", memory_count, "见基础提示词", memory_note),
        Segment("5b", "压缩摘要", count_text(summary_text, state.model_id), "Agno 压缩摘要条目"),
        Segment("6", "用户消息", count_text(user_text, state.model_id), f"{len(runs)} 轮"),
        Segment("7", "工具调用", count_text(calls_text, state.model_id), "调用名与参数"),
        Segment("8", "工具结果", count_text(results_text, state.model_id), "工具返回"),
        Segment("9", "推理", count_text(reasoning_text, state.model_id), "reasoning_content"),
        Segment("10", "助手回复", count_text(assistant_text, state.model_id), "assistant 正文"),
    ]
    if not runs:
        warnings.append("当前会话暂无历史")
    warnings.append("内联附件（图片/文件）未计入")

    usage = _usage_of(runs[-1]) if runs else {}
    return Insight(
        window=_window_of(state),
        usage=usage,
        segments=segments,
        tokenizer=tokenizer_label(state.model_id),
        warnings=warnings,
    )
