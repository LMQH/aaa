"""aaa 自有指令和技能目录；系统消息仍由 Agno 组装。"""

import json
import logging
import os
from importlib.resources import files
from pathlib import Path
from typing import Collection, Optional, Sequence

from ..config.paths import APPEND_SYSTEM_PROMPT_FILE, SYSTEM_PROMPT_FILE
from ..plugins import Skill, project_dirs

logger = logging.getLogger(__name__)


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


def instructions_for(mode: str, tools: Collection[str], project_text: str = "") -> str:
    parts = [_optional(SYSTEM_PROMPT_FILE) or _builtin("SYSTEM.md")]
    append = _optional(APPEND_SYSTEM_PROMPT_FILE)
    if append:
        parts.append(append)
    if project_text:
        parts.append(project_text)
    parts.append(f"<permission_mode>{mode}</permission_mode>")
    if "run_shell_command" in tools:
        parts.append(_builtin("shell-windows.md" if os.name == "nt" else "shell-posix.md"))
    if "run_powershell" in tools:
        parts.append(_builtin("powershell.md"))
    return "\n\n".join(parts)


def skill_context(skills: Sequence[Skill]) -> Optional[str]:
    if not skills:
        return None
    entries = [{"name": skill.name, "description": skill.description} for skill in skills]
    return _builtin("skills.md") + "\n\n" + json.dumps(entries, ensure_ascii=False)
