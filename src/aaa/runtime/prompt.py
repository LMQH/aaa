"""aaa 自有指令；系统消息（含技能索引）仍由 Agno 组装。"""

import logging
import os
import re
from importlib.resources import files
from pathlib import Path
from typing import Collection

from ..config.paths import APPEND_SYSTEM_PROMPT_FILE, SYSTEM_PROMPT_FILE
from ..plugins import project_dirs

logger = logging.getLogger(__name__)


# 工具指引段：文件名 → 任一在场即追加的工具名。指引只描述实际装配的工具。
SECTIONS = (
    ("files", ("read_file", "list_files", "grep", "write_file", "edit_file", "move_file", "delete_file")),
    ("jobs", ("job_output",)),
    ("todos", ("todo_add",)),
    ("delegation", ("delegate_task",)),
)
_BULLET_TOOL = re.compile(r"^- `([a-z_]+)`")


def _builtin(name: str) -> str:
    return files(__package__).joinpath("prompts", name).read_text(encoding="utf-8").strip()


def _optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        logger.warning("无法读取提示词 %s：%s", path, exc)
        return ""


def project_instructions(max_chars: int) -> str:
    """项目根 AGENTS.md 的注入块；上限为 0 或文件缺失时返回空。"""
    if max_chars <= 0:
        return ""
    text = _optional(project_dirs()["agents_md"])
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 16)].rstrip() + "\n…(已截断)"
    return f'<project_instructions source="AGENTS.md">\n{text}\n</project_instructions>'


def _prune(text: str, tools: Collection[str]) -> str:
    """删掉以不在场的工具名开头的条目。"""
    return "\n".join(
        line for line in text.split("\n")
        if not ((match := _BULLET_TOOL.match(line)) and match.group(1) not in tools)
    )


def instructions_for(mode: str, tools: Collection[str], project_text: str = "") -> str:
    """装配顺序：基础指令 → 用户追加 → 项目指令 → 当前目录 → 权限模式 → 工具指引。"""
    parts = [_optional(SYSTEM_PROMPT_FILE) or _builtin("SYSTEM.md")]
    append = _optional(APPEND_SYSTEM_PROMPT_FILE)
    if append:
        parts.append(append)
    if project_text:
        parts.append(project_text)
    parts.append(f"<cwd>{Path.cwd()}</cwd>")
    parts.append(_builtin("permission-modes.md"))
    parts.append(f"<permission_mode>{mode}</permission_mode>")
    if "run_shell_command" in tools:
        parts.append(_builtin("shell-windows.md" if os.name == "nt" else "shell-posix.md"))
    if "run_powershell" in tools:
        parts.append(_builtin("powershell.md"))
    for name, triggers in SECTIONS:
        if name == "jobs" and os.name == "nt":
            continue  # Windows 上后台任务不可用
        if any(tool in tools for tool in triggers):
            parts.append(_prune(_builtin(f"{name}.md"), tools))
    return "\n\n".join(parts)
