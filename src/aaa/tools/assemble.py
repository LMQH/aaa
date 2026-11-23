"""工具装配：按「能力名清单」把工具装成一个列表。

契约（两个面之间唯一共享的词汇）：
- 能力名：`read` / `list` / `write` / `edit` / `move` / `delete` / `grep` / `shell` / `powershell`。
- 调用方给出：允许的能力集合、文件根、需要人工确认的工具名。
本模块不 import `boundary`：它不知道权限模式，也不知道谁决定这些参数。
合并策略与能力的职责在 `runtime`。
"""

from pathlib import Path
from typing import Collection, Dict, List, Optional

from .builtin.files import grep_tool, workspace_tool
from .builtin.powershell import PowerShellTools, find_powershell
from .builtin.processes import ProcessTools
from .builtin.todos import todo_add, todo_done, todo_list, todo_start
from .extensions.weather import WEATHER_TIMEOUT_SECONDS, weather_tool


def build_tools(
    capabilities: Collection[str],
    *,
    root: Path,
    confirm_tools: Collection[str] = (),
    file_caps: Optional[Dict[str, int]] = None,
    weather_timeout: Optional[int] = None,
    shell_timeout: Optional[int] = None,
    shell_output_bytes: Optional[int] = None,
    shell_tail: Optional[int] = None,
) -> list:
    """按能力名装出工具列表。

    Args:
        capabilities: 允许的能力名集合；未列出的能力不会出现在 schema 里。
        root: 文件类工具的工作区根。
        confirm_tools: 需要人工确认的工具名（当前只有命令类工具需要）。
        file_caps / weather_timeout / shell_timeout / shell_output_bytes /
        shell_tail: 数值上限，来自 `config.toml`；省略时用各模块默认值。
    """
    confirms = set(confirm_tools)
    tools: list = [weather_tool(weather_timeout or WEATHER_TIMEOUT_SECONDS), todo_add, todo_list, todo_done, todo_start]
    # 文件改动工具的工具名 → Workspace 的能力别名；确认名单由调用方给工具名。
    FILE_CONFIRM = (("write_file", "write"), ("edit_file", "edit"), ("move_file", "move"))
    tools.append(workspace_tool(
        root, capabilities, file_caps,
        confirm=[alias for name, alias in FILE_CONFIRM if name in confirms],
    ))

    if "grep" in capabilities:
        tools.append(grep_tool(root))

    if "shell" in capabilities:
        tools.append(
            ProcessTools(
                base_dir=root,
                timeout_seconds=shell_timeout,
                max_output_bytes=shell_output_bytes,
                default_tail=shell_tail,
                requires_confirmation_tools=[
                    name for name in ("run_shell_command",) if name in confirms
                ],
            )
        )

    if "powershell" in capabilities and find_powershell() is not None:
        tools.append(
            PowerShellTools(
                timeout_seconds=shell_timeout,
                max_output_bytes=shell_output_bytes,
                default_tail=shell_tail,
                requires_confirmation_tools=[
                    name for name in ("run_powershell",) if name in confirms
                ],
            )
        )
    return tools


def tool_names(tools: list) -> List[str]:
    """把装配好的工具展开成模型可见的工具名；提示词据此描述能力，避免与实际工具集分叉。"""
    names: List[str] = []
    for tool in tools:
        functions = getattr(tool, "functions", None)
        if functions:
            names.extend(functions)
        else:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
            if name:
                names.append(name)
    return names
